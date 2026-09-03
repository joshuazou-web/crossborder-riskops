"""What the AML layer refuses to do, asserted rather than described.

The product claim this file defends: **this system finds patterns and orders a
queue; a person decides.** That claim is only worth making if it is enforced
somewhere other than a paragraph in a README, so each of these tests attacks it
from a different direction - the vocabulary, the actor, the reason, the state
machine, and the dependency graph.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from riskops.aml import aggregate, detect, investigation, priority, world
from riskops.aml.aggregate import CASE_STATES, FORBIDDEN_STATES
from riskops.aml.investigation import (
    DISPOSITIONS,
    FORBIDDEN_DISPOSITION_WORDS,
    INVESTIGATOR_ROLES,
    DispositionError,
    validate,
)

GOOD_REASON = (
    "Transfers TRF_00000123 and TRF_00000124 match the payroll schedule on file; the "
    "beneficial owner for CUST_000045 is verified."
)


# --------------------------------------------------------------------------- #
# The vocabulary cannot express a conclusion the system cannot support
# --------------------------------------------------------------------------- #

class TestVocabulary:
    def test_no_case_state_asserts_a_crime_or_a_filing(self) -> None:
        for state in CASE_STATES:
            assert state not in FORBIDDEN_STATES
            for banned in ("launder", "sar", "regulator", "frozen", "blacklist", "guilty"):
                assert banned not in state.lower(), f"case state {state!r} contains {banned!r}"

    def test_no_disposition_asserts_a_crime_a_filing_or_a_freeze(self) -> None:
        for disposition in DISPOSITIONS:
            surface = f"{disposition.key} {disposition.title} {disposition.next_state}".lower()
            for banned in FORBIDDEN_DISPOSITION_WORDS:
                assert banned.replace("_", " ") not in surface, (
                    f"{disposition.key} offers {banned!r}, which this system cannot do"
                )

    def test_escalation_says_plainly_that_it_reaches_no_authority(self) -> None:
        """The one disposition a reader could mistake for a regulatory filing."""
        escalate = next(d for d in DISPOSITIONS if d.key == "escalate")
        text = escalate.description.lower()
        assert "files nothing" in text or "reaches no authority" in text

    def test_closing_does_not_claim_the_customer_was_cleared(self) -> None:
        close = next(d for d in DISPOSITIONS if d.key == "close_no_action")
        assert "cleared of anything" in close.description.lower()

    def test_every_disposition_maps_to_a_real_case_state(self) -> None:
        for disposition in DISPOSITIONS:
            assert disposition.next_state in CASE_STATES


# --------------------------------------------------------------------------- #
# Only a person may decide
# --------------------------------------------------------------------------- #

class TestOnlyAPersonDecides:
    @pytest.mark.parametrize(
        "role",
        ["ai_copilot", "copilot", "system", "model", "assistant", "pipeline",
         "llm", "agent", "automation", ""],
    )
    def test_a_non_human_actor_is_refused(self, role: str) -> None:
        with pytest.raises(DispositionError, match="may not record"):
            validate("close_no_action", GOOD_REASON, role)

    @pytest.mark.parametrize("role", INVESTIGATOR_ROLES)
    def test_a_human_role_is_allowed(self, role: str) -> None:
        assert validate("close_no_action", GOOD_REASON, role).key == "close_no_action"

    def test_the_ai_copilot_is_not_in_the_permitted_roles(self) -> None:
        for role in INVESTIGATOR_ROLES:
            assert "ai" not in role.split("_"), f"{role!r} looks like a non-human actor"
            assert "copilot" not in role and "model" not in role

    def test_recording_is_refused_before_anything_is_written(self) -> None:
        """`validate` runs first, so a refused disposition touches no table.

        Asserted by construction: `record_disposition` calls `validate` before
        its first `con.execute`, and a connection object that raises on use
        proves nothing was attempted.
        """
        class Explodes:
            def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
                raise AssertionError("a refused disposition wrote to the database")

        with pytest.raises(DispositionError):
            investigation.record_disposition(
                Explodes(), case_id="AMLCASE_X", disposition_key="close_no_action",
                reason=GOOD_REASON, actor_role="ai_copilot", actor_id="bot",
                now=datetime.now(),
            )


# --------------------------------------------------------------------------- #
# A decision without a reason is not a decision
# --------------------------------------------------------------------------- #

class TestReasonIsMandatory:
    @pytest.mark.parametrize(
        "reason", ["", "   ", "ok", "fine", "closed", "n/a", "-", "looks fine", "no issue"]
    )
    def test_an_empty_or_token_reason_is_refused(self, reason: str) -> None:
        with pytest.raises(DispositionError, match="written reason"):
            validate("close_no_action", reason, "aml_investigator")

    @pytest.mark.parametrize("key", ["escalate", "request_information"])
    def test_escalation_and_information_requests_must_name_something(self, key: str) -> None:
        vague = (
            "This looks very suspicious to me and I think somebody more senior "
            "should take a careful look at the whole thing."
        )
        with pytest.raises(DispositionError, match="name what specifically"):
            validate(key, vague, "aml_investigator")

    @pytest.mark.parametrize("key", ["escalate", "request_information"])
    def test_a_reason_naming_evidence_is_accepted(self, key: str) -> None:
        specific = (
            "Beneficial owner for CUST_000123 is unverified; need company registration "
            "documents before this can be judged either way."
        )
        assert validate(key, specific, "aml_investigator").key == key

    def test_an_unknown_disposition_is_refused(self) -> None:
        with pytest.raises(DispositionError, match="not a disposition"):
            validate("freeze_account", GOOD_REASON, "aml_investigator")

    def test_the_minimum_length_is_not_trivially_satisfiable(self) -> None:
        assert investigation.MINIMUM_REASON_CHARACTERS >= 20


# --------------------------------------------------------------------------- #
# The detection path contains no model at all
# --------------------------------------------------------------------------- #

class TestNoModelInTheCoreLoop:
    def test_detection_and_aggregation_import_no_provider(self) -> None:
        """Checked against the import graph, not against the text.

        A substring search over the source would also match the docstrings,
        which discuss models at length precisely because their absence is the
        point. What matters is what the module actually pulls in.
        """
        import ast
        import inspect

        forbidden = {"provider", "copilot", "prompt", "guardrails", "schema"}
        for module in (detect, aggregate, priority, world):
            tree = ast.parse(inspect.getsource(module))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom):
                    imported.add(node.module or "")
                    imported |= {alias.name for alias in node.names}
            leaked = {
                name for name in imported
                if any(bad in name.lower() for bad in forbidden)
                or name.lower() in {"openai", "anthropic", "httpx", "requests"}
            }
            assert not leaked, (
                f"{module.__name__} imports {leaked}; the core loop must run with no "
                "model available at all"
            )

    def test_the_whole_pipeline_runs_with_no_api_key(self, monkeypatch) -> None:
        """The demo has to work for a reader who never sets a key."""
        from riskops.config import Settings

        monkeypatch.delenv("RISKOPS_LLM_API_KEY", raising=False)
        monkeypatch.setenv("RISKOPS_LLM_PROVIDER", "mock")
        settings = Settings.from_env()
        assert settings.llm_api_key is None

        generated = world.generate(settings, seed=7, n_transfers=1_500)
        now = datetime.fromisoformat(settings.as_of_date)
        context = detect.AmlContext.blind(
            generated.transfers, accounts=generated.accounts,
            customers=generated.customers,
            beneficial_owners=generated.beneficial_owners, now=now,
        )
        alerts = detect.run_all(context)
        result = aggregate.aggregate(alerts, now=now)
        cases = priority.prioritise(
            result.cases, generated.transfers, generated.accounts, generated.customers,
            now=now, review_capacity=10,
        )
        assert len(generated.transfers) == 1_500
        assert isinstance(cases, pd.DataFrame)


# --------------------------------------------------------------------------- #
# Priority is an ordering, and says so
# --------------------------------------------------------------------------- #

class TestPriorityIsAnOrdering:
    def test_the_weights_sum_to_one(self) -> None:
        assert abs(sum(priority.WEIGHTS.values()) - 1.0) < 1e-9

    def test_every_weighted_factor_has_a_human_label(self) -> None:
        assert set(priority.WEIGHTS) == set(priority.FACTOR_LABELS)

    def test_a_score_is_the_sum_of_its_published_contributions(self) -> None:
        """The breakdown shown to an investigator has to add up to the number
        they are being shown, or it is decoration rather than an explanation."""
        case = {
            "max_severity": "high", "alert_count": 3, "corroborating_typologies": 2,
            "total_usd_minor": 250_000_00, "subject_id": "ACCT_1",
            "opened_at": datetime(2026, 8, 1),
        }
        transfers = pd.DataFrame([{
            "timestamp": datetime(2026, 7, 20) + timedelta(hours=i),
            "payer_account": "ACCT_1", "beneficiary_account": f"ACCT_{i + 2}",
            "origin_country": "SG", "destination_country": "MY",
            "beneficiary_information_status": "complete", "declared_purpose": "salary",
        } for i in range(8)])
        score, contributions = priority.score_case(
            case, transfers, {"customer_risk_level": "medium", "account_age_days": 400},
            now=datetime(2026, 8, 31),
        )
        assert abs(sum(c.points for c in contributions) - score) < 1e-9
        assert len(contributions) == len(priority.WEIGHTS)

    def test_bands_are_ordered_and_exhaustive(self) -> None:
        floors = [floor for floor, _ in priority.BANDS]
        assert floors == sorted(floors, reverse=True)
        assert floors[-1] == 0.0, "every score must land in a band"

    def test_capacity_marks_a_cut_rather_than_dropping_the_rest(self) -> None:
        """Cases below the line stay in the table. Losing them is how a backlog
        becomes invisible."""
        from riskops.config import Settings

        generated = world.generate(Settings.from_env(), seed=11, n_transfers=2_000)
        now = datetime(2026, 8, 31)
        context = detect.AmlContext.blind(
            generated.transfers, accounts=generated.accounts,
            customers=generated.customers,
            beneficial_owners=generated.beneficial_owners, now=now,
        )
        result = aggregate.aggregate(detect.run_all(context), now=now)
        if result.cases.empty:
            pytest.skip("no cases in this small world")
        cases = priority.prioritise(
            result.cases, generated.transfers, generated.accounts, generated.customers,
            now=now, review_capacity=5,
        )
        assert len(cases) == len(result.cases)
        assert int(cases["within_capacity"].sum()) == min(5, len(cases))
        assert cases["queue_position"].tolist() == list(range(1, len(cases) + 1))


# --------------------------------------------------------------------------- #
# Aggregation must not lose evidence
# --------------------------------------------------------------------------- #

class TestAggregationKeepsEvidence:
    def _alerts(self) -> pd.DataFrame:
        base = datetime(2026, 8, 1)
        rows = []
        for day in range(4):
            rows.append({
                "alert_id": f"ALT_A{day}", "typology_id": "AML_T01_STRUCTURING",
                "typology_version": "1.0.0", "severity": "high", "subject_type": "account",
                "subject_id": "ACCT_1", "subject_customer_id": "CUST_1",
                "triggered_at": base, "window_start": base + timedelta(days=day),
                "window_end": base + timedelta(days=day, hours=6),
                "transfer_ids": "|".join(f"TRF_{i}" for i in range(day + 2)),
                "transfer_count": day + 2, "entity_ids": "ACCT_1",
                "total_usd_minor": 100_000 * (day + 1),
                "feature_values": "x=1", "threshold_values": "y=2",
                "explanation": "e" * 60, "evidence_fields": "a|b",
                "counter_evidence": "c" * 60, "dedup_key": f"structuring:ACCT_1:{day}",
            })
        return pd.DataFrame(rows)

    def test_duplicates_are_collapsed_and_counted(self) -> None:
        deduped, removed = aggregate.deduplicate(self._alerts())
        assert removed == 3
        assert len(deduped) == 1
        assert int(deduped.iloc[0]["duplicate_count"]) == 3

    def test_the_survivor_is_the_widest_observation(self) -> None:
        deduped, _ = aggregate.deduplicate(self._alerts())
        assert int(deduped.iloc[0]["transfer_count"]) == 5

    def test_no_transfer_reference_is_lost(self) -> None:
        alerts = self._alerts()
        before: set[str] = set()
        for value in alerts["transfer_ids"]:
            before |= set(str(value).split("|"))
        deduped, _ = aggregate.deduplicate(alerts)
        after: set[str] = set()
        for value in deduped["transfer_ids"]:
            after |= set(str(value).split("|"))
        assert before == after, "deduplication took evidence with it"

    def test_every_case_records_why_it_merged(self) -> None:
        deduped, _ = aggregate.deduplicate(self._alerts())
        linked, cases = aggregate.build_cases(deduped, now=datetime(2026, 8, 31))
        assert not cases.empty
        assert (cases["merge_rationale"].str.len() > 40).all()
        assert (linked["case_id"].str.len() > 0).all()

    def test_alerts_far_apart_in_time_are_kept_as_separate_cases(self) -> None:
        alerts = self._alerts()
        alerts.loc[3, "window_start"] = datetime(2026, 8, 1) + timedelta(days=120)
        alerts.loc[3, "window_end"] = datetime(2026, 8, 1) + timedelta(days=120, hours=6)
        alerts.loc[3, "dedup_key"] = "structuring:ACCT_1:far"
        deduped, _ = aggregate.deduplicate(alerts)
        _, cases = aggregate.build_cases(deduped, now=datetime(2026, 12, 31))
        assert len(cases) == 2
        assert any("more than" in r for r in cases["merge_rationale"])
