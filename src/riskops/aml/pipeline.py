"""Build the AML layer: generate, detect, deduplicate, aggregate, prioritise, persist.

Runs beside `pipeline.refresh` rather than inside it. Keeping them separate means
the payments warehouse can be rebuilt without regenerating the AML world and vice
versa, and it means this module can be read end to end as the answer to "what
actually happens between a transfer and a case on someone's screen".

The order is fixed and each step is counted, because the counts are what the
evaluation report is computed from and what the overview page displays. Nothing
is sampled, nothing is cached: the same seed produces the same warehouse.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from ..audit.log import AuditLog
from ..config import Settings
from ..db import replace_table, session
from . import detect, world
from .aggregate import aggregate
from .priority import PRIORITY_VERSION, prioritise
from .typology import TYPOLOGY_VERSION

AML_SCHEMA = "aml"

DDL: tuple[str, ...] = (
    "CREATE SCHEMA IF NOT EXISTS aml;",
    """
    CREATE TABLE IF NOT EXISTS aml.investigation_decisions (
        decision_id           VARCHAR PRIMARY KEY,
        case_id               VARCHAR NOT NULL,
        disposition           VARCHAR NOT NULL,
        next_state            VARCHAR NOT NULL,
        reason                VARCHAR NOT NULL,
        notes                 VARCHAR,
        evidence_alert_ids    VARCHAR,
        actor_role            VARCHAR NOT NULL,
        actor_id              VARCHAR NOT NULL,
        decided_at            TIMESTAMP NOT NULL,
        investigation_version VARCHAR NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS aml.build_log (
        batch_id          VARCHAR PRIMARY KEY,
        built_at          TIMESTAMP NOT NULL,
        seed              BIGINT NOT NULL,
        transfers         BIGINT NOT NULL,
        raw_alerts        BIGINT NOT NULL,
        deduped_alerts    BIGINT NOT NULL,
        duplicates_removed BIGINT NOT NULL,
        cases             BIGINT NOT NULL,
        review_capacity   BIGINT NOT NULL,
        typology_version  VARCHAR NOT NULL,
        priority_version  VARCHAR NOT NULL,
        world_version     VARCHAR NOT NULL,
        duration_seconds  DOUBLE NOT NULL
    );
    """,
)


@dataclass
class AmlBuildReport:
    batch_id: str
    seed: int
    counts: dict[str, int] = field(default_factory=dict)
    duplicates_removed: int = 0
    aggregation_rate: float = 0.0
    duration_seconds: float = 0.0
    typology_version: str = TYPOLOGY_VERSION
    priority_version: str = PRIORITY_VERSION
    world_version: str = world.WORLD_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "seed": self.seed,
            "counts": dict(self.counts),
            "duplicates_removed": self.duplicates_removed,
            "aggregation_rate": self.aggregation_rate,
            "duration_seconds": round(self.duration_seconds, 3),
            "typology_version": self.typology_version,
            "priority_version": self.priority_version,
            "world_version": self.world_version,
        }


def review_capacity(settings: Settings, case_count: int) -> int:
    """How many cases a team could actually open in this period.

    Configurable, and deliberately far below the alert count. A queue whose
    capacity equals its length is not a queue, and the interesting product
    question - what do you look at when you cannot look at everything - only
    exists when the number is real.
    """
    configured = int(settings.extra.get("aml_review_capacity", 0))
    if configured > 0:
        return configured
    return max(20, int(case_count * 0.35))


def detect_from_world(generated: world.AmlWorld, *, now: datetime) -> pd.DataFrame:
    """Detection over a generated world, with the provenance columns removed."""
    context = detect.AmlContext.blind(
        generated.transfers,
        accounts=generated.accounts,
        customers=generated.customers,
        beneficial_owners=generated.beneficial_owners,
        now=now,
    )
    return detect.run_all(context)


def build(
    settings: Settings,
    *,
    seed: int | None = None,
    n_transfers: int | None = None,
    persist: bool = True,
) -> tuple[AmlBuildReport, dict[str, pd.DataFrame]]:
    """Run the whole AML layer. Returns the report and every frame it produced."""
    started = time.perf_counter()
    now = datetime.fromisoformat(settings.as_of_date)
    batch_id = f"AMLBATCH_{datetime.now():%Y%m%d%H%M%S}"
    resolved_seed = settings.random_seed if seed is None else seed

    generated = world.generate(settings, seed=resolved_seed, n_transfers=n_transfers)
    raw_alerts = detect_from_world(generated, now=now)
    result = aggregate(raw_alerts, now=now)

    capacity = review_capacity(settings, len(result.cases))
    cases = prioritise(
        result.cases, generated.transfers, generated.accounts, generated.customers,
        now=now, review_capacity=capacity,
        sla_hours={
            "critical": settings.sla_hours_critical,
            "high": settings.sla_hours_high,
            "medium": settings.sla_hours_medium,
            "low": settings.sla_hours_low,
        },
    )
    if not cases.empty:
        cases["case_state"] = cases["within_capacity"].map(
            lambda inside: "queued" if inside else "new"
        )

    frames = {
        "aml.customers": generated.customers,
        "aml.accounts": generated.accounts,
        "aml.beneficial_owners": generated.beneficial_owners,
        "aml.devices": generated.devices,
        "aml.transfers": generated.transfers,
        "aml.scenarios": generated.scenarios,
        "aml.alerts": result.alerts,
        "aml.cases": cases,
    }

    report = AmlBuildReport(
        batch_id=batch_id,
        seed=resolved_seed,
        counts={
            "customers": len(generated.customers),
            "accounts": len(generated.accounts),
            "beneficial_owners": len(generated.beneficial_owners),
            "transfers": len(generated.transfers),
            "injected_scenarios": len(generated.scenarios),
            "raw_alerts": len(raw_alerts),
            "alerts": len(result.alerts),
            "cases": len(cases),
            "review_capacity": capacity,
            "within_capacity": int(cases["within_capacity"].sum()) if not cases.empty else 0,
        },
        duplicates_removed=result.duplicates_removed,
        aggregation_rate=result.aggregation_rate,
        duration_seconds=time.perf_counter() - started,
    )

    if persist:
        _persist(settings, frames, report, capacity, now)
    return report, frames


def _persist(
    settings: Settings,
    frames: dict[str, pd.DataFrame],
    report: AmlBuildReport,
    capacity: int,
    now: datetime,
) -> None:
    with session(settings) as con:
        for statement in DDL:
            con.execute(statement)
        for name, frame in frames.items():
            replace_table(con, name, frame)

        # Dispositions belong to the world they were recorded against. A rebuild
        # generates a different world from the same code, so carrying decisions
        # across would attach a person's written reasoning to transfers that no
        # longer exist. The build log itself accumulates, so run history survives.
        con.execute("DELETE FROM aml.investigation_decisions")

        con.execute(
            """
            INSERT OR REPLACE INTO aml.build_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                report.batch_id, datetime.now().replace(microsecond=0), report.seed,
                report.counts["transfers"], report.counts["raw_alerts"],
                report.counts["alerts"], report.duplicates_removed,
                report.counts["cases"], capacity,
                report.typology_version, report.priority_version, report.world_version,
                round(report.duration_seconds, 3),
            ],
        )

        AuditLog(con).append(
            actor_role="system", actor_id="aml_pipeline", action="aml.build_completed",
            object_type="batch", object_id=report.batch_id,
            summary=(
                f"{report.counts['transfers']} transfers, {report.counts['alerts']} alerts "
                f"({report.duplicates_removed} duplicates removed), "
                f"{report.counts['cases']} cases (seed {report.seed})"
            ),
            payload=report.as_dict(),
            occurred_at=now,
        )


def snapshot(settings: Settings) -> dict[str, object]:
    """Row counts for the status command. Missing tables report as absent."""
    tables = (
        "aml.customers", "aml.accounts", "aml.transfers", "aml.alerts",
        "aml.cases", "aml.investigation_decisions",
    )
    out: dict[str, object] = {}
    with session(settings, read_only=True) as con:
        for table in tables:
            try:
                out[table] = int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            except Exception:
                out[table] = "absent"
    return out
