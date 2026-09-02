"""The controlled vocabulary is the contract every other module depends on."""

import pytest

from riskops.config import get_settings
from riskops.generator.scenarios import SCENARIOS, share_total
from riskops.review.workflow import STATE_AFTER_ACTION
from riskops.risk.cases import OWNER_BY_FAMILY
from riskops.taxonomy import (
    ACTOR_ROLE,
    CASE_STATE,
    DECIDING_ROLES,
    DECISION_ACTION,
    DIMENSIONS,
    HUMAN_ACTIONS,
    PAYMENT_STATE,
    SIGNAL_FAMILY,
    TERMINAL_CASE_STATES,
    total_value_count,
)


class TestStructure:
    def test_every_value_is_documented(self):
        for dimension in DIMENSIONS:
            for value in dimension.values:
                assert value.definition, f"{dimension.key}.{value.name} has no definition"
                assert value.example, f"{dimension.key}.{value.name} has no example"
                assert value.boundary, f"{dimension.key}.{value.name} has no boundary case"

    def test_values_are_unique_within_a_dimension(self):
        for dimension in DIMENSIONS:
            assert len(dimension.names) == len(set(dimension.names))

    def test_aliases_never_collide_across_values(self):
        for dimension in DIMENSIONS:
            mapping = dimension.alias_map()
            # Every alias resolves to exactly one canonical name; the dict would
            # silently absorb a collision, so count the raw pairs instead.
            pairs = [
                (alias, value.name)
                for value in dimension.values
                for alias in [*value.aliases, value.name]
            ]
            assert len(pairs) == len(mapping), f"{dimension.key} has a colliding alias"

    def test_fallback_is_a_real_value_or_deliberately_outside(self):
        for dimension in DIMENSIONS:
            if dimension.fallback in dimension.names:
                continue
            # `unknown` and `other` are escape hatches that may sit outside the
            # enumerated values, but nothing else may.
            assert dimension.fallback in ("unknown", "other", "open", "system",
                                          "RC_OTHER", "request_information")

    def test_dimension_count_is_stable(self):
        assert len(DIMENSIONS) == 6
        assert total_value_count() > 40


class TestNormalisation:
    def test_upstream_spellings_collapse(self):
        for raw in ("CAPTURED", "capture", "Capture", " captured "):
            assert PAYMENT_STATE.normalise(raw) == "captured"

    def test_unknown_value_falls_back_rather_than_guessing(self):
        assert PAYMENT_STATE.normalise("teleported") == "unknown"
        assert SIGNAL_FAMILY.normalise("vibes") == "other"


class TestAuthorityBoundary:
    def test_the_copilot_is_not_a_deciding_role(self):
        # The central product claim, asserted as data rather than prose.
        assert "ai_copilot" in ACTOR_ROLE.names
        assert "ai_copilot" not in DECIDING_ROLES

    def test_abstain_is_reserved_for_the_copilot(self):
        assert "abstain" in DECISION_ACTION.names
        assert "abstain" not in HUMAN_ACTIONS

    def test_every_human_action_moves_the_case_somewhere(self):
        for action in HUMAN_ACTIONS:
            assert action in STATE_AFTER_ACTION
            assert STATE_AFTER_ACTION[action] in CASE_STATE.names

    def test_terminal_states_are_real_states(self):
        for state in TERMINAL_CASE_STATES:
            assert state in CASE_STATE.names

    def test_every_signal_family_has_an_owning_role(self):
        for family in SIGNAL_FAMILY.names:
            assert family in OWNER_BY_FAMILY, f"{family} has no owner; cases would be unrouted"
            assert OWNER_BY_FAMILY[family] in ACTOR_ROLE.names


class TestScenarios:
    def test_shares_sum_to_one(self):
        assert share_total() == pytest.approx(1.0, abs=0.005)

    def test_every_scenario_expects_a_real_action(self):
        for scenario in SCENARIOS:
            assert scenario.expected_action in DECISION_ACTION.names
            assert scenario.family in (*SIGNAL_FAMILY.names, "none")

    def test_benign_scenarios_exist_and_include_false_positive_bait(self):
        benign = [s for s in SCENARIOS if not s.is_actionable]
        assert len(benign) >= 3
        # Without a benign scenario that deliberately trips a rule, a detector
        # that flags everything scores perfectly.
        assert any("false_positive_bait" in s.tags for s in benign)

    def test_adversarial_scenarios_exist(self):
        assert any("prompt_injection" in s.tags for s in SCENARIOS)
        assert any("authority_escalation" in s.tags for s in SCENARIOS)


class TestSettings:
    def test_no_secret_has_a_default(self, monkeypatch):
        monkeypatch.delenv("RISKOPS_LLM_API_KEY", raising=False)
        assert get_settings().llm_api_key is None

    def test_provider_defaults_to_mock_so_the_product_runs_with_no_key(self, monkeypatch):
        monkeypatch.delenv("RISKOPS_LLM_PROVIDER", raising=False)
        assert get_settings().llm_provider == "mock"

    def test_settings_reread_the_environment_on_every_call(self, monkeypatch):
        before = get_settings().random_seed
        monkeypatch.setenv("RISKOPS_SEED", str(before + 7))
        assert get_settings().random_seed == before + 7

    def test_sla_hours_are_tighter_for_higher_risk(self):
        settings = get_settings()
        assert settings.sla_hours("critical") < settings.sla_hours("high")
        assert settings.sla_hours("high") < settings.sla_hours("medium")
        assert settings.sla_hours("medium") < settings.sla_hours("low")
