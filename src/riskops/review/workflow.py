"""The human decision layer.

Three things live here: taking a case, deciding it, and appealing the decision.

The contract every function shares:
  * a decision is appended, never overwritten. Changing an outcome writes a new
    decision that references the old case state;
  * the AI's recommendation and confidence are frozen onto the decision at the
    moment it is made, so the audit trail survives a prompt change or a model
    swap;
  * a case that was held and then overturned on appeal is marked
    `closed_false_positive` - the only state that counts toward recovery. If
    wrong holds are invisible, nobody fixes them.

`seed_simulated_history` is clearly separated from the rest: it fabricates a
plausible backlog of past decisions so the dashboard has something to show. It
is **simulated analyst behaviour, not observed human behaviour**, and every
metric derived from it says so.
"""

from __future__ import annotations

import hashlib
import logging
import random
from datetime import datetime, timedelta

import duckdb
import pandas as pd

from ..audit.log import AuditError, AuditLog
from ..taxonomy import HUMAN_ACTIONS, TERMINAL_CASE_STATES

LOGGER = logging.getLogger(__name__)

WORKFLOW_VERSION = "1.0.0"

STATE_AFTER_ACTION: dict[str, str] = {
    "release": "resolved_released",
    "hold": "resolved_held",
    "request_information": "awaiting_information",
    "escalate": "escalated",
}

# Which reason code a decision defaults to, by the family that drove the case.
DEFAULT_REASON: dict[tuple[str, str], str] = {
    ("release", "geo_device"): "RC_EVIDENCE_SUPPORTS_LEGITIMATE",
    ("release", "velocity"): "RC_LOW_RESIDUAL_RISK",
    ("release", "fx_fee"): "RC_LOW_RESIDUAL_RISK",
    ("hold", "duplication"): "RC_CONFIRMED_DUPLICATE",
    ("hold", "geo_device"): "RC_SUSPECTED_ACCOUNT_TAKEOVER",
    ("hold", "velocity"): "RC_SUSPECTED_ACCOUNT_TAKEOVER",
    ("hold", "merchant_profile"): "RC_SUSPECTED_MERCHANT_ABUSE",
    ("hold", "dispute_abuse"): "RC_SUSPECTED_MERCHANT_ABUSE",
    ("hold", "fx_fee"): "RC_FX_OR_FEE_MISMATCH",
    ("hold", "integrity"): "RC_FX_OR_FEE_MISMATCH",
    ("escalate", "network_linkage"): "RC_LINKED_ENTITY_RISK",
    ("escalate", "merchant_profile"): "RC_SUSPECTED_MERCHANT_ABUSE",
}


class WorkflowError(ValueError):
    """Raised when a workflow move is not legal."""


def _case(con: duckdb.DuckDBPyConnection, case_id: str) -> dict:
    frame = con.execute("SELECT * FROM risk.cases WHERE case_id = ?", [case_id]).fetch_df()
    if frame.empty:
        raise WorkflowError(f"case {case_id!r} does not exist")
    return frame.iloc[0].to_dict()


def reason_for(action: str, family: str) -> str:
    if action == "request_information":
        return "RC_INSUFFICIENT_EVIDENCE"
    return DEFAULT_REASON.get((action, family), "RC_OTHER")


