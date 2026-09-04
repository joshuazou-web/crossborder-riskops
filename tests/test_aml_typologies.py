"""The six typologies: positive, negative and boundary cases for each detector.

Every test here builds its transfers by hand rather than generating a world, so
a failure names one condition rather than "something in the generator changed".
The negative cases matter at least as much as the positive ones: a detector that
fires on everything would pass every positive test in this file.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from riskops.aml import detect
from riskops.aml.typology import (
    CIRCULAR_FLOW,
    FUNNEL_ACCOUNT,
    PROFILE_MISMATCH,
    RAPID_MOVEMENT,
    STRUCTURING,
    TYPOLOGIES,
    render_explanation,
)

NOW = datetime(2026, 8, 31, 12, 0, 0)
START = NOW - timedelta(days=60)


def account(account_id: str, customer_id: str, country: str = "SG",
            currency: str = "USD", age: int = 900) -> dict:
    return {
        "account_id": account_id, "customer_id": customer_id,
        "account_country": country, "currency": currency,
        "opened_at": START - timedelta(days=age), "account_age_days": age,
        "account_type": "personal",
    }


def customer(customer_id: str, *, kind: str = "individual", industry: str = "none",
             risk: str = "low", expected: float = 5_000) -> dict:
    return {
        "customer_id": customer_id, "customer_name": f"Name {customer_id}",
        "customer_type": kind, "home_country": "SG", "industry": industry,
        "customer_risk_level": risk, "onboarded_at": START - timedelta(days=900),
        "expected_monthly_usd": expected,
        "profile_reviewed_at": START - timedelta(days=400),
    }


def transfer(
    transaction_id: str, payer: dict, beneficiary: dict, usd: float, when: datetime,
    *, purpose: str = "family_support", channel: str = "bank_wire",
    info: str = "complete", device: str = "DEV_000001",
) -> dict:
    return {
        "transaction_id": transaction_id, "timestamp": when,
        "payer_account": payer["account_id"], "beneficiary_account": beneficiary["account_id"],
        "payer_customer_id": payer["customer_id"],
        "beneficiary_customer_id": beneficiary["customer_id"],
        "amount_minor": int(usd * 100), "currency": "USD",
        "normalized_amount_usd_minor": int(usd * 100),
        "origin_country": payer["account_country"],
        "destination_country": beneficiary["account_country"],
        "declared_purpose": purpose, "channel": channel,
        "merchant_category": "none", "device_id": device,
        "account_age_days": payer["account_age_days"],
        "customer_risk_level": "low", "beneficiary_information_status": info,
        "is_cross_border": payer["account_country"] != beneficiary["account_country"],
    }


def context(transfers: list[dict], accounts: list[dict], customers: list[dict],
            owners: list[dict] | None = None) -> detect.AmlContext:
    return detect.AmlContext(
        transfers=pd.DataFrame(transfers),
        accounts=pd.DataFrame(accounts),
        customers=pd.DataFrame(customers),
        beneficial_owners=pd.DataFrame(owners or [], columns=[
            "owner_id", "customer_id", "owner_reference", "ownership_pct",
            "verification_status", "recorded_at",
        ]),
        now=NOW,
    )


# --------------------------------------------------------------------------- #
# The guarantee the whole evaluation rests on
# --------------------------------------------------------------------------- #

class TestDetectorsAreBlind:
    """Detection must not be able to read the generator's answer key."""

    def test_context_refuses_provenance_columns(self) -> None:
        payer, beneficiary = account("A1", "C1"), account("A2", "C2")
        rows = [transfer("T1", payer, beneficiary, 100, START)]
        rows[0]["scenario_id"] = "SC_STRUCT_0001"
        rows[0]["scenario_role"] = "leg_0"
        with pytest.raises(ValueError, match="provenance"):
            context(rows, [payer, beneficiary], [customer("C1"), customer("C2")])

    def test_blind_strips_them(self) -> None:
        payer, beneficiary = account("A1", "C1"), account("A2", "C2")
        row = transfer("T1", payer, beneficiary, 100, START)
        row["scenario_id"] = "SC_X"
        row["scenario_role"] = "leg_0"
        ctx = detect.AmlContext.blind(
            pd.DataFrame([row]),
            accounts=pd.DataFrame([payer, beneficiary]),
            customers=pd.DataFrame([customer("C1"), customer("C2")]),
            beneficial_owners=pd.DataFrame(columns=[
                "owner_id", "customer_id", "owner_reference", "ownership_pct",
                "verification_status", "recorded_at",
            ]),
            now=NOW,
        )
        assert "scenario_id" not in ctx.transfers.columns

    def test_no_detector_mentions_the_provenance_columns(self) -> None:
        """A grep, deliberately. The runtime guard above catches a context built
        wrongly; this catches a detector that reached for the column by name."""
        import inspect

        source = inspect.getsource(detect)
        body = source[source.index("def detect_structuring"):]
        for column in detect.PROVENANCE_COLUMNS:
            assert f'"{column}"' not in body, f"a detector reads {column}"


