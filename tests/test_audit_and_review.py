"""The audit trail and the human workflow.

`test_no_ai_actor_can_commit_a_decision` is the single most important test in
this repository. If it ever fails, the product's central claim is false.
"""

from datetime import datetime, timedelta

import pytest

from riskops.audit.chain import GENESIS, link_hash, verify
from riskops.audit.log import AuditError, AuditLog
from riskops.db import init_schema, session
from riskops.review.workflow import (
    WorkflowError,
    assign,
    file_appeal,
    resolve_appeal,
    submit_decision,
)

T0 = datetime(2026, 8, 1, 9, 0, 0)


@pytest.fixture
def con(small_settings):
    with session(small_settings) as connection:
        init_schema(connection)
        connection.execute(
            """
            INSERT INTO risk.cases
                (case_id, transaction_id, opened_at, case_state, risk_band, risk_score,
                 policy_action, policy_rationale, primary_reason_family, signal_count,
                 max_severity, assigned_role, sla_due_at, appeal_count, is_false_positive,
                 policy_version, rules_version, refresh_batch_id)
            VALUES ('CASE_1', 'TXN_1', ?, 'open', 'high', 0.7, 'manual_review', 'test',
                    'geo_device', 2, 'high', 'risk_analyst', ?, 0, FALSE, '1.0.0', '1.0.0', 'B')
            """,
            [T0, T0 + timedelta(hours=8)],
        )
        yield connection


class TestHashChain:
    def test_an_empty_log_verifies(self):
        assert verify([]).status == "verified"

    def test_a_log_with_no_hashes_is_unverifiable_not_broken(self):
        # Reporting a legacy record as tampered is the opposite of what it means.
        entries = [{"entry_id": "a", "entry_hash": ""}, {"entry_id": "b", "entry_hash": ""}]
        assert verify(entries).status == "unverifiable"

    def test_the_chain_verifies_after_appends(self, con):
        log = AuditLog(con)
        for index in range(5):
            log.append(actor_role="system", actor_id="test", action="x",
                       object_type="case", object_id=f"C{index}", summary="s",
                       occurred_at=T0 + timedelta(minutes=index))
        result = log.verify_chain()
        assert result.status == "verified"
        assert result.entries_checked == 5

    def test_editing_one_entry_breaks_the_chain_from_there_on(self, con):
        log = AuditLog(con)
        for index in range(5):
            log.append(actor_role="system", actor_id="test", action="x",
                       object_type="case", object_id=f"C{index}", summary="original",
                       occurred_at=T0 + timedelta(minutes=index))
        con.execute("UPDATE audit.audit_log SET summary = 'tampered' WHERE seq = 3")
        result = log.verify_chain()
        assert result.status == "broken"
        assert result.broken_at == 2  # zero-indexed: the third entry

    def test_the_chain_is_order_sensitive(self):
        first = {"entry_id": "a", "summary": "one"}
        second = {"entry_id": "b", "summary": "two"}
        forward = link_hash(link_hash(GENESIS, first), second)
        reversed_order = link_hash(link_hash(GENESIS, second), first)
        assert forward != reversed_order

    def test_canonical_form_ignores_dict_insertion_order(self):
        from riskops.audit.chain import canonical
        a = {"entry_id": "1", "summary": "s", "actor_role": "system"}
        b = {"actor_role": "system", "summary": "s", "entry_id": "1"}
        assert canonical(a) == canonical(b)

    def test_verification_states_what_it_does_not_prove(self, con):
        result = AuditLog(con).verify_chain()
        assert "partial edit" in result.summary().lower() or result.entries_checked == 0


class TestAuthority:
    def test_no_ai_actor_can_commit_a_decision(self, con):
        # The product's central claim. Do not weaken this test.
        with pytest.raises(AuditError, match="may not commit a decision"):
            AuditLog(con).record_decision(
                case_id="CASE_1", transaction_id="TXN_1", actor_role="ai_copilot",
                actor_id="copilot", action="release", reason_code="RC_LOW_RESIDUAL_RISK",
            )

    def test_customer_support_cannot_decide_a_case(self, con):
        with pytest.raises(AuditError):
            AuditLog(con).record_decision(
                case_id="CASE_1", transaction_id="TXN_1", actor_role="customer_support",
                actor_id="support.ana", action="release", reason_code="RC_LOW_RESIDUAL_RISK",
            )

    def test_a_person_cannot_abstain(self, con):
        with pytest.raises(AuditError, match="abstain"):
            AuditLog(con).record_decision(
                case_id="CASE_1", transaction_id="TXN_1", actor_role="risk_analyst",
                actor_id="analyst.mei", action="abstain", reason_code="RC_OTHER",
            )

    def test_only_the_policy_may_use_the_automatic_reason_code(self, con):
        with pytest.raises(AuditError, match="deterministic policy"):
            AuditLog(con).record_decision(
                case_id="CASE_1", transaction_id="TXN_1", actor_role="risk_analyst",
                actor_id="analyst.mei", action="release", reason_code="RC_POLICY_AUTO",
            )

    def test_requesting_information_must_say_why_it_is_waiting(self, con):
        with pytest.raises(AuditError, match="RC_INSUFFICIENT_EVIDENCE"):
            AuditLog(con).record_decision(
                case_id="CASE_1", transaction_id="TXN_1", actor_role="risk_analyst",
                actor_id="analyst.mei", action="request_information",
                reason_code="RC_LOW_RESIDUAL_RISK",
            )

    def test_an_unknown_reason_code_is_refused(self, con):
        with pytest.raises(AuditError, match="reason-code taxonomy"):
            AuditLog(con).record_decision(
                case_id="CASE_1", transaction_id="TXN_1", actor_role="risk_analyst",
                actor_id="analyst.mei", action="release", reason_code="RC_VIBES",
            )


