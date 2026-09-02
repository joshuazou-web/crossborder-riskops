"""The generator must be reproducible, and the pipeline must not believe it."""

from datetime import datetime

import pandas as pd
import pytest

from riskops.generator.scenarios import SCENARIO_BY_KEY
from riskops.generator.synth import PER_USD, fx_rate, generate, haversine_km
from riskops.money import CURRENCY_EXPONENTS, Money, convert
from riskops.pipeline.build import build_core, normalise_events
from riskops.pipeline.validate import ERROR, overall_status, run_checks
from riskops.taxonomy import PAYMENT_STATE


@pytest.fixture
def world(small_settings):
    return generate(small_settings)


class TestGenerator:
    def test_same_seed_produces_identical_events(self, small_settings):
        first = generate(small_settings, seed=99)
        second = generate(small_settings, seed=99)
        pd.testing.assert_frame_equal(first.events, second.events)
        pd.testing.assert_frame_equal(first.labels, second.labels)

    def test_different_seeds_produce_different_worlds(self, small_settings):
        first = generate(small_settings, seed=1)
        second = generate(small_settings, seed=2)
        assert not first.events.equals(second.events)

    def test_every_currency_has_a_declared_exponent(self):
        assert set(PER_USD) <= set(CURRENCY_EXPONENTS)

    def test_fx_rates_are_self_consistent(self):
        # USD -> SGD -> USD must return to within a rounding step, or every
        # reconciliation built on these rates is measuring the rate table's bugs.
        forward = fx_rate("USD", "SGD")
        back = fx_rate("SGD", "USD")
        assert abs(float(forward * back) - 1.0) < 1e-6

    def test_distance_is_a_real_distance(self):
        assert haversine_km("SG", "SG") == 0.0
        assert 300 < haversine_km("SG", "MY") < 500
        assert haversine_km("SG", "GB") > 10_000

    def test_every_transaction_carries_a_label_and_a_known_scenario(self, world):
        assert world.labels["is_actionable_label"].notna().all()
        assert set(world.labels["scenario"]) <= set(SCENARIO_BY_KEY)

    def test_both_benign_and_actionable_populations_are_present(self, world):
        share = world.labels["is_actionable_label"].mean()
        assert 0.10 < share < 0.40, f"actionable share {share:.2%} is outside the designed band"

    def test_adversarial_notes_actually_reach_the_data(self, world):
        notes = world.labels["merchant_note"].astype(str)
        assert notes.str.contains("ignore", case=False).any()

    def test_amounts_are_integers_in_minor_units(self, world):
        amounts = world.events["amount_minor"].dropna()
        assert (amounts == amounts.astype(int)).all()