# --------------------------------------------------------------------------- #
# T01 Structuring
# --------------------------------------------------------------------------- #

class TestStructuring:
    def _world(self, amounts: list[float], hours: list[float]):
        payer = account("A1", "C1")
        target = account("A2", "C2")
        rows = [
            transfer(f"T{i}", payer, target, usd, START + timedelta(hours=h))
            for i, (usd, h) in enumerate(zip(amounts, hours, strict=True))
        ]
        return context(rows, [payer, target], [customer("C1"), customer("C2")])

    def test_fires_on_four_banded_transfers_in_a_day(self) -> None:
        alerts = detect.detect_structuring(
            self._world([8_500, 9_100, 9_400, 8_800], [1, 5, 9, 20])
        )
        assert alerts
        assert alerts[0]["typology_id"] == STRUCTURING.typology_id
        assert alerts[0]["subject_id"] == "A1"

    def test_silent_below_the_minimum_count(self) -> None:
        assert not detect.detect_structuring(self._world([8_500, 9_100], [1, 5]))

    def test_silent_when_the_window_is_too_wide(self) -> None:
        """Three banded transfers, but spread past the 72-hour lookback."""
        assert not detect.detect_structuring(
            self._world([8_500, 9_100, 9_400], [0, 100, 200])
        )

    def test_silent_on_amounts_outside_the_band(self) -> None:
        # Well under the band: ordinary small payments.
        assert not detect.detect_structuring(self._world([300, 450, 500, 700], [1, 5, 9, 20]))
        # Over the threshold: these would be reported, not structured.
        assert not detect.detect_structuring(
            self._world([12_000, 15_000, 20_000, 30_000], [1, 5, 9, 20])
        )

    def test_boundary_exactly_at_the_threshold_is_not_below_it(self) -> None:
        threshold = STRUCTURING.thresholds["threshold_usd"]
        assert not detect.detect_structuring(
            self._world([threshold] * 4, [1, 5, 9, 20])
        )

    def test_evidence_names_the_transfers(self) -> None:
        alerts = detect.detect_structuring(
            self._world([8_500, 9_100, 9_400, 8_800], [1, 5, 9, 20])
        )
        ids = set(alerts[0]["transfer_ids"].split("|"))
        assert ids <= {"T0", "T1", "T2", "T3"}
        assert len(ids) >= int(STRUCTURING.thresholds["min_transfers"])


# --------------------------------------------------------------------------- #
# T02 Rapid movement
# --------------------------------------------------------------------------- #

class TestRapidMovement:
    def _world(self, inbound_usd: float, outbound_usd: float, hold_minutes: float):
        source = account("A0", "C0")
        middle = account("A1", "C1")
        destination = account("A2", "C2", country="MY")
        rows = [
            transfer("IN", source, middle, inbound_usd, START),
            transfer("OUT", middle, destination, outbound_usd,
                     START + timedelta(minutes=hold_minutes)),
        ]
        return context(rows, [source, middle, destination],
                       [customer("C0"), customer("C1"), customer("C2")])

    def test_fires_on_a_fast_high_share_forward(self) -> None:
        alerts = detect.detect_rapid_movement(self._world(50_000, 47_000, 90))
        assert alerts
        assert alerts[0]["subject_id"] == "A1"

    def test_silent_when_held_too_long(self) -> None:
        beyond = RAPID_MOVEMENT.thresholds["max_hold_minutes"] + 60
        assert not detect.detect_rapid_movement(self._world(50_000, 47_000, beyond))

    def test_silent_when_too_little_is_forwarded(self) -> None:
        assert not detect.detect_rapid_movement(self._world(50_000, 20_000, 90))

    def test_silent_below_the_amount_floor(self) -> None:
        assert not detect.detect_rapid_movement(self._world(1_000, 950, 90))

    def test_boundary_at_the_hold_limit_still_fires(self) -> None:
        at_limit = RAPID_MOVEMENT.thresholds["max_hold_minutes"]
        assert detect.detect_rapid_movement(self._world(50_000, 47_000, at_limit))

    def test_paying_out_more_than_arrived_is_described_as_such(self) -> None:
        """Over 100% is not a pass-through and the explanation must not imply it."""
        alerts = detect.detect_rapid_movement(self._world(10_000, 12_000, 90))
        assert alerts
        assert "not a straight pass-through" in alerts[0]["explanation"]

    def test_silent_far_above_the_band(self) -> None:
        assert not detect.detect_rapid_movement(self._world(10_000, 40_000, 90))


