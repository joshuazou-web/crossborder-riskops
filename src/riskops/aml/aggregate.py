"""Turn alerts into cases: deduplicate, then group.

The failure this module exists to prevent is the one that makes real alerting
systems unusable. One rule firing on forty transfers produces forty alerts; six
rules over one account produce six queues to work; and an investigator who has
to reconstruct that they are all the same story does it forty-six times a day
until they stop reading.

So: an alert is not a case. A case is a *subject over a window*, carrying every
alert that belongs to it, and the reason each alert was merged or kept apart is
written down. That last part matters more than it looks - a merge is a judgement
that two things are one story, and if it is wrong the investigator needs to see
the reasoning to overturn it.

Nothing here decides anything about a customer. Aggregation decides what lands
on one screen.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from .typology import SEVERITY_ORDER, TYPOLOGY_BY_ID

AGGREGATION_VERSION = "1.0.0"

# Two alerts of the same typology on the same subject inside this window are the
# same finding observed twice, not two findings.
DEDUP_WINDOW = timedelta(days=7)

# Alerts on one subject within this window belong to one case. Longer than the
# dedup window because a case is a story about an account over a period, and a
# structuring burst on Monday plus a funnel pattern on Friday is one story.
CASE_WINDOW = timedelta(days=30)

CASE_STATES = (
    "new",
    "queued",
    "investigating",
    "awaiting_information",
    "monitoring",
    "escalated",
    "closed_no_action",
)

# Deliberately absent: any state asserting that laundering occurred. This
# system can say a pattern is present and that a person looked at it. It cannot
# say a crime happened, and a state named for that conclusion would invite the
# claim to be made by clicking. `test_aml_boundaries.py` asserts the absence.
FORBIDDEN_STATES = (
    "confirmed_money_laundering", "confirmed_fraud", "laundering", "guilty",
    "sar_filed", "reported_to_regulator", "account_frozen", "blacklisted",
)

OPEN_STATES = ("new", "queued", "investigating", "awaiting_information", "monitoring")
TERMINAL_STATES = ("escalated", "closed_no_action")

ALERT_OUTPUT_COLUMNS = [
    "alert_id", "typology_id", "typology_version", "severity", "subject_type",
    "subject_id", "subject_customer_id", "triggered_at", "window_start", "window_end",
    "transfer_ids", "transfer_count", "entity_ids", "total_usd_minor",
    "feature_values", "threshold_values", "explanation", "evidence_fields",
    "counter_evidence", "dedup_key", "duplicate_of", "duplicate_count",
    "aggregation_reason", "case_id",
]

CASE_COLUMNS = [
    "case_id", "subject_type", "subject_id", "subject_customer_id", "opened_at",
    "window_start", "window_end", "case_state", "alert_count", "typology_count",
    "typologies", "transfer_count", "total_usd_minor", "max_severity",
    "corroborating_typologies", "aggregation_version", "merge_rationale",
    "priority_score", "priority_band", "priority_contributions", "queue_position",
    "within_capacity", "assigned_role", "sla_due_at", "first_actioned_at",
    "resolved_at", "disposition", "disposition_reason", "disposition_by",
]


@dataclass
class AggregationResult:
    alerts: pd.DataFrame
    cases: pd.DataFrame
    duplicates_removed: int

    @property
    def aggregation_rate(self) -> float:
        """Alerts per case. 1.0 means aggregation did nothing."""
        if self.cases.empty:
            return 0.0
        return round(len(self.alerts) / len(self.cases), 4)


def deduplicate(alerts: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Collapse repeats of one finding, keeping the widest observation.

    The survivor is the alert covering the most transfers rather than the first
    or the largest: an investigator wants the fullest version of the pattern,
    and the count of what it absorbed is kept so nothing is silently discarded.
    """
    if alerts.empty:
        frame = alerts.copy()
        for column in ("duplicate_of", "duplicate_count"):
            frame[column] = pd.Series(dtype="object" if column == "duplicate_of" else "int64")
        return frame, 0

    frame = alerts.copy()
    frame["duplicate_of"] = ""
    frame["duplicate_count"] = 0

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, row in frame.iterrows():
        groups[(str(row["typology_id"]), str(row["subject_id"]))].append(index)

    removed = 0
    drop: list[int] = []
    for indices in groups.values():
        if len(indices) == 1:
            continue
        ordered = sorted(indices, key=lambda i: frame.at[i, "window_start"])
        cluster: list[int] = []
        clusters: list[list[int]] = []
        for index in ordered:
            if cluster and (frame.at[index, "window_start"]
                            - frame.at[cluster[0], "window_start"]) > DEDUP_WINDOW:
                clusters.append(cluster)
                cluster = []
            cluster.append(index)
        if cluster:
            clusters.append(cluster)

        for members in clusters:
            if len(members) == 1:
                continue
            keeper = max(members, key=lambda i: int(frame.at[i, "transfer_count"]))
            frame.at[keeper, "duplicate_count"] = len(members) - 1
            for index in members:
                if index == keeper:
                    continue
                frame.at[index, "duplicate_of"] = frame.at[keeper, "alert_id"]
                drop.append(index)
                removed += 1

    return frame.drop(index=drop).reset_index(drop=True), removed


