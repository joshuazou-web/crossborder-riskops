"""The validation gate.

Runs between `core` and everything downstream. An `error` failure aborts the
refresh and rolls the warehouse back to the previous good tables; a `warning`
is recorded, surfaced on the Data Quality page, and lets the refresh continue.

The distinction is the whole design. "Every check is fatal" means one odd row
takes the dashboard down; "no check is fatal" means corrupt money reaches a
risk decision. So each check declares which it is, and says why in its detail
string rather than in a comment nobody reads at 3am.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

import pandas as pd

from ..money import SUPPORTED_CURRENCIES
from ..taxonomy import PAYMENT_STATE

ERROR = "error"
WARNING = "warning"


@dataclass
class ValidationResult:
    check_name: str
    severity: str
    passed: bool
    observed: float
    threshold: float
    detail: str

    def as_row(self, batch_id: str, checked_at: datetime) -> dict[str, object]:
        row = asdict(self)
        row["batch_id"] = batch_id
        row["checked_at"] = checked_at
        return row


def _rate(numerator: int, denominator: int) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def run_checks(
    transactions: pd.DataFrame,
    events: pd.DataFrame,
    breaks: pd.DataFrame,
    as_of: datetime,
    previous_row_count: int | None = None,
) -> list[ValidationResult]:
    """Validate the core layer. Always returns one result per check."""
    results: list[ValidationResult] = []
    n = len(transactions)

    def add(name: str, severity: str, passed: bool, observed: float,
            threshold: float, detail: str) -> None:
        results.append(ValidationResult(name, severity, passed, float(observed),
                                        float(threshold), detail))

    add("core_not_empty", ERROR, n > 0, n, 1, f"{n} transactions in core")
    if n == 0:
        return results

    # --- identity -------------------------------------------------------
    dup = int(transactions["transaction_id"].duplicated().sum())
    add("transaction_id_unique", ERROR, dup == 0, dup, 0,
        f"{dup} duplicated transaction_id values survived the build")

    dup_events = int(events["raw_event_id"].duplicated().sum())
    add("raw_event_id_unique", ERROR, dup_events == 0, dup_events, 0,
        f"{dup_events} duplicated raw_event_id values")

    # --- taxonomy -------------------------------------------------------
    bad_states = sorted(set(transactions["payment_state"]) - set(PAYMENT_STATE.names))
    add("payment_state_in_taxonomy", ERROR, not bad_states, len(bad_states), 0,
        f"states outside taxonomy v: {bad_states}" if bad_states else "all states are known")

    bad_currency = sorted(
        (set(transactions["presentment_currency"]) | set(transactions["settlement_currency"]))
        - set(SUPPORTED_CURRENCIES)
    )
    add("currencies_supported", ERROR, not bad_currency, len(bad_currency), 0,
        f"unsupported currencies: {bad_currency}" if bad_currency else "all currencies known")

    # --- money invariants ------------------------------------------------
    # These are ERROR because a violation means the ledger arithmetic itself is
    # wrong, not that the data is odd.
    over_capture = int((transactions["captured_minor"] > transactions["authorized_minor"]).sum())
    add("capture_never_exceeds_authorisation", ERROR, over_capture == 0, over_capture, 0,
        f"{over_capture} transactions captured more than they authorised")

    over_refund = int(
        (
            transactions["refunded_minor"]
            > transactions["captured_minor"]
        ).sum()
    )
    add("refund_never_exceeds_capture", ERROR, over_refund == 0, over_refund, 0,
        f"{over_refund} transactions refunded more than they captured")

    negative = int(
        (transactions[["authorized_minor", "captured_minor", "refunded_minor",
                       "charged_back_minor", "fee_minor", "settled_minor"]] < 0).any(axis=1).sum()
    )
    add("no_negative_amounts", ERROR, negative == 0, negative, 0,
        f"{negative} transactions carry a negative money column")

    settled_without_capture = int(
        ((transactions["settled_minor"] > 0) & (transactions["captured_minor"] == 0)).sum()
    )
    add("settlement_requires_capture", ERROR, settled_without_capture == 0,
        settled_without_capture, 0,
        f"{settled_without_capture} transactions settled without a capture")

    # --- completeness ----------------------------------------------------
    missing_device = int((transactions["device_id"].fillna("") == "").sum())
    device_rate = _rate(missing_device, n)
    add("device_fingerprint_coverage", WARNING, device_rate <= 0.10, device_rate, 0.10,
        f"{missing_device} of {n} transactions ({device_rate:.1%}) have no device fingerprint; "
        "these are decidable only after requesting information")

    orphan_merchant = int((transactions["merchant_id"].fillna("") == "").sum())
    add("merchant_id_present", ERROR, orphan_merchant == 0, orphan_merchant, 0,
        f"{orphan_merchant} transactions have no merchant")

    # --- feed health -----------------------------------------------------
    rejected = int(transactions["rejected_event_count"].sum())
    total_events = len(events)
    reject_rate = _rate(rejected, total_events)
    add("rejected_event_rate", WARNING, reject_rate <= 0.05, reject_rate, 0.05,
        f"{rejected} of {total_events} events ({reject_rate:.2%}) were quarantined as illegal "
        "transitions; they were not applied to any ledger")

    future = int((pd.to_datetime(transactions["created_at"]) > pd.Timestamp(as_of)).sum())
    add("no_transactions_after_cutoff", WARNING, future == 0, future, 0,
        f"{future} transactions are dated after the {as_of.date()} reporting cut-off")

    unsettled_share = _rate(
        int((transactions["payment_state"] == "settled").sum()), n
    )
    add("settled_share_plausible", WARNING, 0.40 <= unsettled_share <= 0.98,
        unsettled_share, 0.40,
        f"{unsettled_share:.1%} of transactions reached settled; outside 40-98% suggests the "
        "generator or the state machine changed shape")

    # --- reconciliation ---------------------------------------------------
    break_count = len(breaks)
    break_rate = _rate(break_count, n)
    add("reconciliation_break_rate", WARNING, break_rate <= 0.25, break_rate, 0.25,
        f"{break_count} reconciliation breaks across {n} transactions ({break_rate:.1%})")

    if break_count:
        zero_diff = int((breaks["difference_minor"] == 0).sum())
        # A break with no difference is a bug in the reconciler, not a finding.
        # `quote_expired_at_capture` is exempt: its "difference" is minutes.
        timing = int((breaks["break_type"] == "quote_expired_at_capture").sum())
        offenders = max(zero_diff - timing, 0)
        add("breaks_have_a_difference", ERROR, offenders == 0, offenders, 0,
            f"{offenders} reconciliation breaks report a zero difference")
    else:
        add("breaks_have_a_difference", ERROR, True, 0, 0, "no breaks to check")

    # --- volume stability -------------------------------------------------
    if previous_row_count:
        change = abs(n - previous_row_count) / previous_row_count
        add("volume_change_within_bounds", WARNING, change <= 0.50, change, 0.50,
            f"transaction count moved {change:.1%} against the previous refresh "
            f"({previous_row_count} -> {n})")
    else:
        add("volume_change_within_bounds", WARNING, True, 0, 0.50, "first refresh; no baseline")

    # --- label integrity --------------------------------------------------
    labelled = int(transactions["is_actionable_label"].notna().sum())
    add("every_transaction_is_labelled", ERROR, labelled == n, labelled, n,
        f"{labelled} of {n} transactions carry a ground-truth label; the evaluation "
        "harness cannot score an unlabelled row")

    return results


def overall_status(results: list[ValidationResult]) -> str:
    if any(not r.passed and r.severity == ERROR for r in results):
        return "fail"
    if any(not r.passed for r in results):
        return "warn"
    return "pass"


def to_frame(results: list[ValidationResult], batch_id: str, checked_at: datetime) -> pd.DataFrame:
    return pd.DataFrame([r.as_row(batch_id, checked_at) for r in results])
