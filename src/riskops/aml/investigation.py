"""Recording what an investigator decided, and refusing to decide for them.

Two constraints are enforced here rather than described in a document, because a
boundary that lives only in prose is a boundary that holds until someone is in a
hurry.

**No disposition asserts a crime.** The vocabulary below has no value meaning
"confirmed laundering", no value meaning "reported", and no value meaning
"frozen". This prototype can say a pattern is present and that a person reviewed
it. It cannot establish that money was laundered, it has no channel to any
authority, and it holds no one's funds. A verb it cannot perform must not appear
in a dropdown, because a dropdown is a claim about what the system does.

**No disposition may be recorded by an AI actor.** `record_disposition` raises on
a non-human actor, in the same way the payments audit log does. The copilot can
draft a note, list what evidence is missing and suggest what to look at next;
the moment a decision is written, a named person is on it.

A reason is mandatory and is length-checked. That is not bureaucracy: the reason
is the only part of a case that a second reviewer, a quality sampler or the
customer themselves can actually argue with.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..audit.log import AuditLog

INVESTIGATION_VERSION = "1.0.0"

# Human roles permitted to record a disposition. `ai_copilot` is deliberately
# absent and `system` may only move a case into a queue, never out of one.
INVESTIGATOR_ROLES = ("aml_investigator", "risk_ops_lead", "admin_auditor")

MINIMUM_REASON_CHARACTERS = 30


@dataclass(frozen=True)
class Disposition:
    key: str
    title: str
    next_state: str
    description: str
    requires_evidence_reference: bool = False


DISPOSITIONS: tuple[Disposition, ...] = (
    Disposition(
        key="close_no_action",
        title="Close — no further action",
        next_state="closed_no_action",
        description=(
            "The pattern is explained by the evidence on file. Closing says the alert was "
            "reviewed and understood, not that the customer was cleared of anything."
        ),
    ),
    Disposition(
        key="continue_monitoring",
        title="Keep under monitoring",
        next_state="monitoring",
        description=(
            "Not explained, not sufficient to escalate. The account stays in scope and the "
            "next alert on it will show this decision."
        ),
    ),
    Disposition(
        key="request_information",
        title="Request information",
        next_state="awaiting_information",
        description=(
            "A specific, named gap has to be filled before the case can be judged. The gap "
            "must be written in the reason so the request can be acted on by someone else."
        ),
        requires_evidence_reference=True,
    ),
    Disposition(
        key="enhanced_review",
        title="Send for enhanced review",
        next_state="investigating",
        description=(
            "Needs deeper work than the queue allows - a longer lookback, related accounts, "
            "or a second reviewer."
        ),
    ),
    Disposition(
        key="escalate",
        title="Escalate to the AML team",
        next_state="escalated",
        description=(
            "Hands the case to the responsible team for a decision this system does not make. "
            "Escalation is a referral inside this prototype; it files nothing with anyone and "
            "reaches no authority."
        ),
        requires_evidence_reference=True,
    ),
)

DISPOSITION_BY_KEY = {d.key: d for d in DISPOSITIONS}

# Asserted by the test suite. Any of these appearing as a disposition, a case
# state or a button label is a claim this system cannot support.
FORBIDDEN_DISPOSITION_WORDS = (
    "confirmed_laundering", "money_laundering_confirmed", "file_sar",
    "sar_filed", "report_to_regulator", "freeze", "freeze_account",
    "block_customer", "blacklist", "seize", "prosecute",
)


class DispositionError(ValueError):
    """A disposition was refused. Never raised for anything a person may do."""


def validate(disposition_key: str, reason: str, actor_role: str) -> Disposition:
    """Check a proposed disposition before anything is written.

    Separated from `record_disposition` so the interface can show the same
    refusal message *before* the button is pressed rather than after.
    """
    if actor_role not in INVESTIGATOR_ROLES:
        raise DispositionError(
            f"role {actor_role!r} may not record an investigation disposition; "
            f"only {', '.join(INVESTIGATOR_ROLES)} may. The copilot can draft a note and "
            "list missing evidence, but a person signs the decision."
        )
    disposition = DISPOSITION_BY_KEY.get(disposition_key)
    if disposition is None:
        raise DispositionError(
            f"{disposition_key!r} is not a disposition this system offers: "
            f"{', '.join(DISPOSITION_BY_KEY)}"
        )
    cleaned = (reason or "").strip()
    if len(cleaned) < MINIMUM_REASON_CHARACTERS:
        raise DispositionError(
            f"a written reason of at least {MINIMUM_REASON_CHARACTERS} characters is required; "
            "the reason is the only part of this case a second reviewer can argue with"
        )
    if disposition.requires_evidence_reference and not _references_evidence(cleaned):
        raise DispositionError(
            f"'{disposition.title}' requires the reason to name what specifically is missing "
            "or what specifically prompted it - reference a transfer, an account, a "
            "typology or a document"
        )
    return disposition


def _references_evidence(reason: str) -> bool:
    """A light check that the reason points at something, not a rhetorical one.

    Deliberately generous: it accepts an id, a typology name or a named document
    type. The goal is to stop "escalating" and "looks suspicious" from being
    complete answers, not to grade prose.
    """
    lowered = reason.lower()
    markers = (
        "trf_", "acct_", "cust_", "alt_", "amlcase_",
        "structuring", "funnel", "circular", "rapid", "profile", "missing",
        "invoice", "contract", "statement", "passport", "registration",
        "beneficial owner", "source of funds", "purpose",
    )
    return any(marker in lowered for marker in markers)


def record_disposition(
    con,
    *,
    case_id: str,
    disposition_key: str,
    reason: str,
    actor_role: str,
    actor_id: str,
    now: datetime,
    notes: str = "",
    evidence_alert_ids: list[str] | None = None,
) -> dict:
    """Write the decision, move the case, and append one audit entry.

    The audit entry is appended through the same hash-chained log the payments
    side uses, so an AML disposition and a payment decision are equally hard to
    edit after the fact.
    """
    disposition = validate(disposition_key, reason, actor_role)
    cleaned = reason.strip()

    con.execute(
        """
        UPDATE aml.cases
           SET case_state = ?,
               disposition = ?,
               disposition_reason = ?,
               disposition_by = ?,
               resolved_at = ?,
               first_actioned_at = COALESCE(first_actioned_at, ?)
         WHERE case_id = ?
        """,
        [disposition.next_state, disposition.key, cleaned, actor_id, now, now, case_id],
    )

    con.execute(
        """
        INSERT INTO aml.investigation_decisions
            (decision_id, case_id, disposition, next_state, reason, notes,
             evidence_alert_ids, actor_role, actor_id, decided_at, investigation_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            f"AMLDEC_{case_id}_{now:%Y%m%d%H%M%S}",
            case_id,
            disposition.key,
            disposition.next_state,
            cleaned,
            notes.strip(),
            "|".join(evidence_alert_ids or []),
            actor_role,
            actor_id,
            now,
            INVESTIGATION_VERSION,
        ],
    )

    AuditLog(con).append(
        actor_role=actor_role,
        actor_id=actor_id,
        action="aml.disposition_recorded",
        object_type="aml_case",
        object_id=case_id,
        summary=f"{disposition.title} — {cleaned[:160]}",
        payload={
            "disposition": disposition.key,
            "next_state": disposition.next_state,
            "reason_length": len(cleaned),
            "evidence_alert_ids": evidence_alert_ids or [],
            "investigation_version": INVESTIGATION_VERSION,
        },
        occurred_at=now,
    )
    return {
        "case_id": case_id,
        "disposition": disposition.key,
        "next_state": disposition.next_state,
        "decided_at": now,
    }


def claim_case(con, *, case_id: str, actor_role: str, actor_id: str, now: datetime) -> None:
    """Mark a case as being worked, and start the SLA clock that measures it.

    `first_actioned_at` is when someone picked the case up; `resolved_at` is when
    they finished. Two clocks rather than one, because a queue measured only by
    closure time punishes an investigator for a case that was correctly left open
    awaiting information.
    """
    if actor_role not in INVESTIGATOR_ROLES:
        raise DispositionError(f"role {actor_role!r} may not take an AML case")
    con.execute(
        """
        UPDATE aml.cases
           SET case_state = CASE WHEN case_state IN ('new', 'queued')
                                 THEN 'investigating' ELSE case_state END,
               first_actioned_at = COALESCE(first_actioned_at, ?)
         WHERE case_id = ?
        """,
        [now, case_id],
    )
    AuditLog(con).append(
        actor_role=actor_role, actor_id=actor_id, action="aml.case_claimed",
        object_type="aml_case", object_id=case_id,
        summary="case opened for investigation", payload={}, occurred_at=now,
    )
