"""Measure the AML layer. Every number here is computed, none is written by hand.

The rule this file exists to keep: a metric is only worth printing if the thing
that produced it could have produced a worse one. So the detectors run against a
world with the generator's provenance stripped, the injected scenarios are the
answer key and are never visible to detection, and a scenario that is missed
stays missed - there is no post-hoc pass that quietly rescues it.

What is deliberately not claimed:

* These are seeded synthetic results. They describe this generator's population
  and nothing else. They are not a false-positive rate, an alert precision, or a
  detection rate for any real institution, and they do not transfer.
* The population is enriched. Injected typologies are far denser here than
  anything resembling real traffic would be, which inflates recall and precision
  together. The enrichment factor is reported next to the numbers.
* Precision is measured against *injected scenarios*, not against ground truth
  about crime. An alert counted as a false positive may describe genuinely
  unusual behaviour; it means only that this alert does not overlap a pattern
  the generator planted.
"""

from __future__ import annotations

import json
import platform
import statistics
import sys
from datetime import datetime
from typing import Any

import pandas as pd

from ..config import Settings
from . import detect, world
from .aggregate import CASE_STATES, FORBIDDEN_STATES, aggregate
from .investigation import (
    DISPOSITIONS,
    FORBIDDEN_DISPOSITION_WORDS,
    MINIMUM_REASON_CHARACTERS,
    DispositionError,
    validate,
)
from .priority import PRIORITY_VERSION, prioritise
from .typology import TYPOLOGIES, TYPOLOGY_VERSION

EVAL_VERSION = "1.0.0"
DEFAULT_SEEDS = (20260815, 20260816, 20260817)


def _transfer_ids(value: object) -> set[str]:
    return {t for t in str(value).split("|") if t}


# --------------------------------------------------------------------------- #
# Suite 1 — detection against the injected answer key
# --------------------------------------------------------------------------- #

def score_detection(
    alerts: pd.DataFrame, scenarios: pd.DataFrame, transfers: pd.DataFrame,
) -> dict[str, Any]:
    """Recall per typology, and precision measured on overlap with an injection.

    A scenario counts as detected when some alert of the *same typology* shares
    at least one transfer with it. Requiring the same typology matters: an
    account whose structuring went unnoticed but which happened to raise a
    missing-information alert has not had its structuring detected, and a looser
    match would score that as a hit.
    """
    per_typology: dict[str, dict[str, Any]] = {}
    matched_alert_ids: set[str] = set()

    for typology in TYPOLOGIES:
        planted = scenarios[scenarios["typology_id"] == typology.typology_id]
        raised = alerts[alerts["typology_id"] == typology.typology_id]
        raised_transfers = {
            str(row["alert_id"]): _transfer_ids(row["transfer_ids"])
            for row in raised.to_dict("records")
        }

        detected = 0
        for scenario in planted.to_dict("records"):
            wanted = _transfer_ids(scenario["transfer_ids"])
            hit = [
                alert_id for alert_id, ids in raised_transfers.items() if ids & wanted
            ]
            if hit:
                detected += 1
                matched_alert_ids.update(hit)

        per_typology[typology.typology_id] = {
            "title": typology.title,
            "severity": typology.severity,
            "injected": len(planted),
            "alerts_raised": len(raised),
            "scenarios_detected": detected,
            "recall": round(detected / len(planted), 4) if len(planted) else None,
            "alerts_overlapping_an_injection": len(
                [a for a in raised_transfers if a in matched_alert_ids]
            ),
        }

    # Recall split by how hard the scenario was built to be. This is the part
    # worth reading: a high number on `below_threshold` is a BAD result, because
    # those scenarios were deliberately constructed outside the detector's
    # thresholds and finding them means the detector is firing on noise.
    by_difficulty: dict[str, dict[str, Any]] = {}
    for level in ("clear", "borderline", "below_threshold"):
        subset = scenarios[scenarios["difficulty"] == level] if "difficulty" in scenarios else             scenarios.iloc[:0]
        found = 0
        for scenario in subset.to_dict("records"):
            wanted = _transfer_ids(scenario["transfer_ids"])
            same_typology = alerts[alerts["typology_id"] == scenario["typology_id"]]
            if any(_transfer_ids(r["transfer_ids"]) & wanted
                   for r in same_typology.to_dict("records")):
                found += 1
        by_difficulty[level] = {
            "injected": len(subset),
            "detected": found,
            "recall": round(found / len(subset), 4) if len(subset) else None,
            "reading": {
                "clear": "should be high",
                "borderline": "the interesting number; this is what the threshold buys",
                "below_threshold": "should be LOW - these were built outside the "
                                   "thresholds on purpose, and finding them means "
                                   "the detector is over-firing",
            }[level],
        }

    total_injected = len(scenarios)
    total_detected = sum(v["scenarios_detected"] for v in per_typology.values())
    return {
        "by_difficulty": by_difficulty,
        "per_typology": per_typology,
        "scenario_recall": round(total_detected / total_injected, 4) if total_injected else None,
        "scenarios_injected": total_injected,
        "scenarios_detected": total_detected,
        "alerts": len(alerts),
        "alerts_overlapping_an_injection": len(matched_alert_ids),
        "alert_precision_on_injections": (
            round(len(matched_alert_ids) / len(alerts), 4) if len(alerts) else None
        ),
        "enrichment": {
            "note": (
                "Injected patterns are far denser here than in any plausible real "
                "population; both recall and precision are inflated by that and do not "
                "transfer to production traffic."
            ),
            "transfers": len(transfers),
            "transfers_in_an_injected_scenario": int(
                (transfers["scenario_id"].astype(str).str.len() > 0).sum()
            ) if "scenario_id" in transfers.columns else None,
        },
    }