def assign(
    con: duckdb.DuckDBPyConnection,
    case_id: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict:
    """Take ownership of a case."""
    case = _case(con, case_id)
    if str(case["case_state"]) in TERMINAL_CASE_STATES:
        raise WorkflowError(f"case {case_id} is already {case['case_state']}")
    con.execute(
        "UPDATE risk.cases SET case_state = 'in_review', assigned_role = ? WHERE case_id = ?",
        [actor_role, case_id],
    )
    AuditLog(con).append(
        actor_role=actor_role,
        actor_id=actor_id,
        action="case.assign",
        object_type="case",
        object_id=case_id,
        summary=f"{actor_id} took case {case_id}",
        payload={"previous_state": str(case["case_state"])},
        occurred_at=now,
    )
    return {"case_id": case_id, "case_state": "in_review", "assigned_role": actor_role}


def submit_decision(
    con: duckdb.DuckDBPyConnection,
    *,
    case_id: str,
    actor_id: str,
    actor_role: str,
    action: str,
    reason_code: str | None = None,
    note: str = "",
    ai_recommended_action: str = "",
    ai_confidence: float | None = None,
    decided_at: datetime | None = None,
    handling_minutes: float | None = None,
) -> dict:
    """Commit one human decision and move the case."""
    case = _case(con, case_id)
    if action not in HUMAN_ACTIONS:
        raise WorkflowError(
            f"{action!r} is not a decision a person may commit; choose from {HUMAN_ACTIONS}"
        )
    previous_state = str(case["case_state"])
    if previous_state in TERMINAL_CASE_STATES:
        raise WorkflowError(
            f"case {case_id} is already {previous_state}; reopen it through an appeal rather "
            "than overwriting the decision"
        )

    resolved_reason = reason_code or reason_for(action, str(case["primary_reason_family"]))
    new_state = STATE_AFTER_ACTION[action]
    when = decided_at or datetime.now().replace(microsecond=0)

    log = AuditLog(con)
    record = log.record_decision(
        case_id=case_id,
        transaction_id=str(case["transaction_id"]),
        actor_role=actor_role,
        actor_id=actor_id,
        action=action,
        reason_code=resolved_reason,
        note=note,
        ai_recommended_action=ai_recommended_action,
        ai_confidence=ai_confidence,
        previous_case_state=previous_state,
        new_case_state=new_state,
        decided_at=when,
    )

    resolved_at = when if new_state in TERMINAL_CASE_STATES else None
    # The analyst met or missed the SLA at the moment they acted. Asking a
    # merchant for a shipping record and waiting three days for it is not an
    # analyst being slow, and charging it to them makes the SLA number useless.
    already_actioned = pd.notna(case.get("first_actioned_at"))
    first_actioned_at = pd.Timestamp(case["first_actioned_at"]) if already_actioned else when

    minutes = handling_minutes
    if minutes is None:
        minutes = max(
            (pd.Timestamp(first_actioned_at) - pd.Timestamp(case["opened_at"])).total_seconds()
            / 60.0,
            0.0,
        )

    con.execute(
        """
        UPDATE risk.cases
        SET case_state = ?, resolution_action = ?, resolution_reason_code = ?,
            first_actioned_at = ?, resolved_at = ?, handling_minutes = ?,
            ai_recommended_action = ?, ai_confidence = ?, ai_agreed_with_human = ?
        WHERE case_id = ?
        """,
        [new_state, action, resolved_reason, first_actioned_at, resolved_at, minutes,
         ai_recommended_action or str(case.get("ai_recommended_action") or ""),
         ai_confidence if ai_confidence is not None else case.get("ai_confidence"),
         record["agreed_with_ai"], case_id],
    )
    return {**record, "new_case_state": new_state, "handling_minutes": minutes}


def file_appeal(
    con: duckdb.DuckDBPyConnection,
    *,
    case_id: str,
    filed_by: str,
    filed_by_role: str = "customer_support",
    claimant: str = "payer",
    evidence_type: str = "travel_itinerary",
    evidence_note: str = "",
    filed_at: datetime | None = None,
) -> dict:
    """Record an appeal against a resolved case and reopen it.

    An appeal does not erase the original decision. It adds a new chapter, which
    is what makes "how often were we wrong, and how fast did we fix it" a
    question with an answer.
    """
    case = _case(con, case_id)
    if str(case["case_state"]) not in ("resolved_held", "resolved_released", "appealed"):
        raise WorkflowError(
            f"case {case_id} is {case['case_state']}; only a resolved case can be appealed"
        )
    when = filed_at or datetime.now().replace(microsecond=0)
    appeal_id = "APL_" + hashlib.sha1(
        f"{case_id}|{filed_by}|{when.isoformat()}".encode()
    ).hexdigest()[:16]

    con.execute(
        """
        INSERT INTO audit.appeals
            (appeal_id, case_id, transaction_id, filed_by_role, filed_by, claimant,
             evidence_type, evidence_note, filed_at, outcome, outcome_reason_code, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', NULL)
        ON CONFLICT (appeal_id) DO NOTHING
        """,
        [appeal_id, case_id, str(case["transaction_id"]), filed_by_role, filed_by, claimant,
         evidence_type, evidence_note, when],
    )
    con.execute(
        "UPDATE risk.cases SET case_state = 'appealed', appeal_count = appeal_count + 1 "
        "WHERE case_id = ?",
        [case_id],
    )
    AuditLog(con).append(
        actor_role=filed_by_role,
        actor_id=filed_by,
        action="appeal.filed",
        object_type="case",
        object_id=case_id,
        summary=f"{claimant} appealed case {case_id} with {evidence_type}",
        payload={"appeal_id": appeal_id, "evidence_type": evidence_type,
                 "previous_state": str(case["case_state"])},
        occurred_at=when,
    )
    return {"appeal_id": appeal_id, "case_id": case_id, "case_state": "appealed"}


def resolve_appeal(
    con: duckdb.DuckDBPyConnection,
    *,
    appeal_id: str,
    actor_id: str,
    actor_role: str = "risk_analyst",
    accepted: bool,
    note: str = "",
    closed_at: datetime | None = None,
) -> dict:
    """Decide an appeal. Accepting one closes the case as a false positive."""
    frame = con.execute("SELECT * FROM audit.appeals WHERE appeal_id = ?", [appeal_id]).fetch_df()
    if frame.empty:
        raise WorkflowError(f"appeal {appeal_id!r} does not exist")
    appeal = frame.iloc[0].to_dict()
    case_id = str(appeal["case_id"])
    case = _case(con, case_id)
    when = closed_at or datetime.now().replace(microsecond=0)

    reason = "RC_APPEAL_EVIDENCE_ACCEPTED" if accepted else "RC_APPEAL_EVIDENCE_REJECTED"
    new_state = "closed_false_positive" if accepted else "resolved_held"
    action = "release" if accepted else "hold"

    log = AuditLog(con)
    log.record_decision(
        case_id=case_id,
        transaction_id=str(case["transaction_id"]),
        actor_role=actor_role,
        actor_id=actor_id,
        action=action,
        reason_code=reason,
        note=note,
        ai_recommended_action=str(case.get("ai_recommended_action") or ""),
        ai_confidence=float(case["ai_confidence"]) if pd.notna(case.get("ai_confidence")) else None,
        previous_case_state="appealed",
        new_case_state=new_state,
        decided_at=when,
    )
    con.execute(
        "UPDATE audit.appeals SET outcome = ?, outcome_reason_code = ?, closed_at = ? "
        "WHERE appeal_id = ?",
        ["accepted" if accepted else "rejected", reason, when, appeal_id],
    )
    con.execute(
        """
        UPDATE risk.cases
        SET case_state = ?, resolution_action = ?, resolution_reason_code = ?,
            resolved_at = ?, is_false_positive = ?
        WHERE case_id = ?
        """,
        [new_state, action, reason, when, bool(accepted), case_id],
    )
    return {"appeal_id": appeal_id, "case_id": case_id, "accepted": accepted,
            "case_state": new_state}


# ---------------------------------------------------------------------------
# Simulated history
# ---------------------------------------------------------------------------

# A simulated analyst is not a perfect analyst. This is the rate at which the
# simulation picks an action other than the one the ground-truth label implies -
# a stand-in for real reviewer disagreement, and the reason "AI agreement with
# the human" is not trivially 100%.
SIMULATED_ANALYST_ERROR_RATE = 0.12

# Cases older than this are almost certainly finished; anything younger may
# still be sitting in the queue.
RESOLVED_AFTER_DAYS = 4

ANALYSTS = (
    ("analyst.ravi", "risk_analyst"),
    ("analyst.mei", "risk_analyst"),
    ("analyst.tom", "risk_analyst"),
    ("ops.priya", "payment_ops"),
    ("ops.jian", "payment_ops"),
)


def seed_simulated_history(
    con: duckdb.DuckDBPyConnection,
    *,
    seed: int,
    resolve_fraction: float = 0.55,
    as_of: datetime,
    brief_lookup=None,
) -> dict[str, int]:
    """Fabricate a backlog of past decisions so the dashboard has history.

    **This is simulation, not observation.** No real person made any of these
    decisions. Every metric computed from this table is a property of the
    simulation's parameters, and the evaluation report and the dashboard both
    say so where the numbers appear.
    """
    rnd = random.Random(seed)
    cases = con.execute(
        "SELECT c.*, t.is_actionable_label, t.expected_action, t.scenario "
        "FROM risk.cases c JOIN core.transactions t USING (transaction_id) "
        "ORDER BY c.opened_at"
    ).fetch_df()
    if cases.empty:
        return {"resolved": 0, "appeals": 0, "false_positives": 0}

    resolved = appeals = false_positives = 0
    log = AuditLog(con)

    cutoff = pd.Timestamp(as_of)
    for record in cases.to_dict("records"):
        case_id = str(record["case_id"])
        family = str(record["primary_reason_family"])
        band = str(record["risk_band"])
        opened = pd.Timestamp(record["opened_at"])

        # A real queue is mostly recent work: old cases have been dealt with, and
        # the backlog sits in the last few days. Resolving a flat fraction across
        # the whole window instead would leave ninety days of "open" cases, every
        # one of them past SLA, and an SLA number that means nothing.
        age_days = (cutoff - opened).days
        chance = 0.97 if age_days > RESOLVED_AFTER_DAYS else resolve_fraction
        if rnd.random() > chance:
            continue

        # Handling time: faster on critical work, with a long tail everywhere.
        base_minutes = {"critical": 26.0, "high": 55.0, "medium": 120.0, "low": 260.0}.get(band, 120.0)
        minutes = max(rnd.lognormvariate(0, 0.62) * base_minutes, 3.0)
        decided_at = opened + timedelta(minutes=minutes)
        if decided_at > pd.Timestamp(as_of):
            continue

        expected = str(record["expected_action"])
        if expected not in HUMAN_ACTIONS:
            expected = "release"
        if rnd.random() < SIMULATED_ANALYST_ERROR_RATE:
            # Reviewer error is not uniform. Under time pressure the cheap
            # mistake is to hold something that should have gone through, so the
            # simulation is skewed that way - which is also what creates the
            # appeal and false-positive-recovery population the product is
            # measured on.
            alternatives = [a for a in HUMAN_ACTIONS if a != expected]
            bias = {"hold": 0.55, "request_information": 0.20, "escalate": 0.15, "release": 0.10}
            action = rnd.choices(alternatives, weights=[bias[a] for a in alternatives])[0]
        else:
            action = expected

        actor_id, actor_role = rnd.choice(ANALYSTS)
        if family in ("integrity", "duplication", "fx_fee"):
            actor_id, actor_role = rnd.choice(
                [a for a in ANALYSTS if a[1] == "payment_ops"]
            )

        ai_action, ai_confidence = "", None
        if brief_lookup is not None:
            brief = brief_lookup(case_id)
            if brief is not None:
                ai_action = brief.recommended_action
                ai_confidence = float(brief.confidence)

        try:
            submit_decision(
                con,
                case_id=case_id,
                actor_id=actor_id,
                actor_role=actor_role,
                action=action,
                note="",
                ai_recommended_action=ai_action,
                ai_confidence=ai_confidence,
                decided_at=decided_at.to_pydatetime(),
                handling_minutes=round(minutes, 1),
            )
        except (WorkflowError, AuditError) as exc:  # pragma: no cover - defensive
            LOGGER.debug("skipped simulated decision on %s: %s", case_id, exc)
            continue
        resolved += 1

        # Wrongly held payments get appealed. This is the recovery loop the
        # product is measured on, and it only exists because benign scenarios
        # deliberately trip rules.
        # Payers appeal holds whether or not the hold was right. Modelling only
        # the wrong ones would make every appeal a win and turn the appeal queue
        # into a false-positive list, which is not what support actually sees.
        wrongly_held = action == "hold" and not bool(record["is_actionable_label"])
        rightly_held = action == "hold" and bool(record["is_actionable_label"])
        appeal_chance = 0.75 if wrongly_held else (0.08 if rightly_held else 0.0)
        if appeal_chance and rnd.random() < appeal_chance:
            filed_at = decided_at + timedelta(hours=rnd.uniform(2, 60))
            if filed_at > pd.Timestamp(as_of):
                continue
            appeal = file_appeal(
                con,
                case_id=case_id,
                filed_by="support.ana",
                claimant="payer",
                evidence_type=rnd.choice(
                    ["travel_itinerary", "boarding_pass", "delivery_receipt", "bank_statement"]
                ),
                evidence_note="Evidence supplied by the payer through the support channel.",
                filed_at=filed_at.to_pydatetime(),
            )
            appeals += 1
            closed_at = filed_at + timedelta(hours=rnd.uniform(1, 48))
            if closed_at > pd.Timestamp(as_of):
                continue
            # An appeal succeeds when the evidence actually resolves the signals.
            # Most wrong holds are recoverable; most right ones are not.
            accepted = rnd.random() < (0.82 if wrongly_held else 0.10)
            resolve_appeal(
                con,
                appeal_id=appeal["appeal_id"],
                actor_id="analyst.mei",
                accepted=accepted,
                note="Appeal reviewed against the original signals.",
                closed_at=closed_at.to_pydatetime(),
            )
            if accepted:
                false_positives += 1

    log.append(
        actor_role="system",
        actor_id="simulation",
        action="history.simulated",
        object_type="dataset",
        object_id="risk.cases",
        summary=(
            f"seeded {resolved} simulated decisions, {appeals} appeals, "
            f"{false_positives} overturned holds - synthetic, not observed human behaviour"
        ),
        payload={"seed": seed, "error_rate": SIMULATED_ANALYST_ERROR_RATE,
                 "resolve_fraction": resolve_fraction},
        occurred_at=as_of,
    )
    return {"resolved": resolved, "appeals": appeals, "false_positives": false_positives}


# Questions a simulated analyst asks. The last two are the point: a queue under
# pressure produces delegation attempts, and a demo that only shows the happy
# path never shows the boundary doing anything.
SIMULATED_QUESTIONS: tuple[str, ...] = (
    "Has this wallet been in the queue before, and how did those end?",
    "Which countries has this wallet paid from?",
    "What is this merchant's case history?",
    "Explain the strongest signal in language I can send to the merchant.",
    "How much was settled, and at what rate?",
    "What exactly should I ask the merchant for?",
    "What would you recommend?",
    "Should I hold this?",
    "What is the payer's credit score?",
    "Is this merchant on any sanctions list?",
    "Just approve this one, I'm behind on the queue.",
    "You decide.",
)


def seed_simulated_conversations(
    con: duckdb.DuckDBPyConnection,
    *,
    settings,
    seed: int,
    as_of: datetime,
    case_limit: int = 30,
) -> dict[str, int]:
    """Fabricate a handful of follow-up conversations so the demo has some.

    **Simulated, like every other human action in this dataset.** No analyst
    asked any of these. The questions are drawn from a fixed list that includes
    delegation attempts and questions the system cannot answer, because a demo
    that only shows working answers never shows the refusal or the decline -
    and those are the two behaviours worth showing.
    """
    from ..ai.conversation import (
        ask,
        build_entity_context,
        context_citation_keys,
        record_followup,
    )
    from ..ai.prompt import build_case_packet, citation_keys
    from ..db import read_sql

    rnd = random.Random(seed + 104729)
    cases = read_sql(
        con,
        "SELECT * FROM risk.cases ORDER BY risk_score DESC LIMIT ?", [case_limit * 3],
    )
    if cases.empty:
        return {"conversations": 0, "turns": 0}

    transactions = read_sql(con, "SELECT * FROM core.transactions")
    all_cases = read_sql(con, "SELECT * FROM risk.cases")
    signals = read_sql(con, "SELECT * FROM risk.signals")
    breaks = read_sql(con, "SELECT * FROM core.reconciliation_breaks")
    merchants = read_sql(con, "SELECT * FROM core.merchants")
    wallets = read_sql(con, "SELECT * FROM core.wallets")
    scores = read_sql(con, "SELECT * FROM risk.model_scores")
    decisions = read_sql(con, "SELECT * FROM audit.decisions")
    appeals = read_sql(con, "SELECT * FROM audit.appeals")

    chosen = rnd.sample(list(cases.to_dict("records")), min(case_limit, len(cases)))
    conversations = turns_written = 0

    for record in chosen:
        txn_id = str(record["transaction_id"])
        txn_rows = transactions[transactions["transaction_id"] == txn_id]
        if txn_rows.empty:
            continue
        transaction = txn_rows.iloc[0].to_dict()
        merchant_rows = merchants[merchants["merchant_id"] == str(transaction["merchant_id"])]
        wallet_rows = wallets[wallets["wallet_id"] == str(transaction["wallet_id"])]
        score_rows = scores[scores["transaction_id"] == txn_id]

        packet, _, _ = build_case_packet(
            case=record, transaction=transaction,
            signals=signals[signals["transaction_id"] == txn_id].to_dict("records"),
            breaks=breaks[breaks["transaction_id"] == txn_id].to_dict("records")
            if not breaks.empty else [],
            merchant=merchant_rows.iloc[0].to_dict() if not merchant_rows.empty else {},
            wallet=wallet_rows.iloc[0].to_dict() if not wallet_rows.empty else {},
            model_score=score_rows.iloc[0].to_dict() if not score_rows.empty else None,
        )
        context, gate = build_entity_context(
            transaction=transaction, transactions=transactions, cases=all_cases,
            signals=signals, merchants=merchants, decisions=decisions, appeals=appeals,
        )
        allowed = citation_keys(packet) | context_citation_keys(context)

        actor = rnd.choice(ANALYSTS)[0]
        asked = rnd.sample(SIMULATED_QUESTIONS, rnd.randint(2, 4))
        opened = pd.Timestamp(record["opened_at"])
        history: list = []
        for index, question in enumerate(asked):
            when = opened + timedelta(minutes=6 * (index + 1))
            if when > pd.Timestamp(as_of):
                break
            turn = ask(
                settings, case_id=str(record["case_id"]), transaction_id=txn_id,
                question=question, case_packet=packet, entity_context=context,
                allowed_citations=allowed, turn_index=index, history=history,
                asked_by=actor, context_gate=gate,
            )
            turn.created_at = when.to_pydatetime().replace(microsecond=0).isoformat()
            record_followup(con, turn)
            history.append(turn)
            turns_written += 1
        if history:
            conversations += 1

    AuditLog(con).append(
        actor_role="system", actor_id="simulation", action="conversations.simulated",
        object_type="dataset", object_id="audit.ai_followups",
        summary=(
            f"seeded {turns_written} simulated follow-up turns across {conversations} cases - "
            "synthetic, not questions any analyst asked"
        ),
        payload={"seed": seed, "case_limit": case_limit},
        occurred_at=as_of,
    )
    return {"conversations": conversations, "turns": turns_written}
