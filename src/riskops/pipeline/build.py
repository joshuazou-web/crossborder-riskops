"""Raw events to the canonical payment model.

Every transaction is rebuilt by *replaying its events through the state machine*
rather than by reading a status field. That is the whole point: the warehouse's
idea of a payment and the events' idea of a payment cannot drift apart, because
there is only one idea.

The second job here is reconciliation. Four money invariants are checked with
exact arithmetic, and each failure becomes a row in `core.reconciliation_breaks`
with the expected value, the observed value and the gap in basis points. The
rule engine reads those rows; it does not recompute the money itself, so a
"break" always means exactly one thing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

import pandas as pd

from ..config import Settings
from ..generator.synth import CROSS_BORDER_SURCHARGE_PCT, FEE_BY_TIER
from ..money import Money, convert
from ..statemachine import replay
from ..taxonomy import PAYMENT_STATE

LOGGER = logging.getLogger(__name__)

PIPELINE_VERSION = "1.0.0"

BREAK_TYPES = (
    "fx_settlement_outside_tolerance",
    "fee_off_schedule",
    "auth_capture_gap",
    "double_credit",
    "quote_expired_at_capture",
)


def _bps(expected: int, observed: int) -> int:
    """Signed difference in basis points of the expected amount."""
    if expected == 0:
        return 0 if observed == 0 else 10_000
    return int(round((observed - expected) * 10_000 / expected))


def normalise_events(raw: pd.DataFrame, batch_id: str) -> pd.DataFrame:
    """Canonicalise the feed before anything is replayed.

    Upstream systems emit `CAPTURED`, `capture` and `Capture`. Three spellings
    are three states in any report, so aliases collapse here, once.
    """
    frame = raw.copy()
    frame["event_type"] = frame["event_type"].astype(str).str.strip().str.lower()
    for column in ("currency", "settlement_currency"):
        frame[column] = frame[column].astype(str).str.strip().str.upper()
    for column in (
        "payer_country", "merchant_country", "wallet_country", "ip_country",
    ):
        frame[column] = frame[column].astype(str).str.strip().str.upper()
    frame["source_batch_id"] = batch_id
    # A replayed delivery of the same event is not a second event.
    before = len(frame)
    frame = frame.drop_duplicates(subset=["raw_event_id"], keep="first")
    if len(frame) != before:
        LOGGER.info("dropped %d re-delivered raw events", before - len(frame))
    return frame.sort_values(["transaction_id", "occurred_at"]).reset_index(drop=True)


def build_core(
    settings: Settings,
    events: pd.DataFrame,
    labels: pd.DataFrame,
    merchants: pd.DataFrame,
    wallets: pd.DataFrame,
    fx_quotes: pd.DataFrame,
    batch_id: str,
    now: datetime | None = None,
) -> dict[str, pd.DataFrame]:
    """Replay every transaction and produce the core tables."""
    detected_at = now or datetime.fromisoformat(settings.as_of_date)

    merchant_by_id = merchants.set_index("merchant_id").to_dict("index")
    wallet_by_id = wallets.set_index("wallet_id").to_dict("index")
    quote_by_id = fx_quotes.set_index("fx_quote_id").to_dict("index") if len(fx_quotes) else {}
    label_by_txn = labels.set_index("transaction_id").to_dict("index")

    transaction_rows: list[dict] = []
    event_rows: list[dict] = []
    break_rows: list[dict] = []

    for txn_id, group in events.groupby("transaction_id", sort=True):
        records = group.to_dict("records")
        state = replay(str(txn_id), records)
        ledger = state.ledger
        first = records[0]
        label = label_by_txn.get(txn_id, {})

        merchant = merchant_by_id.get(str(first["merchant_id"]), {})
        wallet = wallet_by_id.get(str(first["wallet_id"]), {})

        settle_events = [r for r in records if r["event_type"] == "settle"]
        settled_minor = 0
        settlement_currency = str(first.get("settlement_currency") or first["currency"])
        applied_rate = ""
        quote_id = str(first.get("fx_quote_id") or "")
        if settle_events:
            # Only a settle event the state machine accepted may move money.
            accepted_settles = [
                r for r in settle_events
                if any(
                    h["event_type"] == "settle" and h["occurred_at"] == r["occurred_at"]
                    for h in state.history
                )
            ]
            chosen = accepted_settles[0] if accepted_settles else None
            if chosen is not None:
                settled_minor = int(chosen.get("settlement_amount_minor") or 0)
                applied_rate = str(chosen.get("fx_rate_applied") or "")

        capture_events = [r for r in records if r["event_type"] == "capture"]
        capture_at = capture_events[0]["occurred_at"] if capture_events else None
        merchant_note = str(capture_events[0].get("payload_note") or "") if capture_events else ""

        quote = quote_by_id.get(quote_id, {})
        quoted_rate = str(quote.get("rate") or "")
        quoted_at = quote.get("quoted_at")
        expires_at = quote.get("expires_at")

        presentment = ledger.currency if ledger else str(first["currency"])
        row = {
            "transaction_id": str(txn_id),
            "created_at": records[0]["occurred_at"],
            "last_event_at": max(r["occurred_at"] for r in records),
            "payment_state": state.state,
            "wallet_id": str(first["wallet_id"]),
            "merchant_id": str(first["merchant_id"]),
            "device_id": str(first["device_id"] or ""),
            "idempotency_key": str(first["idempotency_key"]),
            "channel": str(first["channel"]),
            "presentment_currency": presentment,
            "authorized_minor": ledger.authorized_minor if ledger else 0,
            "captured_minor": ledger.captured_minor if ledger else 0,
            "refunded_minor": ledger.refunded_minor if ledger else 0,
            "charged_back_minor": ledger.charged_back_minor if ledger else 0,
            "fee_minor": ledger.fee_minor if ledger else 0,
            "settlement_currency": settlement_currency,
            "settled_minor": settled_minor,
            "quoted_fx_rate": quoted_rate,
            "applied_fx_rate": applied_rate,
            "fx_quote_id": quote_id,
            "fx_quoted_at": quoted_at,
            "payer_country": str(first["payer_country"]),
            "merchant_country": str(first["merchant_country"]),
            "wallet_country": str(first["wallet_country"]),
            "ip_country": str(first["ip_country"]),
            "is_cross_border": bool(
                presentment != settlement_currency
                or str(first["wallet_country"]) != str(first["merchant_country"])
            ),
            "scenario": str(first["scenario"]),
            "is_actionable_label": bool(label.get("is_actionable_label", False)),
            "expected_action": str(label.get("expected_action", "release")),
            "label_source": str(label.get("label_source", "generator")),
            "event_count": len(state.history),
            "rejected_event_count": len(state.rejected),
            "merchant_note": merchant_note,
            "refresh_batch_id": batch_id,
        }
        transaction_rows.append(row)

        for record in records:
            history = next(
                (
                    h for h in state.history
                    if h["event_type"] == record["event_type"]
                    and h["occurred_at"] == record["occurred_at"]
                ),
                None,
            )
            rejection = next(
                (
                    r for r in state.rejected
                    if r["event_type"] == record["event_type"]
                    and r["occurred_at"] == record["occurred_at"]
                ),
                None,
            )
            event_rows.append({
                **{k: v for k, v in record.items() if k != "payload_note"},
                "from_state": (history or rejection or {}).get("from_state", ""),
                "to_state": (history or rejection or {}).get("to_state", ""),
                "accepted": history is not None,
                "reject_reason": (rejection or {}).get("reject_reason", ""),
                "merchant_note": str(record.get("payload_note") or ""),
            })

        break_rows.extend(
            _reconcile(
                settings=settings,
                txn_id=str(txn_id),
                presentment=presentment,
                settlement_currency=settlement_currency,
                captured_minor=row["captured_minor"],
                authorized_minor=row["authorized_minor"],
                fee_minor=row["fee_minor"],
                settled_minor=settled_minor,
                refunded_minor=row["refunded_minor"],
                charged_back_minor=row["charged_back_minor"],
                quoted_rate=quoted_rate,
                merchant=merchant,
                wallet=wallet,
                is_cross_border=row["is_cross_border"],
                capture_at=capture_at,
                quote_expires_at=expires_at,
                detected_at=detected_at,
            )
        )
    transactions = pd.DataFrame(transaction_rows)
    if not transactions.empty:
        bad = set(transactions["payment_state"]) - set(PAYMENT_STATE.names)
        if bad:
            raise ValueError(f"replay produced states outside the taxonomy: {sorted(bad)}")

    return {
        "core.transactions": transactions,
        "core.payment_events": pd.DataFrame(event_rows),
        "core.reconciliation_breaks": pd.DataFrame(break_rows),
    }


def _reconcile(
    *,
    settings: Settings,
    txn_id: str,
    presentment: str,
    settlement_currency: str,
    captured_minor: int,
    authorized_minor: int,
    fee_minor: int,
    settled_minor: int,
    refunded_minor: int,
    charged_back_minor: int,
    quoted_rate: str,
    merchant: dict,
    wallet: dict,
    is_cross_border: bool,
    capture_at,
    quote_expires_at,
    detected_at: datetime,
) -> list[dict]:
    """Four money invariants, checked exactly. Each failure is one row."""
    breaks: list[dict] = []

    def add(break_type: str, expected: int, observed: int, currency: str, detail: str) -> None:
        breaks.append({
            "break_id": f"BRK_{txn_id}_{break_type}",
            "transaction_id": txn_id,
            "break_type": break_type,
            "expected_minor": int(expected),
            "observed_minor": int(observed),
            "difference_minor": int(observed - expected),
            "currency": currency,
            "difference_bps": _bps(expected, observed),
            "detected_at": detected_at,
            "detail": detail,
        })

    # 1. Authorisation against capture.
    if authorized_minor > 0 and captured_minor > 0 and captured_minor != authorized_minor:
        gap_bps = abs(_bps(authorized_minor, captured_minor))
        if gap_bps > 500:  # a 5% tip or adjustment is ordinary; more is not
            add(
                "auth_capture_gap", authorized_minor, captured_minor, presentment,
                f"captured amount differs from the authorisation by {gap_bps} bps",
            )

    # 2. Fee against the merchant's schedule.
    if captured_minor > 0 and merchant:
        pct = Decimal(FEE_BY_TIER.get(str(merchant.get("risk_tier", "low")), "1.90"))
        if is_cross_border:
            pct += Decimal(CROSS_BORDER_SURCHARGE_PCT)
        expected_fee, _ = Money(captured_minor, presentment).split_percent(pct)
        if expected_fee.minor_units != fee_minor:
            gap_bps = abs(_bps(expected_fee.minor_units, fee_minor))
            if gap_bps > 100:
                add(
                    "fee_off_schedule", expected_fee.minor_units, fee_minor, presentment,
                    f"fee schedule for a {merchant.get('risk_tier')}-tier merchant on a "
                    f"{'cross-border' if is_cross_border else 'domestic'} corridor is {pct}%",
                )

    # 3. Settlement against the booked quote.
    if settled_minor > 0 and quoted_rate:
        net = Money(captured_minor - fee_minor, presentment)
        expected = convert(net, settlement_currency, quoted_rate).target
        gap_bps = abs(_bps(expected.minor_units, settled_minor))
        if gap_bps > settings.fx_tolerance_bps:
            add(
                "fx_settlement_outside_tolerance", expected.minor_units, settled_minor,
                settlement_currency,
                f"settlement is {gap_bps} bps from {net.format()} converted at the booked "
                f"quote {quoted_rate}; tolerance is {settings.fx_tolerance_bps} bps",
            )

    # 4. Quote validity at capture time.
    if capture_at is not None and quote_expires_at is not None:
        capture_ts = pd.Timestamp(capture_at)
        expiry_ts = pd.Timestamp(quote_expires_at)
        if capture_ts > expiry_ts:
            stale_minutes = int((capture_ts - expiry_ts).total_seconds() // 60)
            add(
                "quote_expired_at_capture", 0, stale_minutes, settlement_currency,
                f"capture happened {stale_minutes} minutes after the FX quote expired",
            )

    # 5. Refund and chargeback on the same payment: the payer is credited twice.
    if refunded_minor > 0 and charged_back_minor > 0:
        add(
            "double_credit", captured_minor, refunded_minor + charged_back_minor, presentment,
            f"refunded {refunded_minor} and charged back {charged_back_minor} minor units "
            f"against a capture of {captured_minor}",
        )

    return breaks