# --------------------------------------------------------------------------- #
# T03 Funnel account
# --------------------------------------------------------------------------- #

class TestFunnel:
    def _world(self, sender_count: int, usd_each: float = 4_000, days: float = 5):
        target = account("TARGET", "CT")
        senders = [account(f"S{i}", f"CS{i}") for i in range(sender_count)]
        rows = [
            transfer(f"T{i}", sender, target, usd_each,
                     START + timedelta(days=days * i / max(1, sender_count)),
                     device=f"DEV_{i:06d}")
            for i, sender in enumerate(senders)
        ]
        customers = [customer("CT")] + [customer(f"CS{i}") for i in range(sender_count)]
        return context(rows, [target, *senders], customers)

    def test_fires_on_many_unrelated_senders(self) -> None:
        alerts = detect.detect_funnel(self._world(12))
        assert alerts
        assert alerts[0]["subject_id"] == "TARGET"

    def test_silent_below_the_sender_minimum(self) -> None:
        assert not detect.detect_funnel(self._world(5))

    def test_silent_when_the_total_is_small(self) -> None:
        """Enough senders, but nowhere near the value floor."""
        assert not detect.detect_funnel(self._world(12, usd_each=100))

    def test_boundary_at_the_sender_minimum_fires(self) -> None:
        minimum = int(FUNNEL_ACCOUNT.thresholds["min_senders"])
        assert detect.detect_funnel(self._world(minimum, usd_each=5_000))

    def test_senders_sharing_a_device_are_not_counted_as_unrelated(self) -> None:
        target = account("TARGET", "CT")
        senders = [account(f"S{i}", f"CS{i}") for i in range(12)]
        rows = [
            transfer(f"T{i}", sender, target, 4_000, START + timedelta(hours=i),
                     device="DEV_SHARED")
            for i, sender in enumerate(senders)
        ]
        customers = [customer("CT")] + [customer(f"CS{i}") for i in range(12)]
        alerts = detect.detect_funnel(context(rows, [target, *senders], customers))
        assert alerts
        unrelated = int(
            [f for f in alerts[0]["feature_values"].split("; ")
             if f.startswith("unrelated_sender_count=")][0].split("=")[1]
        )
        assert unrelated == 1, "a shared device should collapse the senders into one group"


# --------------------------------------------------------------------------- #
# T04 Circular flow
# --------------------------------------------------------------------------- #

class TestCircular:
    def _world(self, hops: int, decay: float, gap_hours: float = 10):
        origin = account("ORIGIN", "C0")
        middles = [account(f"M{i}", f"CM{i}") for i in range(hops)]
        chain = [origin, *middles, origin]
        rows = []
        usd = 60_000.0
        clock = START
        for leg in range(len(chain) - 1):
            usd *= decay
            clock += timedelta(hours=gap_hours)
            rows.append(transfer(f"H{leg}", chain[leg], chain[leg + 1], usd, clock))
        customers = [customer("C0")] + [customer(f"CM{i}") for i in range(hops)]
        return context(rows, [origin, *middles], customers)

    def test_fires_on_a_three_hop_round_trip(self) -> None:
        alerts = detect.detect_circular(self._world(2, 0.96))
        assert alerts
        assert alerts[0]["subject_id"] == "ORIGIN"

    def test_silent_on_a_single_hop_out_and_back(self) -> None:
        """Two legs is below the three-hop floor - that is a refund shape."""
        assert not detect.detect_circular(self._world(1, 0.96))

    def test_silent_when_too_little_returns(self) -> None:
        assert not detect.detect_circular(self._world(2, 0.60))

    def test_silent_when_the_round_trip_takes_too_long(self) -> None:
        beyond = CIRCULAR_FLOW.thresholds["max_elapsed_hours"]
        assert not detect.detect_circular(self._world(2, 0.96, gap_hours=beyond))

    def test_the_country_path_is_recorded(self) -> None:
        alerts = detect.detect_circular(self._world(2, 0.96))
        assert "country_path=" in alerts[0]["feature_values"]