class TestWorkflow:
    def test_a_decision_moves_the_case_and_writes_the_trail(self, con):
        assign(con, "CASE_1", "analyst.mei", "risk_analyst", now=T0)
        result = submit_decision(
            con, case_id="CASE_1", actor_id="analyst.mei", actor_role="risk_analyst",
            action="hold", note="two independent signals",
            ai_recommended_action="hold", ai_confidence=0.8,
            decided_at=T0 + timedelta(hours=1),
        )
        assert result["new_case_state"] == "resolved_held"
        assert result["agreed_with_ai"] is True
        case = con.execute("SELECT * FROM risk.cases WHERE case_id='CASE_1'").fetch_df().iloc[0]
        assert case["case_state"] == "resolved_held"
        assert case["resolution_reason_code"] == "RC_SUSPECTED_ACCOUNT_TAKEOVER"
        assert case["handling_minutes"] == pytest.approx(60.0)
        assert AuditLog(con).verify_chain().status == "verified"

    def test_disagreement_with_the_copilot_is_recorded_not_hidden(self, con):
        result = submit_decision(
            con, case_id="CASE_1", actor_id="analyst.mei", actor_role="risk_analyst",
            action="release", ai_recommended_action="hold", ai_confidence=0.9,
            decided_at=T0 + timedelta(hours=1),
        )
        assert result["agreed_with_ai"] is False
        decisions = AuditLog(con).decisions()
        assert bool(decisions.iloc[0]["agreed_with_ai"]) is False
        assert decisions.iloc[0]["ai_recommended_action"] == "hold"

    def test_a_resolved_case_cannot_be_silently_overwritten(self, con):
        submit_decision(con, case_id="CASE_1", actor_id="a", actor_role="risk_analyst",
                        action="hold", decided_at=T0 + timedelta(hours=1))
        with pytest.raises(WorkflowError, match="appeal"):
            submit_decision(con, case_id="CASE_1", actor_id="a", actor_role="risk_analyst",
                            action="release", decided_at=T0 + timedelta(hours=2))

    def test_an_unknown_case_is_refused(self, con):
        with pytest.raises(WorkflowError, match="does not exist"):
            submit_decision(con, case_id="CASE_NOPE", actor_id="a", actor_role="risk_analyst",
                            action="hold")

    def test_requesting_information_leaves_the_case_open(self, con):
        submit_decision(con, case_id="CASE_1", actor_id="a", actor_role="risk_analyst",
                        action="request_information", decided_at=T0 + timedelta(hours=1))
        case = con.execute("SELECT * FROM risk.cases WHERE case_id='CASE_1'").fetch_df().iloc[0]
        assert case["case_state"] == "awaiting_information"
        assert case["resolved_at"] is None or str(case["resolved_at"]) == "NaT"


class TestAppeals:
    def _hold(self, con):
        submit_decision(con, case_id="CASE_1", actor_id="analyst.mei",
                        actor_role="risk_analyst", action="hold",
                        decided_at=T0 + timedelta(hours=1))

    def test_only_a_resolved_case_can_be_appealed(self, con):
        with pytest.raises(WorkflowError, match="only a resolved case"):
            file_appeal(con, case_id="CASE_1", filed_by="support.ana")

    def test_an_accepted_appeal_closes_the_case_as_a_false_positive(self, con):
        self._hold(con)
        appeal = file_appeal(con, case_id="CASE_1", filed_by="support.ana",
                             evidence_type="boarding_pass",
                             filed_at=T0 + timedelta(hours=5))
        result = resolve_appeal(con, appeal_id=appeal["appeal_id"], actor_id="analyst.tom",
                                accepted=True, closed_at=T0 + timedelta(hours=9))
        assert result["case_state"] == "closed_false_positive"
        case = con.execute("SELECT * FROM risk.cases WHERE case_id='CASE_1'").fetch_df().iloc[0]
        assert bool(case["is_false_positive"]) is True
        assert case["resolution_reason_code"] == "RC_APPEAL_EVIDENCE_ACCEPTED"
        assert int(case["appeal_count"]) == 1

    def test_a_rejected_appeal_leaves_the_hold_standing(self, con):
        self._hold(con)
        appeal = file_appeal(con, case_id="CASE_1", filed_by="support.ana",
                             filed_at=T0 + timedelta(hours=5))
        result = resolve_appeal(con, appeal_id=appeal["appeal_id"], actor_id="analyst.tom",
                                accepted=False, closed_at=T0 + timedelta(hours=9))
        assert result["case_state"] == "resolved_held"
        case = con.execute("SELECT * FROM risk.cases WHERE case_id='CASE_1'").fetch_df().iloc[0]
        assert bool(case["is_false_positive"]) is False
        assert case["resolution_reason_code"] == "RC_APPEAL_EVIDENCE_REJECTED"

    def test_an_appeal_never_erases_the_original_decision(self, con):
        self._hold(con)
        appeal = file_appeal(con, case_id="CASE_1", filed_by="support.ana",
                             filed_at=T0 + timedelta(hours=5))
        resolve_appeal(con, appeal_id=appeal["appeal_id"], actor_id="analyst.tom",
                       accepted=True, closed_at=T0 + timedelta(hours=9))
        decisions = AuditLog(con).decisions()
        assert len(decisions) == 2
        assert list(decisions["action"]) == ["hold", "release"]
        assert AuditLog(con).verify_chain().status == "verified"

    def test_an_unknown_appeal_is_refused(self, con):
        with pytest.raises(WorkflowError, match="does not exist"):
            resolve_appeal(con, appeal_id="APL_NOPE", actor_id="a", accepted=True)
