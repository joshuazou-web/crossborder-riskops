"""The cross-border payment state machine.

A payment is a sequence of events, and the order matters. Settling a payment
that was never captured, refunding twice past the captured amount, or capturing
after a chargeback are all *bugs in the money*, and a system that silently
accepts them produces reconciliation breaks nobody can explain later.

So the transition table is explicit data, every transition is checked, and an
illegal event is rejected with a reason rather than dropped. `apply_event`
returns a result object instead of raising, because the pipeline needs to
quarantine a bad event and carry on, not abort a batch of 40,000.

Deliberately simplified relative to a real scheme: no partial authorisations,
no incremental auth, no representment cycle beyond the first chargeback. What
is here is consistent and testable, which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .money import Money, MoneyError

INITIAL_STATE = "initiated"

# event_type -> (allowed source states, resulting state)
TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "initiate": ((), "initiated"),
    "authorize": (("initiated",), "authorized"),
    "capture": (("authorized", "under_review"), "captured"),
    "settle": (("captured",), "settled"),
    # `refunded` is a legal source for another refund: partial refunds are
    # ordinary, and a merchant refunding twice must stay inside the captured
    # balance rather than be blocked by the state alone.
    "refund": (("captured", "settled", "refunded"), "refunded"),
    "chargeback": (("captured", "settled", "refunded"), "chargeback"),
    "decline": (("initiated", "authorized", "under_review"), "failed"),
    "expire": (("initiated", "authorized"), "failed"),
    "cancel": (("initiated", "authorized"), "failed"),
    "hold_for_review": (("initiated", "authorized", "captured"), "under_review"),
    "release_from_review": (("under_review",), "authorized"),
}

EVENT_TYPES = tuple(TRANSITIONS)

# States from which nothing further may happen.
TERMINAL_STATES = ("failed", "chargeback")

# Events that move money, and the sign of that movement from the payer's view.
MONEY_MOVING_EVENTS = ("capture", "settle", "refund", "chargeback")


class StateMachineError(ValueError):
    """Raised only for programmer error - an unknown event type."""


@dataclass
class TransitionResult:
    ok: bool
    from_state: str
    to_state: str
    event_type: str
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.ok


@dataclass
class PaymentLedger:
    """Running money position of one transaction, in its presentment currency.

    Kept in minor units. `refunded` and `charged_back` are tracked separately
    because a refund followed by a chargeback is double credit - a real
    operations failure this system is specifically built to notice.
    """

    currency: str
    authorized_minor: int = 0
    captured_minor: int = 0
    settled_minor: int = 0
    refunded_minor: int = 0
    charged_back_minor: int = 0
    fee_minor: int = 0

    @property
    def net_to_merchant_minor(self) -> int:
        """What the merchant should end up with, before settlement timing."""
        return (
            self.captured_minor
            - self.refunded_minor
            - self.charged_back_minor
            - self.fee_minor
        )

    @property
    def payer_out_of_pocket_minor(self) -> int:
        return self.captured_minor - self.refunded_minor - self.charged_back_minor

    def as_money(self, field_name: str) -> Money:
        return Money(int(getattr(self, field_name)), self.currency)


@dataclass
class PaymentState:
    """The full replayable state of one transaction."""

    transaction_id: str
    state: str = INITIAL_STATE
    ledger: PaymentLedger | None = None
    history: list[dict[str, object]] = field(default_factory=list)
    rejected: list[dict[str, object]] = field(default_factory=list)

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def can_transition(from_state: str, event_type: str) -> TransitionResult:
    """Pure check: may `event_type` be applied from `from_state`?"""
    if event_type not in TRANSITIONS:
        raise StateMachineError(f"unknown event type {event_type!r}; expected one of {EVENT_TYPES}")
    allowed, target = TRANSITIONS[event_type]

    if event_type == "initiate":
        if from_state not in ("", INITIAL_STATE):
            return TransitionResult(
                False, from_state, target, event_type,
                "a transaction can only be initiated once",
            )
        return TransitionResult(True, from_state, target, event_type)

    if from_state in TERMINAL_STATES:
        return TransitionResult(
            False, from_state, target, event_type,
            f"{from_state} is terminal; no further events are accepted",
        )
    if from_state not in allowed:
        return TransitionResult(
            False, from_state, target, event_type,
            f"{event_type} is legal from {allowed}, not from {from_state!r}",
        )
    return TransitionResult(True, from_state, target, event_type)


def _amount_checks(
    state: PaymentState,
    event_type: str,
    amount: Money | None,
) -> str:
    """Money-level legality, on top of state-level legality. Returns "" if fine."""
    ledger = state.ledger
    if ledger is None or amount is None:
        return ""
    if amount.currency != ledger.currency:
        return (
            f"event currency {amount.currency} does not match the transaction's "
            f"presentment currency {ledger.currency}"
        )
    if amount.minor_units < 0:
        return "event amount must not be negative"

    if event_type == "capture":
        # Over-capture is a real scheme concept but is out of scope here: a
        # capture above the authorisation is an integrity defect the rules must
        # see, so it is rejected at the ledger and surfaced, not absorbed.
        if amount.minor_units > ledger.authorized_minor:
            return (
                f"capture of {amount.minor_units} exceeds authorisation of "
                f"{ledger.authorized_minor} minor units"
            )
    if event_type == "refund":
        outstanding = ledger.captured_minor - ledger.refunded_minor - ledger.charged_back_minor
        if amount.minor_units > outstanding:
            return (
                f"refund of {amount.minor_units} exceeds the refundable balance of "
                f"{outstanding} minor units"
            )
    if event_type == "chargeback":
        outstanding = ledger.captured_minor - ledger.charged_back_minor
        if amount.minor_units > outstanding:
            return (
                f"chargeback of {amount.minor_units} exceeds the captured balance of "
                f"{outstanding} minor units"
            )
    if event_type == "settle":
        if ledger.captured_minor == 0:
            return "nothing has been captured, so nothing can settle"
    return ""


def apply_event(
    state: PaymentState,
    event_type: str,
    occurred_at: datetime,
    amount: Money | None = None,
    fee: Money | None = None,
    metadata: dict[str, object] | None = None,
) -> TransitionResult:
    """Apply one event, mutating `state`. Never raises on bad data.

    An illegal event is appended to `state.rejected` with its reason, so the
    pipeline can quarantine it and the Data Quality page can count it.
    """
    if event_type not in TRANSITIONS:
        raise StateMachineError(f"unknown event type {event_type!r}")

    if event_type == "initiate" and state.ledger is None and amount is not None:
        state.ledger = PaymentLedger(currency=amount.currency)

    verdict = can_transition(state.state, event_type)
    if verdict.ok:
        problem = _amount_checks(state, event_type, amount)
        if problem:
            verdict = TransitionResult(False, state.state, verdict.to_state, event_type, problem)

    record = {
        "transaction_id": state.transaction_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "from_state": state.state,
        "to_state": verdict.to_state if verdict.ok else state.state,
        "amount_minor": amount.minor_units if amount else None,
        "currency": amount.currency if amount else None,
        "fee_minor": fee.minor_units if fee else None,
        "accepted": verdict.ok,
        "reject_reason": verdict.reason,
        "metadata": metadata or {},
    }

    if not verdict.ok:
        state.rejected.append(record)
        return verdict

    ledger = state.ledger
    if ledger is not None and amount is not None:
        if event_type == "authorize":
            ledger.authorized_minor += amount.minor_units
        elif event_type == "capture":
            ledger.captured_minor += amount.minor_units
        elif event_type == "settle":
            ledger.settled_minor += amount.minor_units
        elif event_type == "refund":
            ledger.refunded_minor += amount.minor_units
        elif event_type == "chargeback":
            ledger.charged_back_minor += amount.minor_units
    if ledger is not None and fee is not None:
        if fee.currency != ledger.currency:
            raise MoneyError(
                f"fee currency {fee.currency} does not match ledger currency {ledger.currency}"
            )
        ledger.fee_minor += fee.minor_units

    state.state = verdict.to_state
    state.history.append(record)
    return verdict


def replay(
    transaction_id: str,
    events: list[dict[str, object]],
) -> PaymentState:
    """Rebuild a transaction's state from its raw events, in order.

    This is the function the pipeline uses and the function the tests assert
    against, so "what the warehouse says" and "what the events mean" cannot
    drift apart.
    """
    state = PaymentState(transaction_id=transaction_id)
    ordered = sorted(events, key=lambda e: (e["occurred_at"], _event_rank(str(e["event_type"]))))
    for event in ordered:
        currency = event.get("currency")
        amount = _optional_money(event.get("amount_minor"), currency)
        fee = _optional_money(event.get("fee_minor"), currency)
        apply_event(
            state,
            str(event["event_type"]),
            event["occurred_at"],  # type: ignore[arg-type]
            amount=amount,
            fee=fee,
            metadata=event.get("metadata"),  # type: ignore[arg-type]
        )
    return state


_RANK = {name: index for index, name in enumerate(
    ["initiate", "authorize", "hold_for_review", "release_from_review",
     "capture", "settle", "refund", "chargeback", "decline", "expire", "cancel"]
)}


def _optional_money(value: object, currency: object) -> Money | None:
    """Build Money from a possibly-missing feed value.

    A pandas frame turns a missing integer into `NaN`, which is a float, and a
    float is exactly what this codebase refuses to treat as money. So absence is
    normalised to `None` here rather than allowed to become 0 by accident - a
    fee that "is not present" and a fee that "is zero" are different facts.
    """
    if value is None or currency is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    try:
        return Money(int(value), str(currency))
    except (TypeError, ValueError):
        return None


def _event_rank(event_type: str) -> int:
    """Tie-break events sharing a timestamp into their causal order.

    Synthetic events land on second resolution, and two events in the same
    second must still replay as authorise-then-capture, never the reverse.
    """
    return _RANK.get(event_type, 99)