def _case_id(subject_id: str, window_start: datetime) -> str:
    digest = hashlib.sha256(
        f"{subject_id}|{window_start:%Y-%m-%d}".encode()
    ).hexdigest()[:10].upper()
    return f"AMLCASE_{digest}"


def _merge_rationale(subject_id: str, members: pd.DataFrame, kept_apart: str) -> str:
    typologies = sorted({str(t) for t in members["typology_id"]})
    span = (members["window_end"].max() - members["window_start"].min()).days
    parts = [
        f"{len(members)} alert(s) across {len(typologies)} typology(ies) merged on "
        f"account {subject_id}: they share a subject and fall within a {CASE_WINDOW.days}-day "
        f"window (observed span {span} day(s))."
    ]
    if len(typologies) > 1:
        parts.append(
            "Kept together because several independent typologies describing the same account "
            "corroborate each other; splitting them would hide that."
        )
    if kept_apart:
        parts.append(kept_apart)
    return " ".join(parts)


def build_cases(alerts: pd.DataFrame, *, now: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Group deduplicated alerts into cases, one subject-window at a time."""
    if alerts.empty:
        return (
            alerts.assign(aggregation_reason="", case_id=""),
            pd.DataFrame(columns=CASE_COLUMNS),
        )

    working = alerts.copy()
    working["aggregation_reason"] = ""
    working["case_id"] = ""

    cases: list[dict] = []
    by_subject: dict[str, list[int]] = defaultdict(list)
    for index, row in working.iterrows():
        by_subject[str(row["subject_id"])].append(index)

    for subject_id, indices in by_subject.items():
        ordered = sorted(indices, key=lambda i: working.at[i, "window_start"])
        buckets: list[list[int]] = []
        current: list[int] = []
        for index in ordered:
            if current and (working.at[index, "window_start"]
                            - working.at[current[0], "window_start"]) > CASE_WINDOW:
                buckets.append(current)
                current = []
            current.append(index)
        if current:
            buckets.append(current)

        kept_apart = ""
        if len(buckets) > 1:
            kept_apart = (
                f"{len(buckets)} separate cases were opened for this account because the alert "
                f"windows are more than {CASE_WINDOW.days} days apart; treating months-old "
                "activity as part of today's case would misrepresent both."
            )

        for members in buckets:
            frame = working.loc[members]
            window_start = frame["window_start"].min()
            case_id = _case_id(subject_id, window_start)
            typologies = sorted({str(t) for t in frame["typology_id"]})
            rationale = _merge_rationale(subject_id, frame, kept_apart)
            for index in members:
                working.at[index, "case_id"] = case_id
                working.at[index, "aggregation_reason"] = rationale

            transfer_ids: set[str] = set()
            for value in frame["transfer_ids"]:
                transfer_ids |= {t for t in str(value).split("|") if t}

            cases.append({
                "case_id": case_id,
                "subject_type": "account",
                "subject_id": subject_id,
                "subject_customer_id": str(frame["subject_customer_id"].iloc[0]),
                # A case opens when its most recent alert fired, not when this
                # batch happened to run. Using the batch time would make every
                # case exactly as old as every other, which silently zeroes the
                # queue-fairness factor in priority.py for the whole warehouse.
                "opened_at": frame["window_end"].max(),
                "window_start": window_start,
                "window_end": frame["window_end"].max(),
                "case_state": "new",
                "alert_count": len(frame),
                "typology_count": len(typologies),
                "typologies": "|".join(typologies),
                "transfer_count": len(transfer_ids),
                "total_usd_minor": int(frame["total_usd_minor"].sum()),
                "max_severity": max(
                    (str(s) for s in frame["severity"]),
                    key=lambda s: SEVERITY_ORDER.get(s, 0),
                ),
                # Several independent typologies on one subject is the single
                # strongest thing this system can say, because each was computed
                # from a different feature of the data.
                "corroborating_typologies": len(typologies),
                "aggregation_version": AGGREGATION_VERSION,
                "merge_rationale": rationale,
                "priority_score": 0.0,
                "priority_band": "",
                "priority_contributions": "",
                "queue_position": 0,
                "within_capacity": False,
                "assigned_role": "aml_investigator",
                "sla_due_at": now,
                "first_actioned_at": pd.NaT,
                "resolved_at": pd.NaT,
                "disposition": "",
                "disposition_reason": "",
                "disposition_by": "",
            })

    case_frame = pd.DataFrame(cases, columns=CASE_COLUMNS)
    return working[ALERT_OUTPUT_COLUMNS[:len(working.columns)]] if False else working, case_frame


def aggregate(alerts: pd.DataFrame, *, now: datetime) -> AggregationResult:
    deduped, removed = deduplicate(alerts)
    linked, cases = build_cases(deduped, now=now)
    return AggregationResult(alerts=linked, cases=cases, duplicates_removed=removed)


def typology_titles(typology_ids: str) -> list[str]:
    return [
        TYPOLOGY_BY_ID[t].title
        for t in str(typology_ids).split("|")
        if t in TYPOLOGY_BY_ID
    ]


def render_merge_rationale(case: dict, *, language: str = "en") -> str:
    """Rebuild a case's merge rationale in the reader's language.

    Composed from the case's own columns rather than translated from the stored
    sentence. The stored `merge_rationale` stays canonical English because it is
    what an auditor reads; this is what the interface shows. Rebuilding rather
    than translating also means the numbers can never drift away from the row
    they describe.
    """
    alerts = int(case.get("alert_count", 0))
    typologies = int(case.get("typology_count", 0))
    subject = str(case.get("subject_id", ""))
    stored = str(case.get("merge_rationale", ""))
    split = "more than" in stored or "separate cases" in stored

    if language != "zh":
        return stored

    parts = [
        f"账户 {subject} 上的 {alerts} 条预警、{typologies} 类典型学被合并为一个案件:"
        f"它们指向同一主体,且落在同一个 {CASE_WINDOW.days} 天窗口内。"
    ]
    if typologies > 1:
        parts.append(
            "之所以放在一起,是因为多条相互独立的典型学在描述同一个账户、彼此印证;"
            "拆开会把这一点藏起来。"
        )
    if split:
        parts.append(
            f"该账户还有另外的案件:那些预警窗口相隔超过 {CASE_WINDOW.days} 天,"
            "把几个月前的活动算进今天的案件会同时歪曲两者。"
        )
    return "".join(parts)