# --------------------------------------------------------------------------- #
# T05 Profile mismatch
# --------------------------------------------------------------------------- #

class TestProfileMismatch:
    def _world(self, monthly_expected: float, moved: float, transfers: int = 6):
        payer = account("A1", "C1")
        target = account("A2", "C2")
        rows = [
            transfer(f"T{i}", payer, target, moved / transfers,
                     START + timedelta(days=i * 2), purpose="investment")
            for i in range(transfers)
        ]
        return context(
            rows, [payer, target],
            [customer("C1", expected=monthly_expected), customer("C2")],
        )

    def test_fires_well_above_the_declared_expectation(self) -> None:
        alerts = detect.detect_profile_mismatch(self._world(5_000, 90_000))
        assert alerts
        assert alerts[0]["subject_id"] == "A1"

    def test_silent_within_the_declared_expectation(self) -> None:
        assert not detect.detect_profile_mismatch(self._world(50_000, 60_000))

    def test_silent_below_the_value_floor_even_at_a_high_ratio(self) -> None:
        """20x the expectation, but the expectation was tiny. Not worth a case."""
        assert not detect.detect_profile_mismatch(self._world(100, 2_000))

    def test_boundary_just_over_the_ratio_fires(self) -> None:
        ratio = PROFILE_MISMATCH.thresholds["min_ratio"]
        assert detect.detect_profile_mismatch(self._world(5_000, 5_000 * (ratio + 0.5)))

    def test_boundary_just_under_the_ratio_is_silent(self) -> None:
        ratio = PROFILE_MISMATCH.thresholds["min_ratio"]
        assert not detect.detect_profile_mismatch(self._world(5_000, 5_000 * (ratio - 0.5)))

    def test_off_profile_purposes_are_named(self) -> None:
        alerts = detect.detect_profile_mismatch(self._world(5_000, 90_000))
        assert "investment" in alerts[0]["explanation"]


# --------------------------------------------------------------------------- #
# T06 Missing information
# --------------------------------------------------------------------------- #

class TestMissingInformation:
    def _world(self, affected: int, usd_each: float = 4_000, info: str = "missing"):
        payer = account("A1", "C1")
        target = account("A2", "C2")
        rows = [
            transfer(f"T{i}", payer, target, usd_each, START + timedelta(days=i), info=info)
            for i in range(affected)
        ]
        rows += [
            transfer(f"OK{i}", payer, target, 1_000, START + timedelta(days=30 + i))
            for i in range(3)
        ]
        return context(rows, [payer, target], [customer("C1"), customer("C2")])

    def test_fires_on_several_incomplete_transfers(self) -> None:
        alerts = detect.detect_missing_information(self._world(5))
        assert alerts
        assert alerts[0]["subject_id"] == "A1"

    def test_silent_below_the_count_minimum(self) -> None:
        assert not detect.detect_missing_information(self._world(2))

    def test_silent_below_the_value_floor(self) -> None:
        assert not detect.detect_missing_information(self._world(5, usd_each=100))

    def test_partial_information_counts_as_a_gap(self) -> None:
        assert detect.detect_missing_information(self._world(5, info="partial"))

    def test_complete_information_does_not(self) -> None:
        assert not detect.detect_missing_information(self._world(5, info="complete"))

    def test_a_blank_purpose_is_a_gap_on_its_own(self) -> None:
        payer, target = account("A1", "C1"), account("A2", "C2")
        rows = [
            transfer(f"T{i}", payer, target, 4_000, START + timedelta(days=i), purpose="")
            for i in range(5)
        ]
        alerts = detect.detect_missing_information(
            context(rows, [payer, target], [customer("C1"), customer("C2")])
        )
        assert alerts
        assert "declared_purpose" in alerts[0]["explanation"]

    def test_an_unverified_owner_is_reported_as_part_of_the_gap(self) -> None:
        payer, target = account("A1", "C1"), account("A2", "C2")
        rows = [
            transfer(f"T{i}", payer, target, 4_000, START + timedelta(days=i), info="missing")
            for i in range(5)
        ]
        owners = [{
            "owner_id": "BO_1", "customer_id": "C1", "owner_reference": "abc",
            "ownership_pct": 100, "verification_status": "unverified",
            "recorded_at": START,
        }]
        alerts = detect.detect_missing_information(
            context(rows, [payer, target],
                    [customer("C1", kind="business"), customer("C2")], owners)
        )
        assert alerts
        assert "beneficial_ownership" in alerts[0]["explanation"]


