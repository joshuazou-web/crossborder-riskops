"""Refresh orchestration - one command builds the whole product.

The order is fixed and each step is recorded:

    generate -> land raw -> replay to core -> validate -> rules -> model ->
    policy -> cases -> AI briefs -> simulated review history -> marts

Two rules the orchestration obeys:

  * **The validation gate is a gate.** An error-severity failure rolls the core
    tables back to the previous good version and stops. A dashboard showing
    stale-but-correct data beats one showing fresh-but-wrong data.
  * **Every judgement records its version.** The refresh log carries the
    taxonomy, rules and model versions and the seed. A metric without its
    versions is not reproducible, and this project only quotes reproducible
    metrics.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from ..ai.copilot import investigate, record_invocation
from ..ai.prompt import build_case_packet
from ..ai.provider import build_provider
from ..ai.schema import CaseBrief
from ..audit.log import AuditLog
from ..config import Settings
from ..db import (
    append_table,
    init_schema,
    read_sql,
    replace_table,
    restore_table,
    run_sql_file,
    session,
    snapshot_table,
)
from ..generator.synth import PER_USD, generate
from ..money import CURRENCY_EXPONENTS
from ..review.workflow import seed_simulated_conversations, seed_simulated_history
from ..risk import cases as case_builder
from ..risk import policy, rules, scoring
from ..taxonomy import TAXONOMY_VERSION
from .build import build_core, normalise_events
from .validate import overall_status, run_checks, to_frame

LOGGER = logging.getLogger(__name__)

CORE_TABLES_TO_SNAPSHOT = (
    "core.transactions",
    "core.payment_events",
    "core.reconciliation_breaks",
)


@dataclass
class RefreshReport:
    batch_id: str
    status: str
    validation_status: str
    seed: int
    duration_seconds: float
    counts: dict[str, int] = field(default_factory=dict)
    model_metrics: dict[str, float] = field(default_factory=dict)
    review_history: dict[str, int] = field(default_factory=dict)
    failed_checks: list[str] = field(default_factory=list)
    error_message: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "status": self.status,
            "validation_status": self.validation_status,
            "seed": self.seed,
            "duration_seconds": round(self.duration_seconds, 2),
            "counts": self.counts,
            "model_metrics": self.model_metrics,
            "review_history": self.review_history,
            "failed_checks": self.failed_checks,
            "error_message": self.error_message,
        }


def _fx_reference() -> pd.DataFrame:
    return pd.DataFrame([
        {"currency": code, "per_usd": float(rate), "exponent": CURRENCY_EXPONENTS[code]}
        for code, rate in PER_USD.items()
    ])


def run_refresh(
    settings: Settings,
    *,
    seed: int | None = None,
    generate_briefs: bool = True,
    seed_history: bool = True,
    brief_limit: int | None = None,
) -> RefreshReport:
    """Build the whole warehouse from a seed. Idempotent."""
    started = time.perf_counter()
    started_at = datetime.now().replace(microsecond=0)
    as_of = datetime.fromisoformat(settings.as_of_date)
    batch_id = f"BATCH_{started_at.strftime('%Y%m%d%H%M%S')}"
    resolved_seed = settings.random_seed if seed is None else seed

    world = generate(settings, seed=resolved_seed)
    counts: dict[str, int] = {}
    failed: list[str] = []

    with session(settings) as con:
        init_schema(con)
        con.execute("CREATE SCHEMA IF NOT EXISTS marts;")
        previous_rows = int(
            con.execute("SELECT count(*) FROM core.transactions").fetchone()[0]
        ) or None
        for table in CORE_TABLES_TO_SNAPSHOT:
            snapshot_table(con, table)

        # This refresh rebuilds the entire synthetic world from a seed, so the
        # audit trail *of that world* is rebuilt with it. Carrying decisions and
        # AI invocations over from a previous world would attach them to cases
        # that no longer mean the same thing, and would chain-link two unrelated
        # histories together.
        #
        # To be explicit about what this is: in a real deployment an audit log is
        # never truncated. Here the log is a property of the generated dataset,
        # not a record of real events, and `audit.refresh_log` and
        # `audit.validation_results` still accumulate across runs so the run
        # history itself survives.
        for table in ("audit.audit_log", "audit.decisions", "audit.appeals",
                      "audit.ai_invocations", "audit.ai_followups"):
            con.execute(f"DELETE FROM {table}")

        events = normalise_events(world.events, batch_id)
        counts["raw_events"] = replace_table(con, "raw.payment_events", events)
        counts["merchants"] = replace_table(con, "core.merchants", world.merchants)
        counts["wallets"] = replace_table(con, "core.wallets", world.wallets)
        counts["devices"] = replace_table(con, "core.devices", world.devices)
        counts["fx_quotes"] = replace_table(con, "core.fx_quotes", world.fx_quotes)
        replace_table(con, "marts.fx_reference", _fx_reference())

        core = build_core(
            settings, events, world.labels, world.merchants, world.wallets,
            world.fx_quotes, batch_id, now=as_of,
        )
        transactions = core["core.transactions"]
        counts["transactions"] = len(transactions)
        counts["payment_events"] = len(core["core.payment_events"])
        counts["reconciliation_breaks"] = len(core["core.reconciliation_breaks"])

        results = run_checks(
            transactions, core["core.payment_events"], core["core.reconciliation_breaks"],
            as_of=as_of, previous_row_count=previous_rows,
        )
        status = overall_status(results)
        failed = [r.check_name for r in results if not r.passed]
        append_table(con, "audit.validation_results", to_frame(results, batch_id, started_at))

        if status == "fail":
            for table in CORE_TABLES_TO_SNAPSHOT:
                restore_table(con, table)
            report = RefreshReport(
                batch_id=batch_id, status="failed", validation_status=status,
                seed=resolved_seed, duration_seconds=time.perf_counter() - started,
                counts=counts, failed_checks=failed,
                error_message="validation gate failed; core tables rolled back to the previous good version",
            )
            _write_refresh_log(con, report, started_at, "", "")
            return report

        for name, frame in core.items():
            replace_table(con, name, frame)

        # --- risk engine -------------------------------------------------
        context = rules.RuleContext(
            settings=settings,
            transactions=transactions,
            merchants=world.merchants,
            wallets=world.wallets,
            breaks=core["core.reconciliation_breaks"],
            now=as_of,
        )
        signals = rules.evaluate(context)
        counts["signals"] = replace_table(con, "risk.signals", signals)

        features = scoring.build_features(
            transactions, signals, world.merchants, world.wallets,
            core["core.reconciliation_breaks"], as_of,
        )
        model = scoring.train(features, transactions["is_actionable_label"].astype(int), settings)
        scoring.save(model, settings.model_path)
        scores = scoring.score(model, features, as_of)
        counts["model_scores"] = replace_table(con, "risk.model_scores", scores)

        decisions = policy.decide_all(settings, transactions, signals, scores)
        counts["policy_decisions"] = replace_table(con, "risk.policy_decisions", decisions)

        cases = case_builder.build_cases(
            settings, decisions, transactions, signals, batch_id, rules.RULES_VERSION, as_of,
        )
        counts["cases"] = replace_table(con, "risk.cases", cases)

        log = AuditLog(con)
        log.append(
            actor_role="system", actor_id="pipeline", action="refresh.completed",
            object_type="batch", object_id=batch_id,
            summary=(
                f"{counts['transactions']} transactions, {counts['signals']} signals, "
                f"{counts['cases']} cases (seed {resolved_seed})"
            ),
            payload={
                "seed": resolved_seed,
                "taxonomy_version": TAXONOMY_VERSION,
                "rules_version": rules.RULES_VERSION,
                "model_version": model.version,
                "policy_version": policy.POLICY_VERSION,
                "validation_status": status,
                "counts": counts,
            },
            occurred_at=started_at,
        )

        # --- AI briefs ----------------------------------------------------
        briefs: dict[str, CaseBrief] = {}
        if generate_briefs and not cases.empty:
            briefs = _generate_briefs(
                settings, con, cases, transactions, signals,
                core["core.reconciliation_breaks"], world.merchants, world.wallets, scores,
                limit=brief_limit,
            )
            counts["ai_briefs"] = len(briefs)
            _apply_brief_recommendations(con, briefs)

        # --- simulated review history --------------------------------------
        history = {}
        if seed_history:
            history = seed_simulated_history(
                con, seed=resolved_seed, as_of=as_of,
                brief_lookup=lambda case_id: briefs.get(case_id),
            )
            conversations = seed_simulated_conversations(
                con, settings=settings, seed=resolved_seed, as_of=as_of,
            )
            history.update(conversations)
            counts["followup_turns"] = conversations["turns"]

        run_sql_file(con, settings.sql_dir / "marts.sql")
        counts["audit_entries"] = int(
            con.execute("SELECT count(*) FROM audit.audit_log").fetchone()[0]
        )

        report = RefreshReport(
            batch_id=batch_id,
            status="success",
            validation_status=status,
            seed=resolved_seed,
            duration_seconds=time.perf_counter() - started,
            counts=counts,
            model_metrics=model.metrics,
            review_history=history,
            failed_checks=failed,
        )
        _write_refresh_log(con, report, started_at, rules.RULES_VERSION, model.version)
        return report


def _generate_briefs(
    settings: Settings,
    con,
    cases: pd.DataFrame,
    transactions: pd.DataFrame,
    signals: pd.DataFrame,
    breaks: pd.DataFrame,
    merchants: pd.DataFrame,
    wallets: pd.DataFrame,
    scores: pd.DataFrame,
    limit: int | None = None,
) -> dict[str, CaseBrief]:
    """Pre-compute a brief per case.

    In the product a brief is requested by an analyst opening a case. Computing
    them up front is a demo convenience so every case detail page has one to
    show, and so the evaluation harness has a full population to score. The
    Case Detail page can still regenerate on demand.
    """
    provider = build_provider(settings)
    rows = cases.to_dict("records")
    if limit:
        rows = rows[:limit]

    # Index once. Filtering a 6,000-row frame per case turns a two-second job
    # into a two-minute one, and the shape of the packet does not change.
    txn_by_id = {str(r["transaction_id"]): r for r in transactions.to_dict("records")}
    merchant_by_id = {str(r["merchant_id"]): r for r in merchants.to_dict("records")}
    wallet_by_id = {str(r["wallet_id"]): r for r in wallets.to_dict("records")}
    score_by_txn = {str(r["transaction_id"]): r for r in scores.to_dict("records")}
    signals_by_txn: dict[str, list[dict]] = {}
    for record in signals.to_dict("records"):
        signals_by_txn.setdefault(str(record["transaction_id"]), []).append(record)
    breaks_by_txn: dict[str, list[dict]] = {}
    for record in breaks.to_dict("records"):
        breaks_by_txn.setdefault(str(record["transaction_id"]), []).append(record)

    briefs: dict[str, CaseBrief] = {}
    for record in rows:
        txn_id = str(record["transaction_id"])
        transaction = txn_by_id.get(txn_id, {})
        packet, allowed, gate = build_case_packet(
            case=record,
            transaction=transaction,
            signals=signals_by_txn.get(txn_id, []),
            breaks=breaks_by_txn.get(txn_id, []),
            merchant=merchant_by_id.get(str(transaction.get("merchant_id", "")), {}),
            wallet=wallet_by_id.get(str(transaction.get("wallet_id", "")), {}),
            model_score=score_by_txn.get(txn_id),
        )
        brief = investigate(
            settings,
            case_id=str(record["case_id"]),
            transaction_id=str(record["transaction_id"]),
            packet=packet,
            allowed_citations=allowed,
            input_gate=gate,
            provider=provider,
            requested_by="pipeline",
        )
        record_invocation(con, brief, packet=packet, requested_by="pipeline")
        briefs[str(record["case_id"])] = brief
    return briefs


def _apply_brief_recommendations(con, briefs: dict[str, CaseBrief]) -> None:
    """Copy the advisory recommendation onto the case, clearly as advice."""
    if not briefs:
        return
    frame = pd.DataFrame([
        {
            "case_id": case_id,
            "ai_recommended_action": brief.recommended_action,
            "ai_confidence": float(brief.confidence),
        }
        for case_id, brief in briefs.items()
    ])
    con.register("_briefs", frame)
    try:
        con.execute(
            """
            UPDATE risk.cases AS c
            SET ai_recommended_action = b.ai_recommended_action,
                ai_confidence = b.ai_confidence
            FROM _briefs AS b
            WHERE c.case_id = b.case_id
            """
        )
    finally:
        con.unregister("_briefs")


def _write_refresh_log(
    con, report: RefreshReport, started_at: datetime, rules_version: str, model_version: str
) -> None:
    con.execute(
        """
        INSERT INTO audit.refresh_log
            (batch_id, started_at, ended_at, duration_seconds, status, rows_ingested,
             rows_core, rows_rejected, cases_opened, validation_status, taxonomy_version,
             rules_version, model_version, seed, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (batch_id) DO NOTHING
        """,
        [
            report.batch_id, started_at, datetime.now().replace(microsecond=0),
            report.duration_seconds, report.status,
            report.counts.get("raw_events", 0), report.counts.get("transactions", 0),
            report.counts.get("quarantined_events", 0), report.counts.get("cases", 0),
            report.validation_status, TAXONOMY_VERSION, rules_version, model_version,
            report.seed, report.error_message,
        ],
    )


def status_snapshot(settings: Settings) -> dict[str, object]:
    """Row counts, the latest refresh and the audit-chain verdict."""
    with session(settings, read_only=True) as con:
        tables = [
            "raw.payment_events", "core.transactions", "core.payment_events",
            "core.merchants", "core.wallets", "core.fx_quotes",
            "core.reconciliation_breaks", "risk.signals", "risk.model_scores",
            "risk.policy_decisions", "risk.cases", "audit.audit_log",
            "audit.decisions", "audit.appeals", "audit.ai_invocations",
        ]
        counts = {}
        for table in tables:
            try:
                counts[table] = int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            except Exception:  # noqa: BLE001 - a missing table means "not built yet"
                counts[table] = 0
        refreshes = read_sql(
            con, "SELECT * FROM audit.refresh_log ORDER BY started_at DESC LIMIT 5"
        )
        chain = AuditLog(con).verify_chain()
    return {
        "counts": counts,
        "refreshes": refreshes.to_dict("records"),
        "audit_chain": {"status": chain.status, "summary": chain.summary()},
    }