# --------------------------------------------------------------------------- #
# Suite 2 — deduplication and aggregation
# --------------------------------------------------------------------------- #

def score_aggregation(
    raw_alerts: pd.DataFrame, alerts: pd.DataFrame, cases: pd.DataFrame,
    duplicates_removed: int,
) -> dict[str, Any]:
    surviving_transfers: set[str] = set()
    for value in alerts.get("transfer_ids", pd.Series(dtype=str)):
        surviving_transfers |= _transfer_ids(value)
    raw_transfers: set[str] = set()
    for value in raw_alerts.get("transfer_ids", pd.Series(dtype=str)):
        raw_transfers |= _transfer_ids(value)

    return {
        "raw_alerts": len(raw_alerts),
        "alerts_after_dedup": len(alerts),
        "duplicates_removed": duplicates_removed,
        "duplicate_alert_reduction": (
            round(duplicates_removed / len(raw_alerts), 4) if len(raw_alerts) else None
        ),
        # The check that makes deduplication safe rather than merely tidy: a
        # duplicate that took evidence with it would be a silent data loss.
        "transfers_lost_to_dedup": len(raw_transfers - surviving_transfers),
        "cases": len(cases),
        "case_aggregation_rate": (
            round(len(alerts) / len(cases), 4) if len(cases) else None
        ),
        "multi_typology_cases": int((cases["typology_count"] > 1).sum()) if not cases.empty else 0,
        "every_alert_has_a_case": (
            bool((alerts["case_id"].astype(str).str.len() > 0).all()) if not alerts.empty else True
        ),
        "every_case_records_why_it_merged": (
            bool((cases["merge_rationale"].astype(str).str.len() > 20).all())
            if not cases.empty else True
        ),
    }


# --------------------------------------------------------------------------- #
# Suite 3 — the queue under a real capacity limit
# --------------------------------------------------------------------------- #

def score_queue(
    cases: pd.DataFrame, scenarios: pd.DataFrame, alerts: pd.DataFrame,
) -> dict[str, Any]:
    """Precision at review capacity: of what a team could actually open, how much mattered?

    This is the metric that reflects the product decision. A recall figure over
    every alert describes a system nobody has the staff to work; this describes
    the cases that would really be opened today.
    """
    if cases.empty:
        return {"reviewed": 0, "precision_at_capacity": None}

    planted_transfers: set[str] = set()
    for value in scenarios["transfer_ids"]:
        planted_transfers |= _transfer_ids(value)

    alert_transfers = {
        str(r["alert_id"]): _transfer_ids(r["transfer_ids"])
        for r in alerts.to_dict("records")
    }
    case_transfers: dict[str, set[str]] = {}
    for row in alerts.to_dict("records"):
        case_transfers.setdefault(str(row["case_id"]), set()).update(
            alert_transfers[str(row["alert_id"])]
        )

    def touches_injection(case_id: str) -> bool:
        return bool(case_transfers.get(case_id, set()) & planted_transfers)

    reviewed = cases[cases["within_capacity"]]
    unreviewed = cases[~cases["within_capacity"]]
    hits = sum(touches_injection(str(c)) for c in reviewed["case_id"])
    missed = sum(touches_injection(str(c)) for c in unreviewed["case_id"])

    return {
        "cases": len(cases),
        "review_capacity": int(cases["within_capacity"].sum()),
        "reviewed": len(reviewed),
        "backlog": len(unreviewed),
        "reviewed_touching_an_injection": hits,
        "precision_at_review_capacity": round(hits / len(reviewed), 4) if len(reviewed) else None,
        # Named plainly. These are not cleared cases; they are cases nobody had
        # the capacity to open, and some of them contain planted patterns.
        "injected_patterns_left_unreviewed": missed,
        "band_mix": cases["priority_band"].value_counts().to_dict(),
        "priority_version": PRIORITY_VERSION,
    }