# --------------------------------------------------------------------------- #
# Shared properties of every typology
# --------------------------------------------------------------------------- #

class TestTypologyContracts:
    @pytest.mark.parametrize("typology", TYPOLOGIES, ids=lambda t: t.key)
    def test_no_typology_asserts_a_crime(self, typology) -> None:
        """A typology names a pattern. It must not name a conclusion."""
        text = " ".join([
            typology.title, typology.question, typology.explanation_template,
            *typology.counter_evidence_hints,
        ]).lower()
        for claim in ("money laundering", "laundered", "criminal", "confirmed fraud",
                      "is laundering", "proves", "guilty"):
            assert claim not in text, f"{typology.key} asserts {claim!r}"

    @pytest.mark.parametrize("typology", TYPOLOGIES, ids=lambda t: t.key)
    def test_every_typology_offers_counter_evidence(self, typology) -> None:
        assert len(typology.counter_evidence_hints) >= 2, (
            f"{typology.key} gives a reviewer nothing to argue back with"
        )

    @pytest.mark.parametrize("typology", TYPOLOGIES, ids=lambda t: t.key)
    def test_the_explanation_template_only_uses_declared_fields(self, typology) -> None:
        import string

        used = {
            name for _, name, _, _ in string.Formatter().parse(typology.explanation_template)
            if name
        }
        assert used <= set(typology.evidence_fields), (
            f"{typology.key} interpolates {used - set(typology.evidence_fields)} which is "
            "not in evidence_fields, so it would render as '(not computed)'"
        )

    @pytest.mark.parametrize("typology", TYPOLOGIES, ids=lambda t: t.key)
    def test_a_missing_feature_is_stated_rather_than_hidden(self, typology) -> None:
        rendered = render_explanation(typology, {})
        assert "(not computed)" in rendered or "{" not in rendered


class TestBilingualExplanations:
    """A stored alert is written once in English and read in either language.

    The alternative - storing two sentences - would let the numbers in them
    drift apart. Storing the measurements once and rebuilding the sentence at
    display time cannot.
    """

    @pytest.mark.parametrize("typology", TYPOLOGIES, ids=lambda t: t.key)
    def test_both_templates_take_the_same_fields(self, typology) -> None:
        import string

        def holes(template: str) -> set[str]:
            return {name for _, name, _, _ in string.Formatter().parse(template) if name}

        assert holes(typology.explanation_template) == \
               holes(typology.explanation_template_zh), (
            f"{typology.key}: the two templates interpolate different fields, so one of "
            "them will render a hole"
        )

    @pytest.mark.parametrize("typology", TYPOLOGIES, ids=lambda t: t.key)
    def test_the_chinese_template_is_actually_chinese(self, typology) -> None:
        text = typology.explanation_template_zh
        assert any("一" <= ch <= "鿿" for ch in text), (
            f"{typology.key} has no Chinese characters in its Chinese template"
        )

    def test_feature_values_round_trip(self) -> None:
        from riskops.aml.typology import parse_features

        stored = "sender_count=16; total_usd=96,156; window_days=10"
        assert parse_features(stored) == {
            "sender_count": "16", "total_usd": "96,156", "window_days": "10",
        }

    def test_the_numbers_survive_translation(self) -> None:
        """The point of rebuilding rather than translating: identical values."""
        from riskops.aml.typology import parse_features

        features = parse_features(
            "sender_count=16; sender_country_count=12; total_usd=96,156; "
            "window_days=10; unrelated_sender_count=16; transfer_count=16"
        )
        english = render_explanation(FUNNEL_ACCOUNT, features, "en")
        chinese = render_explanation(FUNNEL_ACCOUNT, features, "zh")
        for value in ("16", "12", "96,156", "10"):
            assert value in english and value in chinese
        assert english != chinese