class TestPipeline:
    def _core(self, settings, world):
        events = normalise_events(world.events, "BATCH_TEST")
        return build_core(
            settings, events, world.labels, world.merchants, world.wallets,
            world.fx_quotes, "BATCH_TEST",
            now=datetime.fromisoformat(settings.as_of_date),
        )

    def test_normalisation_collapses_case_and_whitespace(self, world):
        noisy = world.events.head(5).copy()
        noisy["event_type"] = " CAPTURE "
        noisy["currency"] = "usd"
        cleaned = normalise_events(noisy, "B")
        assert set(cleaned["event_type"]) == {"capture"}
        assert set(cleaned["currency"]) == {"USD"}

    def test_redelivered_events_are_dropped_not_double_counted(self, world):
        doubled = pd.concat([world.events, world.events]).reset_index(drop=True)
        cleaned = normalise_events(doubled, "B")
        assert len(cleaned) == len(world.events)

    def test_state_comes_from_replay_not_from_a_status_field(self, small_settings, world):
        core = self._core(small_settings, world)
        transactions = core["core.transactions"]
        assert set(transactions["payment_state"]) <= set(PAYMENT_STATE.names)
        # No transaction may have settled without capturing.
        bad = transactions[
            (transactions["settled_minor"] > 0) & (transactions["captured_minor"] == 0)
        ]
        assert bad.empty

    def test_ledger_invariants_hold_across_the_whole_dataset(self, small_settings, world):
        transactions = self._core(small_settings, world)["core.transactions"]
        assert (transactions["captured_minor"] <= transactions["authorized_minor"]).all()
        assert (transactions["refunded_minor"] <= transactions["captured_minor"]).all()
        money_columns = ["authorized_minor", "captured_minor", "refunded_minor",
                         "charged_back_minor", "fee_minor", "settled_minor"]
        assert (transactions[money_columns] >= 0).all().all()

    def test_illegal_events_are_quarantined_with_a_reason(self, small_settings, world):
        core = self._core(small_settings, world)
        rejected = core["core.payment_events"][~core["core.payment_events"]["accepted"]]
        assert len(rejected) > 0, "the feed noise the generator injects should be caught"
        assert (rejected["reject_reason"].astype(str) != "").all()

    def test_reconciliation_breaks_can_be_reproduced_by_hand(self, small_settings, world):
        core = self._core(small_settings, world)
        breaks = core["core.reconciliation_breaks"]
        fx_breaks = breaks[breaks["break_type"] == "fx_settlement_outside_tolerance"]
        if fx_breaks.empty:
            pytest.skip("no FX breaks in this sample")
        transactions = core["core.transactions"].set_index("transaction_id")
        for record in fx_breaks.head(5).to_dict("records"):
            row = transactions.loc[record["transaction_id"]]
            net = Money(int(row["captured_minor"]) - int(row["fee_minor"]),
                        str(row["presentment_currency"]))
            expected = convert(net, str(row["settlement_currency"]),
                               str(row["quoted_fx_rate"])).target
            assert expected.minor_units == int(record["expected_minor"])
            assert int(row["settled_minor"]) == int(record["observed_minor"])

    def test_a_break_always_carries_a_difference(self, small_settings, world):
        breaks = self._core(small_settings, world)["core.reconciliation_breaks"]
        substantive = breaks[breaks["break_type"] != "quote_expired_at_capture"]
        assert (substantive["difference_minor"] != 0).all()


class TestValidationGate:
    def _results(self, settings, world):
        events = normalise_events(world.events, "B")
        core = build_core(settings, events, world.labels, world.merchants, world.wallets,
                          world.fx_quotes, "B",
                          now=datetime.fromisoformat(settings.as_of_date))
        return run_checks(
            core["core.transactions"], core["core.payment_events"],
            core["core.reconciliation_breaks"],
            as_of=datetime.fromisoformat(settings.as_of_date),
        ), core

    def test_a_clean_build_passes(self, small_settings, world):
        results, _ = self._results(small_settings, world)
        assert overall_status(results) in ("pass", "warn")
        failed_errors = [r for r in results if not r.passed and r.severity == ERROR]
        assert not failed_errors, [r.detail for r in failed_errors]

    def test_every_check_reports_a_detail_a_human_can_act_on(self, small_settings, world):
        results, _ = self._results(small_settings, world)
        for result in results:
            assert result.detail, f"{result.check_name} reports nothing"

    def test_corrupt_money_is_caught_as_an_error_not_a_warning(self, small_settings, world):
        results, core = self._results(small_settings, world)
        transactions = core["core.transactions"].copy()
        transactions.loc[transactions.index[0], "captured_minor"] = (
            int(transactions.iloc[0]["authorized_minor"]) + 5000
        )
        broken = run_checks(
            transactions, core["core.payment_events"], core["core.reconciliation_breaks"],
            as_of=datetime.fromisoformat(small_settings.as_of_date),
        )
        assert overall_status(broken) == "fail"
        offender = next(r for r in broken if r.check_name == "capture_never_exceeds_authorisation")
        assert offender.severity == ERROR and not offender.passed

    def test_an_empty_build_fails_immediately(self, small_settings):
        empty = pd.DataFrame(columns=["transaction_id"])
        results = run_checks(empty, empty, empty,
                             as_of=datetime.fromisoformat(small_settings.as_of_date))
        assert overall_status(results) == "fail"