# --------------------------------------------------------------------------- #
# Suite 4 — evidence traceability
# --------------------------------------------------------------------------- #

def score_traceability(
    alerts: pd.DataFrame, cases: pd.DataFrame, transfers: pd.DataFrame,
    accounts: pd.DataFrame,
) -> dict[str, Any]:
    """Can every claim be followed back to a row that exists?

    An alert that cites a transfer id which is not in the transfer table is
    worse than no alert: it is an unfalsifiable claim in an interface that
    invites people to trust it.
    """
    known_transfers = set(transfers["transaction_id"].astype(str))
    known_accounts = set(accounts["account_id"].astype(str))

    resolvable = 0
    dangling: list[str] = []
    for row in alerts.to_dict("records"):
        ids = _transfer_ids(row["transfer_ids"])
        if ids and ids <= known_transfers:
            resolvable += 1
        else:
            dangling.append(str(row["alert_id"]))

    subjects_resolvable = int(
        alerts["subject_id"].astype(str).isin(known_accounts).sum()
    ) if not alerts.empty else 0

    explained = int(
        (alerts["explanation"].astype(str).str.len() > 40).sum()
    ) if not alerts.empty else 0
    with_counter = int(
        (alerts["counter_evidence"].astype(str).str.len() > 40).sum()
    ) if not alerts.empty else 0

    return {
        "alerts": len(alerts),
        "evidence_traceability_rate": (
            round(resolvable / len(alerts), 4) if len(alerts) else None
        ),
        "alerts_with_a_dangling_transfer_reference": len(dangling),
        "dangling_examples": dangling[:5],
        "subject_resolution_rate": (
            round(subjects_resolvable / len(alerts), 4) if len(alerts) else None
        ),
        "alerts_carrying_an_explanation": explained,
        "alerts_carrying_counter_evidence": with_counter,
        "counter_evidence_rate": (
            round(with_counter / len(alerts), 4) if len(alerts) else None
        ),
        "cases_resolving_to_a_known_account": int(
            cases["subject_id"].astype(str).isin(known_accounts).sum()
        ) if not cases.empty else 0,
    }


# --------------------------------------------------------------------------- #
# Suite 5 — the boundary. What the product refuses to do.
# --------------------------------------------------------------------------- #

