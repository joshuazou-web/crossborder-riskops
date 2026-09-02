"""Turning policy decisions into work a person can pick up.

A case is created for every routed decision - `manual_review`,
`request_information` and `auto_hold`. Auto-released transactions get no case;
the policy decision is recorded on the transaction and in the audit log, and
that is the honest record of "nobody looked at this, here is why nobody needed
to".

Priority and SLA come from the risk band, and the owning role comes from the
signal family - a settlement break belongs to payment operations, not to a fraud
analyst, and routing it correctly is most of what makes an ops tool usable.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from ..config import Settings
from ..taxonomy import SEVERITY_ORDER

# Which role owns a case, by the family of its strongest signal.
OWNER_BY_FAMILY: dict[str, str] = {
    "integrity": "payment_ops",
    "duplication": "payment_ops",
    "fx_fee": "payment_ops",
    "velocity": "risk_analyst",
    "geo_device": "risk_analyst",
    "merchant_profile": "risk_analyst",
    "dispute_abuse": "risk_analyst",
    "network_linkage": "risk_analyst",
    "completeness": "customer_support",
    "other": "risk_analyst",
    "none": "risk_analyst",
}

INITIAL_STATE_BY_ACTION: dict[str, str] = {
    "manual_review": "open",
    "auto_hold": "open",
    "request_information": "awaiting_information",
}


def build_cases(
    settings: Settings,
    decisions: pd.DataFrame,
    transactions: pd.DataFrame,
    signals: pd.DataFrame,
    batch_id: str,
    rules_version: str,
    now: datetime,
) -> pd.DataFrame:
    """One case row per routed decision."""
    columns = [
        "case_id", "transaction_id", "opened_at", "case_state", "risk_band", "risk_score",
        "policy_action", "policy_rationale", "primary_reason_family", "signal_count",
        "max_severity", "assigned_role", "sla_due_at", "first_actioned_at", "resolved_at",
        "resolution_action",
        "resolution_reason_code", "handling_minutes", "ai_recommended_action", "ai_confidence",
        "ai_agreed_with_human", "appeal_count", "is_false_positive", "policy_version",
        "rules_version", "refresh_batch_id",
    ]
    if decisions.empty:
        return pd.DataFrame(columns=columns)

    routed = decisions[decisions["policy_action"] != "auto_release"]
    if routed.empty:
        return pd.DataFrame(columns=columns)

    created_by_txn = dict(
        zip(
            transactions["transaction_id"].astype(str),
            pd.to_datetime(transactions["created_at"]),
            strict=True,
        )
    )

    severity_by_txn: dict[str, str] = {}
    if not signals.empty:
        work = signals.copy()
        work["rank"] = work["severity"].map(SEVERITY_ORDER).fillna(0)
        best = work.sort_values("rank", ascending=False).drop_duplicates("transaction_id")
        severity_by_txn = dict(
            zip(best["transaction_id"].astype(str), best["severity"].astype(str), strict=True)
        )

    rows = []
    for record in routed.to_dict("records"):
        txn_id = str(record["transaction_id"])
        band = str(record["risk_band"])
        opened_at = created_by_txn.get(txn_id, now)
        family = str(record["primary_reason_family"])
        rows.append({
            "case_id": f"CASE_{txn_id.replace('TXN_', '')}",
            "transaction_id": txn_id,
            "opened_at": opened_at,
            "case_state": INITIAL_STATE_BY_ACTION[str(record["policy_action"])],
            "risk_band": band,
            "risk_score": float(record["risk_score"]),
            "policy_action": str(record["policy_action"]),
            "policy_rationale": str(record["policy_rationale"]),
            "primary_reason_family": family,
            "signal_count": int(record["signal_count"]),
            "max_severity": severity_by_txn.get(txn_id, str(record["max_severity"])),
            "assigned_role": OWNER_BY_FAMILY.get(family, "risk_analyst"),
            "sla_due_at": pd.Timestamp(opened_at) + timedelta(hours=settings.sla_hours(band)),
            "first_actioned_at": pd.NaT,
            "resolved_at": pd.NaT,
            "resolution_action": "",
            "resolution_reason_code": "",
            "handling_minutes": None,
            "ai_recommended_action": "",
            "ai_confidence": None,
            "ai_agreed_with_human": None,
            "appeal_count": 0,
            "is_false_positive": False,
            "policy_version": str(record.get("policy_version", "")),
            "rules_version": rules_version,
            "refresh_batch_id": batch_id,
        })
    return pd.DataFrame(rows, columns=columns)
