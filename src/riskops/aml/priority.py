"""Investigation priority: which case a person should open next, and why.

This is an ordering, not a verdict. Priority answers "given that we can review
sixty cases today, which sixty?" - it says nothing about whether any of them is
laundering, and a case that never reaches capacity has not been cleared, it has
only not been looked at yet. The queue page says exactly that, because the
difference is the whole ethical content of a capacity model.

A single opaque number would be useless here for a practical reason as well as a
principled one. An investigator who disagrees with the ordering has to be able
to see *which factor* pushed a case up, and a rules analyst tuning the queue has
to be able to see which factor is dominating it. So every factor's contribution
is computed separately, stored on the case, and rendered as a breakdown.

Each factor is normalised to 0..1 and multiplied by a weight. The weights are
declared here, sum to 1.0, and are asserted to do so - a weight table that
silently stopped summing to one would rescale every score and change the queue
without changing any threshold anyone could see.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from .typology import SEVERITY_ORDER

PRIORITY_VERSION = "1.0.0"

WEIGHTS: dict[str, float] = {
    "signal_strength": 0.20,      # how many typologies, and how severe
    "corroboration": 0.18,        # independent typologies agreeing
    "amount": 0.15,               # value at stake
    "velocity": 0.12,             # how fast the money moved
    "network_breadth": 0.12,      # how many counterparties and countries
    "information_gaps": 0.10,     # what we cannot see
    "customer_history": 0.08,     # prior risk level and account age
    "waiting_time": 0.05,         # queue fairness: age must eventually win
}

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "priority weights must sum to 1.0"

# Cut against the score distribution these factors actually produce, measured on
# seed 20260815 at 40,000 transfers: median 0.211, p70 0.256, p90 0.373,
# p99 0.456, maximum 0.521. The cuts sit at roughly the 99th, 90th and 70th
# percentiles.
#
# The ceiling is around 0.52 rather than 1.0 because a case has to be unusual on
# several independent axes at once to score higher, and almost none are: 0.18 of
# the total weight sits on corroboration, which pays nothing at all to a case
# carrying a single typology, and most cases carry a single typology. That is the
# intended behaviour, not a scaling bug - one pattern on one account is exactly
# the thing that should not reach the top of a queue.
#
# These are not universal constants. A different population needs them re-cut,
# and the evaluation report prints the resulting band mix so a reader can see
# whether they still describe the data.
BANDS: tuple[tuple[float, str], ...] = (
    (0.45, "critical"),
    (0.36, "high"),
    (0.25, "medium"),
    (0.0, "low"),
)

FACTOR_LABELS: dict[str, str] = {
    "signal_strength": "Signal strength",
    "corroboration": "Corroborating typologies",
    "amount": "Amount involved",
    "velocity": "Movement speed",
    "network_breadth": "Network breadth",
    "information_gaps": "Missing information",
    "customer_history": "Customer risk history",
    "waiting_time": "Time waiting in queue",
}

# Money is scored on a log scale between these two points: at or below the floor
# the amount factor contributes nothing, at or above the ceiling it contributes
# its full weight.
#
# Linear scaling was wrong here and visibly so. Case totals in this dataset span
# from about 5,000 USD to 7.2m - three orders of magnitude - so a linear ceiling
# set high enough for the largest case scored the median case at 0.06 and made
# the factor almost inert; set low enough for the median, every large case
# saturated and the factor stopped distinguishing them. A log scale treats a
# 10x difference as the same step wherever it happens, which is how people
# actually reason about amounts.
AMOUNT_FLOOR_USD_MINOR = 1_000 * 100
AMOUNT_CEILING_USD_MINOR = 1_000_000 * 100


@dataclass(frozen=True)
class Contribution:
    factor: str
    raw: float          # the normalised 0..1 input
    weight: float
    points: float       # raw * weight, what it added to the score
    detail: str         # the sentence shown to the investigator

    def as_text(self) -> str:
        return f"{self.factor}={self.points:.4f} ({self.detail})"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _signal_strength(case: dict) -> Contribution:
    severity = SEVERITY_ORDER.get(str(case["max_severity"]), 1)
    alerts = int(case["alert_count"])
    raw = _clamp((severity / 4) * 0.65 + min(alerts, 6) / 6 * 0.35)
    return Contribution(
        "signal_strength", raw, WEIGHTS["signal_strength"],
        raw * WEIGHTS["signal_strength"],
        f"{alerts} alert(s), highest severity {case['max_severity']}",
    )


def _corroboration(case: dict) -> Contribution:
    count = int(case["corroborating_typologies"])
    # One typology is the baseline and earns nothing here; the factor exists to
    # reward independent agreement, which is qualitatively different evidence.
    raw = _clamp((count - 1) / 2)
    detail = (
        f"{count} independent typologies describe this account"
        if count > 1 else "a single typology; no independent corroboration"
    )
    return Contribution("corroboration", raw, WEIGHTS["corroboration"],
                        raw * WEIGHTS["corroboration"], detail)


def _amount(case: dict) -> Contribution:
    total = int(case["total_usd_minor"])
    if total <= AMOUNT_FLOOR_USD_MINOR:
        raw = 0.0
    else:
        span = math.log10(AMOUNT_CEILING_USD_MINOR / AMOUNT_FLOOR_USD_MINOR)
        raw = _clamp(math.log10(total / AMOUNT_FLOOR_USD_MINOR) / span)
    return Contribution("amount", raw, WEIGHTS["amount"], raw * WEIGHTS["amount"],
                        f"{total / 100:,.0f} USD across the alerted transfers")


def _velocity(case: dict, transfers: pd.DataFrame) -> Contribution:
    if transfers.empty:
        return Contribution("velocity", 0.0, WEIGHTS["velocity"], 0.0,
                            "no transfers resolved for this case")
    span_hours = max(
        1.0,
        (transfers["timestamp"].max() - transfers["timestamp"].min()).total_seconds() / 3600,
    )
    per_day = len(transfers) / (span_hours / 24)
    raw = _clamp(per_day / 6)
    return Contribution("velocity", raw, WEIGHTS["velocity"], raw * WEIGHTS["velocity"],
                        f"{per_day:.1f} transfers a day over {span_hours / 24:.1f} days")


def _network_breadth(case: dict, transfers: pd.DataFrame) -> Contribution:
    if transfers.empty:
        return Contribution("network_breadth", 0.0, WEIGHTS["network_breadth"], 0.0,
                            "no transfers resolved for this case")
    counterparties = set(transfers["payer_account"]) | set(transfers["beneficiary_account"])
    counterparties.discard(str(case["subject_id"]))
    countries = set(transfers["origin_country"]) | set(transfers["destination_country"])
    raw = _clamp(len(counterparties) / 12 * 0.6 + len(countries) / 5 * 0.4)
    return Contribution(
        "network_breadth", raw, WEIGHTS["network_breadth"], raw * WEIGHTS["network_breadth"],
        f"{len(counterparties)} counterparty account(s) across {len(countries)} country(ies)",
    )


def _information_gaps(case: dict, transfers: pd.DataFrame) -> Contribution:
    if transfers.empty:
        return Contribution("information_gaps", 0.0, WEIGHTS["information_gaps"], 0.0,
                            "no transfers resolved for this case")
    incomplete = transfers["beneficiary_information_status"].isin(("missing", "partial")).sum()
    blank_purpose = (transfers["declared_purpose"].fillna("").str.strip() == "").sum()
    share = (incomplete + blank_purpose) / (2 * len(transfers))
    raw = _clamp(share)
    return Contribution(
        "information_gaps", raw, WEIGHTS["information_gaps"], raw * WEIGHTS["information_gaps"],
        f"{incomplete} transfer(s) with incomplete beneficiary data, "
        f"{blank_purpose} with no declared purpose",
    )


def _customer_history(case: dict, customer: dict) -> Contribution:
    level = str(customer.get("customer_risk_level", "low"))
    base = {"low": 0.15, "medium": 0.5, "high": 0.9}.get(level, 0.15)
    age_days = int(customer.get("account_age_days") or 0)
    # A young account is not suspicious, but it does mean there is less history
    # to judge against, which is a reason to look sooner rather than later.
    young = 0.25 if age_days and age_days < 90 else 0.0
    raw = _clamp(base + young)
    detail = f"onboarding risk level {level}"
    if young:
        detail += f"; account is {age_days} days old, so there is little history to compare to"
    return Contribution("customer_history", raw, WEIGHTS["customer_history"],
                        raw * WEIGHTS["customer_history"], detail)


def _waiting_time(case: dict, now: datetime) -> Contribution:
    opened = case.get("opened_at")
    if not isinstance(opened, datetime):
        hours = 0.0
    else:
        hours = max(0.0, (now - opened).total_seconds() / 3600)
    # Saturates at two weeks. Without this factor a low-scoring case can sit
    # below the capacity line for ever and never be looked at, which is a policy
    # decision nobody made on purpose.
    raw = _clamp(hours / (14 * 24))
    return Contribution("waiting_time", raw, WEIGHTS["waiting_time"],
                        raw * WEIGHTS["waiting_time"],
                        f"waiting {hours / 24:.1f} day(s)")


def band_for(score: float) -> str:
    for floor, name in BANDS:
        if score >= floor:
            return name
    return "low"


def score_case(
    case: dict,
    transfers: pd.DataFrame,
    customer: dict,
    *,
    now: datetime,
) -> tuple[float, list[Contribution]]:
    contributions = [
        _signal_strength(case),
        _corroboration(case),
        _amount(case),
        _velocity(case, transfers),
        _network_breadth(case, transfers),
        _information_gaps(case, transfers),
        _customer_history(case, customer),
        _waiting_time(case, now),
    ]
    score = round(sum(c.points for c in contributions), 6)
    return score, contributions


def prioritise(
    cases: pd.DataFrame,
    transfers: pd.DataFrame,
    accounts: pd.DataFrame,
    customers: pd.DataFrame,
    *,
    now: datetime,
    review_capacity: int,
    sla_hours: dict[str, int] | None = None,
) -> pd.DataFrame:
    """Score, order and cut the queue at the day's review capacity.

    `within_capacity` is the honest part of this function. Cases below the line
    are not cleared and are not low-risk; they are unreviewed, and the number of
    them is reported on the overview as backlog rather than quietly dropped.
    """
    if cases.empty:
        return cases

    sla = sla_hours or {"critical": 8, "high": 24, "medium": 72, "low": 168}
    account_by_id = accounts.set_index("account_id").to_dict("index")
    customer_by_id = customers.set_index("customer_id").to_dict("index")

    transfers_by_id = {}
    if not transfers.empty:
        transfers_by_id = {str(r["transaction_id"]): r for r in transfers.to_dict("records")}

    frame = cases.copy()
    scores: list[float] = []
    bands: list[str] = []
    breakdowns: list[str] = []
    due: list[datetime] = []

    # Which transfers belong to a case, resolved through its alerts.
    case_transfers: dict[str, pd.DataFrame] = {}
    for case_id, group in frame.groupby("case_id"):
        ids: set[str] = set()
        for value in group.get("transfer_ids", pd.Series(dtype=str)).fillna(""):
            ids |= {t for t in str(value).split("|") if t}
        rows = [transfers_by_id[t] for t in ids if t in transfers_by_id]
        case_transfers[str(case_id)] = pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=transfers.columns
        )

    for record in frame.to_dict("records"):
        case_id = str(record["case_id"])
        subject_account = account_by_id.get(str(record["subject_id"]), {})
        customer = dict(customer_by_id.get(str(record["subject_customer_id"]), {}))
        customer["account_age_days"] = subject_account.get("account_age_days", 0)
        score, contributions = score_case(
            record, case_transfers.get(case_id, pd.DataFrame()), customer, now=now,
        )
        band = band_for(score)
        scores.append(score)
        bands.append(band)
        breakdowns.append("; ".join(c.as_text() for c in contributions))
        due.append(now + timedelta(hours=sla.get(band, 168)))

    frame["priority_score"] = scores
    frame["priority_band"] = bands
    frame["priority_contributions"] = breakdowns
    frame["sla_due_at"] = due

    frame = frame.sort_values(
        ["priority_score", "total_usd_minor"], ascending=[False, False]
    ).reset_index(drop=True)
    frame["queue_position"] = range(1, len(frame) + 1)
    frame["within_capacity"] = frame["queue_position"] <= max(0, review_capacity)
    return frame


def parse_contributions(text: str) -> list[dict]:
    """Read a stored breakdown back into rows for the workbench table."""
    rows: list[dict] = []
    for chunk in str(text).split("; "):
        if "=" not in chunk:
            continue
        factor, rest = chunk.split("=", 1)
        points, _, detail = rest.partition(" (")
        try:
            value = float(points)
        except ValueError:
            continue
        rows.append({
            "factor": factor,
            "label": FACTOR_LABELS.get(factor, factor),
            "points": value,
            "weight": WEIGHTS.get(factor, 0.0),
            "detail": detail.rstrip(")"),
        })
    return sorted(rows, key=lambda r: r["points"], reverse=True)