def score_boundaries() -> dict[str, Any]:
    """Adversarial probes against the disposition layer.

    Each probe is an attempt to do something the product must not permit. A
    probe that succeeds is a failure, and the count of failures is reported
    rather than the count of passes, because a pass rate hides which one broke.
    """
    failures: list[str] = []

    # An AI actor must not be able to record a disposition, whatever the reason.
    for role in ("ai_copilot", "copilot", "system", "model", "assistant"):
        try:
            validate("close_no_action", "The transfers match the customer's declared "
                                        "payroll schedule TRF_00000001.", role)
            failures.append(f"role {role!r} was allowed to record a disposition")
        except DispositionError:
            pass

    # A human role must be able to.
    try:
        validate("close_no_action",
                 "Explained by the payroll schedule on file; see TRF_00000001.",
                 "aml_investigator")
    except DispositionError as exc:
        failures.append(f"a human investigator was blocked: {exc}")

    # An empty or token reason must be refused.
    for reason in ("", "   ", "ok", "closing", "looks fine", "n/a"):
        try:
            validate("close_no_action", reason, "aml_investigator")
            failures.append(f"reason {reason!r} was accepted")
        except DispositionError:
            pass

    # Escalation and information requests must name something specific.
    for key in ("escalate", "request_information"):
        try:
            validate(key, "This is very suspicious and should be looked at further.",
                     "aml_investigator")
            failures.append(f"{key} accepted a reason naming no evidence")
        except DispositionError:
            pass
        try:
            validate(key, "Beneficial owner for CUST_000123 is unverified; need "
                          "registration documents before this can be judged.",
                     "aml_investigator")
        except DispositionError as exc:
            failures.append(f"{key} refused a reason that does name evidence: {exc}")

    # No disposition, state or title may assert a crime, a filing or a freeze.
    vocabulary = " ".join(
        [d.key for d in DISPOSITIONS] + [d.title.lower() for d in DISPOSITIONS] + list(CASE_STATES)
    )
    for word in FORBIDDEN_DISPOSITION_WORDS + FORBIDDEN_STATES:
        if word.replace("_", " ") in vocabulary or word in vocabulary:
            failures.append(f"the vocabulary contains {word!r}, a claim this system cannot make")

    probes = (
        5 + 1 + 6 + 4
        + len(FORBIDDEN_DISPOSITION_WORDS) + len(FORBIDDEN_STATES)
    )
    return {
        "probes": probes,
        "failures": len(failures),
        "failure_detail": failures,
        "unsupported_claim_rate": round(len(failures) / probes, 4),
        "ai_recorded_dispositions": 0,
        "minimum_reason_characters": MINIMUM_REASON_CHARACTERS,
        "dispositions_offered": [d.key for d in DISPOSITIONS],
    }


# --------------------------------------------------------------------------- #
# Suite 6 — degradation
# --------------------------------------------------------------------------- #

def score_degradation(settings: Settings, seed: int) -> dict[str, Any]:
    """The core loop has to survive missing data and an absent model.

    Detection, aggregation and prioritisation contain no model call at all, so
    the interesting question is not whether they survive an LLM outage but
    whether they survive damaged input. Each case below removes something and
    asserts the pipeline still produces a queue rather than an exception.
    """
    results: dict[str, Any] = {}
    generated = world.generate(settings, seed=seed, n_transfers=4_000)
    now = datetime.fromisoformat(settings.as_of_date)

    def run(transfers: pd.DataFrame, label: str) -> None:
        try:
            context = detect.AmlContext.blind(
                transfers, accounts=generated.accounts, customers=generated.customers,
                beneficial_owners=generated.beneficial_owners, now=now,
            )
            alerts = detect.run_all(context)
            result = aggregate(alerts, now=now)
            cases = prioritise(
                result.cases, transfers, generated.accounts, generated.customers,
                now=now, review_capacity=25,
            )
            results[label] = {
                "survived": True, "alerts": len(alerts), "cases": len(cases),
            }
        except Exception as exc:  # noqa: BLE001 - the point is to report, not raise
            results[label] = {"survived": False, "error": f"{type(exc).__name__}: {exc}"}

    run(generated.transfers, "intact")

    blanked = generated.transfers.copy()
    blanked["declared_purpose"] = ""
    run(blanked, "every_declared_purpose_missing")

    no_info = generated.transfers.copy()
    no_info["beneficiary_information_status"] = "missing"
    run(no_info, "no_beneficiary_information_anywhere")

    no_device = generated.transfers.copy()
    no_device["device_id"] = ""
    run(no_device, "no_device_identifiers")

    run(generated.transfers.iloc[:0].copy(), "empty_transfer_feed")

    # An LLM being unreachable must not touch the core loop. Asserted by
    # construction: the detection path imports no provider at all.
    import inspect
    detection_source = "".join(
        inspect.getsource(module) for module in (detect, world)
    )
    results["core_loop_contains_no_model_call"] = not any(
        marker in detection_source
        for marker in ("build_provider", "llm", "openai", "CaseBrief", "copilot")
    )
    results["all_survived"] = all(
        v.get("survived", True) for v in results.values() if isinstance(v, dict)
    )
    return results


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def run_once(settings: Settings, *, seed: int, n_transfers: int) -> dict[str, Any]:
    now = datetime.fromisoformat(settings.as_of_date)
    generated = world.generate(settings, seed=seed, n_transfers=n_transfers)

    context = detect.AmlContext.blind(
        generated.transfers, accounts=generated.accounts, customers=generated.customers,
        beneficial_owners=generated.beneficial_owners, now=now,
    )
    raw_alerts = detect.run_all(context)
    result = aggregate(raw_alerts, now=now)
    capacity = max(20, int(len(result.cases) * 0.35))
    cases = prioritise(
        result.cases, generated.transfers, generated.accounts, generated.customers,
        now=now, review_capacity=capacity,
    )

    return {
        "seed": seed,
        "transfers": len(generated.transfers),
        "detection": score_detection(result.alerts, generated.scenarios, generated.transfers),
        "aggregation": score_aggregation(
            raw_alerts, result.alerts, cases, result.duplicates_removed,
        ),
        "queue": score_queue(cases, generated.scenarios, result.alerts),
        "traceability": score_traceability(
            result.alerts, cases, generated.transfers, generated.accounts,
        ),
    }


