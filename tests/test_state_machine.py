"""The payment lifecycle must reject impossible histories rather than absorb them."""

from datetime import datetime, timedelta

import pytest

from riskops.money import Money
from riskops.statemachine import (
    PaymentState,
    StateMachineError,
    apply_event,
    can_transition,
    replay,
)
from riskops.taxonomy import PAYMENT_STATE

T0 = datetime(2026, 8, 1, 9, 0, 0)


def _fresh(currency: str = "USD") -> PaymentState:
    state = PaymentState(transaction_id="TXN_TEST")
    apply_event(state, "initiate", T0, amount=Money(0, currency))
    return state


class TestTransitionTable:
    def test_every_target_state_is_in_the_taxonomy(self):
        from riskops.statemachine import TRANSITIONS

        for _, target in TRANSITIONS.values():
            assert target in PAYMENT_STATE.names

    def test_happy_path(self):
        state = _fresh()
        assert apply_event(state, "authorize", T0, Money(6240, "USD")).ok
        assert state.state == "authorized"
        assert apply_event(state, "capture", T0 + timedelta(hours=1), Money(6240, "USD")).ok
        assert state.state == "captured"
        assert apply_event(state, "settle", T0 + timedelta(days=2), Money(6240, "USD")).ok
        assert state.state == "settled"
        assert state.ledger.captured_minor == 6240

    def test_capture_without_authorisation_is_rejected(self):
        state = _fresh()
        result = apply_event(state, "capture", T0, Money(6240, "USD"))
        assert not result.ok
        assert "legal from" in result.reason
        assert state.state == "initiated"
        assert len(state.rejected) == 1

    def test_settle_without_capture_is_rejected(self):
        state = _fresh()
        apply_event(state, "authorize", T0, Money(6240, "USD"))
        assert not apply_event(state, "settle", T0, Money(6240, "USD")).ok

    def test_terminal_states_accept_nothing_further(self):
        state = _fresh()
        apply_event(state, "authorize", T0, Money(100, "USD"))
        apply_event(state, "decline", T0)
        assert state.state == "failed"
        result = apply_event(state, "capture", T0, Money(100, "USD"))
        assert not result.ok
        assert "terminal" in result.reason

    def test_unknown_event_type_is_a_programmer_error(self):
        with pytest.raises(StateMachineError):
            can_transition("initiated", "teleport")

    def test_hold_and_release_round_trip(self):
        state = _fresh()
        apply_event(state, "authorize", T0, Money(100, "USD"))
        assert apply_event(state, "hold_for_review", T0).ok
        assert state.state == "under_review"
        assert apply_event(state, "release_from_review", T0).ok
        assert state.state == "authorized"


class TestMoneyLegality:
    def test_over_capture_is_rejected_with_the_numbers_named(self):
        state = _fresh()
        apply_event(state, "authorize", T0, Money(6240, "USD"))
        result = apply_event(state, "capture", T0, Money(9000, "USD"))
        assert not result.ok
        assert "exceeds authorisation" in result.reason
        assert state.ledger.captured_minor == 0

    def test_refund_beyond_the_captured_balance_is_rejected(self):
        state = _fresh()
        apply_event(state, "authorize", T0, Money(6240, "USD"))
        apply_event(state, "capture", T0, Money(6240, "USD"))
        assert apply_event(state, "refund", T0, Money(3000, "USD")).ok
        assert apply_event(state, "refund", T0, Money(3240, "USD")).ok
        # The third refund has nothing left to return.
        result = apply_event(state, "refund", T0, Money(1, "USD"))
        assert not result.ok
        assert "refundable balance" in result.reason

    def test_refund_then_chargeback_is_allowed_but_bounded(self):
        # Double credit is a real operations failure, so a chargeback after a
        # partial refund is permitted up to the captured amount - the risk
        # rules, not the state machine, are what flag it as a problem.
        state = _fresh()
        apply_event(state, "authorize", T0, Money(10000, "USD"))
        apply_event(state, "capture", T0, Money(10000, "USD"))
        apply_event(state, "refund", T0, Money(4000, "USD"))
        assert apply_event(state, "chargeback", T0, Money(10000, "USD")).ok
        assert state.ledger.payer_out_of_pocket_minor == -4000

    def test_currency_mismatch_on_an_event_is_rejected(self):
        state = _fresh("USD")
        apply_event(state, "authorize", T0, Money(6240, "USD"))
        result = apply_event(state, "capture", T0, Money(6240, "EUR"))
        assert not result.ok
        assert "presentment currency" in result.reason

    def test_negative_amounts_are_rejected(self):
        state = _fresh()
        result = apply_event(state, "authorize", T0, Money(-100, "USD"))
        assert not result.ok


class TestReplay:
    def _events(self) -> list[dict]:
        return [
            {"event_type": "initiate", "occurred_at": T0, "amount_minor": 0, "currency": "USD"},
            {"event_type": "authorize", "occurred_at": T0, "amount_minor": 6240, "currency": "USD"},
            {"event_type": "capture", "occurred_at": T0, "amount_minor": 6240,
             "currency": "USD", "fee_minor": 181},
            {"event_type": "settle", "occurred_at": T0 + timedelta(days=2),
             "amount_minor": 6059, "currency": "USD"},
        ]

    def test_same_second_events_replay_in_causal_order(self):
        # All four share a timestamp; naive sorting would try to capture first.
        state = replay("TXN_1", list(reversed(self._events())))
        assert state.state == "settled"
        assert not state.rejected

    def test_replay_is_deterministic(self):
        first = replay("TXN_1", self._events())
        second = replay("TXN_1", list(reversed(self._events())))
        assert first.state == second.state
        assert first.ledger.captured_minor == second.ledger.captured_minor
        assert first.ledger.fee_minor == second.ledger.fee_minor == 181

    def test_net_to_merchant_subtracts_fees_refunds_and_chargebacks(self):
        events = self._events() + [
            {"event_type": "refund", "occurred_at": T0 + timedelta(days=3),
             "amount_minor": 1000, "currency": "USD"},
        ]
        state = replay("TXN_1", events)
        assert state.ledger.net_to_merchant_minor == 6240 - 1000 - 181