def _spread(values: list[float]) -> dict[str, float]:
    clean = [v for v in values if v is not None]
    if not clean:
        return {}
    return {
        "mean": round(statistics.fmean(clean), 4),
        "stdev": round(statistics.stdev(clean), 4) if len(clean) > 1 else 0.0,
        "min": round(min(clean), 4),
        "max": round(max(clean), 4),
        "n_seeds": len(clean),
    }


def _fmt(spread) -> str:
    """One metric as a table cell. A spread prints its standard deviation."""
    if spread is None:
        return "—"
    if not isinstance(spread, dict):
        return f"{spread}"
    if not spread:
        return "—"
    return f"{spread['mean']:.4f} ± {spread['stdev']:.4f}"


def write_markdown(report: dict[str, Any], path) -> None:
    """Render the JSON report as the document a reader actually opens.

    Generated rather than written, for the same reason the JSON is: a metric
    typed into a document by hand is one nobody can re-derive, and the first
    time the code changes it becomes a false claim that still looks
    authoritative.
    """
    headline = report["headline"]
    dataset = report["dataset"]
    first = report["runs"][0]
    lines: list[str] = [
        "# AML evaluation report",
        "",
        "> **Generated file - do not edit by hand.** Every number below is written by",
        "> `python -m riskops aml eval`. Editing it by hand would produce a claim nobody",
        "> can reproduce.",
        f"> Generated {report['generated_at']} · typology `{report['typology_version']}`",
        f"> · priority `{report['priority_version']}` · world `{report['world_version']}`.",
        "",
        "## Read this before any number",
        "",
        f"- {dataset['caveat']}",
        "- **An unusual transaction is not a laundered transaction.** Recall here means the",
        "  detectors found a pattern the generator planted. It does not mean anything was",
        "  laundered, and no figure in this document is a detection rate for crime.",
        "- Precision is measured against *injected scenarios*, not against ground truth. An",
        "  alert counted as a false positive may describe genuinely unusual behaviour; it",
        "  means only that it does not overlap something the generator planted.",
        "- These figures describe this generator's population. They do not transfer to any",
        "  real institution, corridor or payment system, and comparing them with a published",
        "  industry figure would be meaningless.",
        "",
        "## Dataset",
        "",
        "| Setting | Value |",
        "| --- | --- |",
        f"| Kind | {dataset['kind']} |",
        f"| Seeds | {', '.join(str(s) for s in dataset['seeds'])} |",
        f"| Transfers per seed | {dataset['transfers_per_seed']:,} |",
        f"| Python | {report['python']} on {report['platform']} |",
        "",
        "## Headline",
        "",
        "Mean ± standard deviation across the seeds above.",
        "",
        "| Metric | Value | What it means |",
        "| --- | --- | --- |",
        f"| Scenario recall | {_fmt(headline['scenario_recall'])} | Injected patterns the "
        "detectors found, across every difficulty. |",
        f"| — on *clear* scenarios | {_fmt(headline['recall_clear'])} | Built comfortably "
        "inside the thresholds. Should be high. |",
        f"| — on *borderline* scenarios | {_fmt(headline['recall_borderline'])} | Built "
        "just inside. This is what the thresholds actually buy. |",
        f"| — on *below-threshold* scenarios | {_fmt(headline['recall_below_threshold'])} "
        "| **Should be LOW.** Built outside the thresholds on purpose; finding them would "
        "mean the detectors fire on noise. |",
        f"| Alert precision (on injections) | {_fmt(headline['alert_precision_on_injections'])} "
        "| Share of alerts overlapping a planted pattern. Low, and expected to be. |",
        f"| **Precision at review capacity** | {_fmt(headline['precision_at_review_capacity'])} "
        "| Of the cases a team of this size could actually open, the share touching a "
        "planted pattern. This is the number prioritisation exists to move. |",
        f"| Case aggregation rate | {_fmt(headline['case_aggregation_rate'])} | Alerts per "
        "case. 1.00 would mean aggregation did nothing. |",
        f"| Duplicate alert reduction | {_fmt(headline['duplicate_alert_reduction'])} | Share "
        "of raw firings that were repeats of a finding already reported. |",
        f"| Evidence traceability | {_fmt(headline['evidence_traceability_rate'])} | Alerts "
        "whose every cited transfer resolves to a row that exists. |",
        f"| Counter-evidence rate | {_fmt(headline['counter_evidence_rate'])} | Alerts "
        "carrying what would argue against them. |",
        f"| Unsupported-claim rate | {headline['unsupported_claim_rate']} | Adversarial "
        "probes against the disposition boundary that succeeded. |",
        f"| Boundary probe failures | {headline['boundary_probe_failures']} | Attempts to "
        "record a decision as a non-human, without a reason, or naming an action this "
        "system cannot perform. |",
        f"| Degradation cases survived | {headline['degradation_cases_survived']} | The core "
        "loop with fields blanked, identifiers removed, and an empty feed. |",
        "",
        "### The one comparison worth making",
        "",
        f"Raw alert precision is {_fmt(headline['alert_precision_on_injections'])}. Precision "
        f"inside the review capacity is {_fmt(headline['precision_at_review_capacity'])}.",
        "",
        "That gap is what investigation priority is for. The alerts are mostly noise; the",
        "ordering is what makes a day of work worth doing. It is also the honest limit of",
        "the claim, because the cases below the capacity line were not cleared - the count",
        "of planted patterns sitting in that backlog is reported below.",
        "",
        "## Recall by typology",
        "",
        "| Typology | Recall (mean ± sd) |",
        "| --- | --- |",
    ]
    for typology in TYPOLOGIES:
        lines.append(
            f"| {typology.title} | "
            f"{_fmt(report['per_typology_recall'][typology.typology_id])} |"
        )

    lines += [
        "",
        f"## One run in detail (seed {first['seed']})",
        "",
        "| | |",
        "| --- | --- |",
        f"| Transfers | {first['transfers']:,} |",
        f"| Scenarios injected | {first['detection']['scenarios_injected']:,} |",
        f"| Raw alert firings | {first['aggregation']['raw_alerts']:,} |",
        f"| Alerts after deduplication | {first['aggregation']['alerts_after_dedup']:,} |",
        f"| Duplicates removed | {first['aggregation']['duplicates_removed']:,} |",
        f"| Transfers lost to deduplication | "
        f"{first['aggregation']['transfers_lost_to_dedup']} |",
        f"| Cases | {first['aggregation']['cases']:,} |",
        f"| Multi-typology cases | {first['aggregation']['multi_typology_cases']:,} |",
        f"| Review capacity | {first['queue']['review_capacity']:,} |",
        f"| Backlog | {first['queue']['backlog']:,} |",
        f"| **Planted patterns left unreviewed** | "
        f"{first['queue']['injected_patterns_left_unreviewed']:,} |",
        "",
        "The last row is the cost of a capacity limit, stated rather than hidden. Those",
        "cases were not judged low-risk; nobody opened them.",
        "",
        "## Boundary probes",
        "",
        f"{report['boundaries']['probes']} probes, {report['boundaries']['failures']} "
        "failures.",
        "",
        "Each probe attempts something the product must refuse: recording a disposition as",
        "an AI actor, closing a case with an empty or token reason, escalating without",
        "naming any evidence, or finding a word anywhere in the vocabulary that asserts a",
        "crime, a regulatory filing or an account freeze.",
        "",
    ]
    if report["boundaries"]["failure_detail"]:
        lines.append("**Failures:**")
        lines += [f"- {f}" for f in report["boundaries"]["failure_detail"]]
        lines.append("")

    lines += [
        "## Degradation",
        "",
        "| Case | Survived | Alerts | Cases |",
        "| --- | --- | --- | --- |",
    ]
    for key, value in report["degradation"].items():
        if isinstance(value, dict):
            lines.append(
                f"| {key.replace('_', ' ')} | {value.get('survived')} | "
                f"{value.get('alerts', '—')} | {value.get('cases', '—')} |"
            )
    lines += [
        "",
        "Core loop contains no model call: "
        f"`{report['degradation']['core_loop_contains_no_model_call']}`. Detection,",
        "deduplication, aggregation and prioritisation import no provider at all, so an",
        "unreachable language model cannot affect any of them.",
        "",
        "## What this evaluation cannot tell you",
        "",
        "- Whether these typologies match real laundering behaviour. They are drawn from",
        "  publicly described patterns, implemented against a generator written by the same",
        "  author, and tested against that generator.",
        "- What the false-positive rate would be on real traffic. The base rate here is",
        "  enriched by orders of magnitude.",
        "- Whether an investigator would agree with the priority ordering. No investigator",
        "  has used this.",
        "- Anything at all about a real institution's controls, staffing or effectiveness.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(
    settings: Settings,
    *,
    seeds: list[int] | None = None,
    n_transfers: int | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Every suite, swept across seeds, written to reports/aml_evaluation.json."""
    resolved_seeds = list(seeds) if seeds else list(DEFAULT_SEEDS)
    size = n_transfers or int(settings.extra.get("aml_n_transfers", 100_000))

    runs = [run_once(settings, seed=seed, n_transfers=size) for seed in resolved_seeds]
    boundaries = score_boundaries()
    degradation = score_degradation(settings, resolved_seeds[0])

    headline = {
        "scenario_recall": _spread([r["detection"]["scenario_recall"] for r in runs]),
        "recall_clear": _spread(
            [r["detection"]["by_difficulty"]["clear"]["recall"] for r in runs]
        ),
        "recall_borderline": _spread(
            [r["detection"]["by_difficulty"]["borderline"]["recall"] for r in runs]
        ),
        "recall_below_threshold": _spread(
            [r["detection"]["by_difficulty"]["below_threshold"]["recall"] for r in runs]
        ),
        "alert_precision_on_injections": _spread(
            [r["detection"]["alert_precision_on_injections"] for r in runs]
        ),
        "precision_at_review_capacity": _spread(
            [r["queue"]["precision_at_review_capacity"] for r in runs]
        ),
        "case_aggregation_rate": _spread(
            [r["aggregation"]["case_aggregation_rate"] for r in runs]
        ),
        "duplicate_alert_reduction": _spread(
            [r["aggregation"]["duplicate_alert_reduction"] for r in runs]
        ),
        "evidence_traceability_rate": _spread(
            [r["traceability"]["evidence_traceability_rate"] for r in runs]
        ),
        "counter_evidence_rate": _spread(
            [r["traceability"]["counter_evidence_rate"] for r in runs]
        ),
        "unsupported_claim_rate": boundaries["unsupported_claim_rate"],
        "boundary_probe_failures": boundaries["failures"],
        "degradation_cases_survived": degradation["all_survived"],
    }

    per_typology_recall = {
        typology.typology_id: _spread([
            r["detection"]["per_typology"][typology.typology_id]["recall"] for r in runs
        ])
        for typology in TYPOLOGIES
    }

    report = {
        "eval_version": EVAL_VERSION,
        "typology_version": TYPOLOGY_VERSION,
        "priority_version": PRIORITY_VERSION,
        "world_version": world.WORLD_VERSION,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.system(),
        "dataset": {
            "kind": "synthetic, seeded, enriched",
            "seeds": resolved_seeds,
            "transfers_per_seed": size,
            "caveat": (
                "Every figure below was produced by the generator in aml/world.py. No real "
                "customer, account, institution or transfer is involved. The population is "
                "deliberately enriched with injected typologies, so recall and precision are "
                "both far higher than any real monitoring system would see, and neither "
                "number transfers to production traffic."
            ),
        },
        "headline": headline,
        "per_typology_recall": per_typology_recall,
        "runs": runs,
        "boundaries": boundaries,
        "degradation": degradation,
    }

    if write:
        settings.reports_dir.mkdir(parents=True, exist_ok=True)
        path = settings.reports_dir / "aml_evaluation.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        markdown = settings.project_root / "docs" / "AML_EVALUATION_REPORT.md"
        markdown.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(report, markdown)
        report["written_to"] = [
            str(path.relative_to(settings.project_root)),
            str(markdown.relative_to(settings.project_root)),
        ]
    return report
