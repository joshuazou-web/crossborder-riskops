"""Case Detail - the screen an analyst actually works in.

The layout encodes the product's central claim. Evidence and the AI brief sit
side by side, the brief is labelled advisory everywhere it appears, and the
decision controls are the only place an outcome can be committed - by a person,
with a reason code, into an append-only log.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from _shared import money, page_setup, pill, synthetic_banner  # noqa: E402
from riskops.ai.copilot import investigate, record_invocation  # noqa: E402
from riskops.ai.prompt import build_case_packet  # noqa: E402
from riskops.ai.schema import CaseBrief  # noqa: E402
from riskops.config import get_settings  # noqa: E402
from riskops.db import session  # noqa: E402
from riskops.review.workflow import (  # noqa: E402
    WorkflowError,
    file_appeal,
    reason_for,
    resolve_appeal,
    submit_decision,
)
from riskops.taxonomy import HUMAN_ACTIONS, REASON_CODE  # noqa: E402

frames = page_setup("Case Detail", "🗂️")
cases = frames["marts.fct_cases"]
signals = frames["risk.signals"]
breaks = frames["core.reconciliation_breaks"]
invocations = frames["audit.ai_invocations"]
decisions = frames["audit.decisions"]
appeals = frames["audit.appeals"]
events = frames["core.payment_events"]

st.title("Case Detail")
synthetic_banner(
    "Decisions taken on this screen are written to the local warehouse and are part of the demo."
)

case_ids = list(cases.sort_values(["is_open", "risk_score"], ascending=[False, False])["case_id"])
if not case_ids:
    st.info("No cases in this dataset.")
    st.stop()

preselected = st.session_state.get("selected_case_id")
index = case_ids.index(preselected) if preselected in case_ids else 0
case_id = st.selectbox("Case", case_ids, index=index)
st.session_state["selected_case_id"] = case_id

case = cases[cases["case_id"] == case_id].iloc[0]
txn_id = str(case["transaction_id"])
transaction = frames["core.transactions"]
full = transaction[transaction["transaction_id"] == txn_id].iloc[0]

# --- header ----------------------------------------------------------------
st.markdown(
    pill(str(case["risk_band"]).upper(), str(case["risk_band"]))
    + pill(str(case["case_state"]), "none")
    + pill(f"policy: {case['policy_action']}", "none")
    + pill(f"owner: {case['assigned_role']}", "none"),
    unsafe_allow_html=True,
)
header = st.columns(5)
header[0].metric("Risk score", f"{case['risk_score']:.2f}")
header[1].metric("Signals", int(case["signal_count"]))
header[2].metric("Amount", money(full["captured_minor"], full["presentment_currency"]))
header[3].metric("Corridor", str(case["corridor"]))
header[4].metric("SLA due", pd.Timestamp(case["sla_due_at"]).strftime("%Y-%m-%d %H:%M"))

st.caption(f"**Why this case exists:** {case['policy_rationale']}")

st.divider()

evidence_column, ai_column = st.columns([3, 2], gap="large")

# --- evidence --------------------------------------------------------------
with evidence_column:
    st.subheader("Evidence")

    st.markdown("**Payment timeline**")
    timeline = events[events["transaction_id"] == txn_id].sort_values("occurred_at")
    st.dataframe(
        timeline[["occurred_at", "event_type", "from_state", "to_state", "amount_minor",
                  "currency", "accepted"]],
        use_container_width=True, hide_index=True, height=200,
        column_config={
            "occurred_at": st.column_config.DatetimeColumn("When", format="MM-DD HH:mm:ss"),
            "event_type": st.column_config.TextColumn("Event", width="small"),
            "amount_minor": st.column_config.NumberColumn("Minor units", format="%d"),
            "accepted": st.column_config.CheckboxColumn("OK"),
        },
    )

    st.markdown("**Signals**")
    case_signals = signals[signals["transaction_id"] == txn_id].sort_values(
        "weight", ascending=False
    )
    for record in case_signals.to_dict("records"):
        with st.container(border=True):
            st.markdown(
                pill(record["severity"], record["severity"])
                + f"**{record['rule_id']}** — {record['title']}",
                unsafe_allow_html=True,
            )
            st.write(record["detail"])
            st.markdown(f"<div class='evidence'>evidence fields: {record['evidence_fields']}"
                        f" · family {record['signal_family']} · rules v{record['rule_version']}"
                        "</div>", unsafe_allow_html=True)

    case_breaks = breaks[breaks["transaction_id"] == txn_id]
    if not case_breaks.empty:
        st.markdown("**Reconciliation breaks**")
        for record in case_breaks.to_dict("records"):
            with st.container(border=True):
                st.markdown(f"**{record['break_type']}** · {record['difference_bps']} bps")
                st.write(record["detail"])
                st.markdown(
                    f"<div class='evidence'>expected {record['expected_minor']} · observed "
                    f"{record['observed_minor']} {record['currency']}</div>",
                    unsafe_allow_html=True,
                )

    scores = frames["risk.model_scores"]
    score_row = scores[scores["transaction_id"] == txn_id]
    if not score_row.empty:
        st.markdown("**Residual-risk model**")
        row = score_row.iloc[0]
        st.write(
            f"Score **{row['score']:.2f}** (band {row['band']}), model version "
            f"`{row['model_version']}`. Logistic regression on named features - the four that "
            "moved this score most:"
        )
        contributions = pd.DataFrame(json.loads(row["top_features"]))
        st.dataframe(contributions[["label", "contribution"]], use_container_width=True,
                     hide_index=True,
                     column_config={
                         "label": st.column_config.TextColumn("Feature"),
                         "contribution": st.column_config.NumberColumn(
                             "Contribution", format="%.3f"),
                     })

    note = str(full["merchant_note"] or "")
    if note:
        st.markdown("**Merchant free text** — untrusted, attacker-controlled")
        st.code(note, language=None)

# --- AI brief --------------------------------------------------------------
with ai_column:
    st.subheader("AI investigation brief")
    st.markdown(
        """<div class="advisory-box">
        <h4>Advisory only</h4>
        This brief organises evidence, explains signals, names what is missing and
        <em>suggests</em> an action. It has no authority to release, hold, refund or close
        anything, and the audit log refuses to record an AI actor on a decision.
        </div>""",
        unsafe_allow_html=True,
    )

    stored = invocations[invocations["case_id"] == case_id].sort_values("created_at")
    brief = None
    if not stored.empty:
        brief = CaseBrief.from_dict(json.loads(stored.iloc[-1]["brief_json"]))

    if st.button("Regenerate brief", help="Re-runs the copilot against the current case packet."):
        settings = get_settings()
        merchants = frames["core.merchants"]
        wallets = frames["core.wallets"]
        merchant_rows = merchants[merchants["merchant_id"] == str(full["merchant_id"])]
        wallet_rows = wallets[wallets["wallet_id"] == str(full["wallet_id"])]
        score_rows = frames["risk.model_scores"]
        score_rows = score_rows[score_rows["transaction_id"] == txn_id]
        packet, allowed, gate = build_case_packet(
            case=case.to_dict(),
            transaction=full.to_dict(),
            signals=case_signals.to_dict("records"),
            breaks=case_breaks.to_dict("records"),
            merchant=merchant_rows.iloc[0].to_dict() if not merchant_rows.empty else {},
            wallet=wallet_rows.iloc[0].to_dict() if not wallet_rows.empty else {},
            model_score=score_rows.iloc[0].to_dict() if not score_rows.empty else None,
        )
        brief = investigate(
            settings, case_id=case_id, transaction_id=txn_id, packet=packet,
            allowed_citations=allowed, input_gate=gate, requested_by="dashboard",
        )
        with session(settings) as con:
            record_invocation(con, brief, packet=packet, requested_by="dashboard")
        st.cache_data.clear()

    if brief is None:
        st.info("No brief has been generated for this case yet.")
    else:
        if brief.abstained:
            st.warning(f"**The copilot abstained.** {brief.rationale}")
        else:
            st.markdown(
                f"**Suggested action:** `{brief.recommended_action}` · confidence "
                f"{brief.confidence:.2f}"
            )
            st.caption(brief.rationale)

        st.markdown("**Summary**")
        st.write(brief.summary)

        if brief.key_facts:
            st.markdown("**Key facts**")
            for finding in brief.key_facts:
                st.markdown(f"- {finding.statement}")
                st.markdown(
                    f"<div class='evidence'>cites: {', '.join(finding.citations)}</div>",
                    unsafe_allow_html=True,
                )

        if brief.signal_explanations:
            st.markdown("**What the signals mean**")
            for finding in brief.signal_explanations:
                st.markdown(f"- {finding.statement}")
                st.markdown(
                    f"<div class='evidence'>cites: {', '.join(finding.citations)}</div>",
                    unsafe_allow_html=True,
                )

        if brief.conflicts:
            st.markdown("**Conflicts in the evidence**")
            for finding in brief.conflicts:
                st.markdown(f"- {finding.statement}")

        if brief.missing_information:
            st.markdown("**Missing information**")
            for item in brief.missing_information:
                st.markdown(f"- {item}")

        if brief.suggested_questions:
            st.markdown("**Questions to ask**")
            for item in brief.suggested_questions:
                st.markdown(f"- {item}")

        with st.expander("Guardrails and provenance"):
            guard = brief.guardrail
            st.write(
                pd.DataFrame([
                    {"Field": "Provider", "Value": brief.provider},
                    {"Field": "Model", "Value": brief.model_version},
                    {"Field": "Prompt version", "Value": brief.prompt_version},
                    {"Field": "Rules version", "Value": brief.rules_version},
                    {"Field": "Generated", "Value": brief.generated_at},
                    {"Field": "Latency", "Value": f"{brief.latency_ms:.0f} ms"},
                    {"Field": "Input gate", "Value": guard.injection_verdict},
                    {"Field": "Injection patterns", "Value": ", ".join(guard.injection_labels) or "-"},
                    {"Field": "Output gate", "Value": guard.verdict},
                    {"Field": "Findings dropped as ungrounded", "Value": guard.dropped_findings},
                    {"Field": "Unresolved citations",
                     "Value": ", ".join(guard.unresolved_citations) or "-"},
                    {"Field": "PII redactions", "Value": guard.redactions},
                ]).set_index("Field")
            )
            if guard.injection_verdict == "quarantined":
                st.error(
                    "Merchant free text on this case contained instructions aimed at an "
                    "automated reader. It was withheld from the model. Whatever explanation it "
                    "contained has to be obtained from a person instead."
                )
            for reason in guard.reasons:
                st.caption(f"• {reason}")

st.divider()

# --- decision --------------------------------------------------------------
st.subheader("Decision")
if str(case["case_state"]) in ("resolved_released", "resolved_held", "closed_false_positive"):
    st.info(
        f"This case is **{case['case_state']}** — decided `{case['resolution_action']}` with "
        f"reason `{case['resolution_reason_code']}`. A resolved case is never overwritten; "
        "reopen it through an appeal below."
    )
else:
    with st.form("decision"):
        columns = st.columns([1, 1, 2])
        action = columns[0].selectbox("Action", HUMAN_ACTIONS)
        default_reason = reason_for(action, str(case["primary_reason_family"]))
        reason_options = (
            ["RC_INSUFFICIENT_EVIDENCE"] if action == "request_information"
            else [r for r in REASON_CODE.names if r != "RC_POLICY_AUTO"]
        )
        reason_index = reason_options.index(default_reason) if default_reason in reason_options else 0
        reason = columns[1].selectbox("Reason code", reason_options, index=reason_index)
        note = columns[2].text_input("Note", placeholder="what settled it")
        actor = st.text_input("Your identifier", value="analyst.demo")
        submitted = st.form_submit_button("Commit decision", type="primary")

    if submitted:
        try:
            with session(get_settings()) as con:
                result = submit_decision(
                    con, case_id=case_id, actor_id=actor,
                    actor_role=str(case["assigned_role"]) if str(case["assigned_role"]) != "customer_support"
                    else "risk_analyst",
                    action=action, reason_code=reason, note=note,
                    ai_recommended_action=str(case["ai_recommended_action"] or ""),
                    ai_confidence=float(case["ai_confidence"])
                    if pd.notna(case["ai_confidence"]) else None,
                )
            st.cache_data.clear()
            st.success(
                f"Recorded `{action}` ({reason}). Case is now **{result['new_case_state']}**. "
                "The decision is appended to the hash-chained audit log."
            )
            st.rerun()
        except WorkflowError as exc:
            st.error(str(exc))

# --- appeals ---------------------------------------------------------------
st.markdown("**Appeals**")
case_appeals = appeals[appeals["case_id"] == case_id]
if not case_appeals.empty:
    st.dataframe(
        case_appeals[["appeal_id", "claimant", "evidence_type", "filed_at", "outcome",
                      "outcome_reason_code", "closed_at"]],
        use_container_width=True, hide_index=True,
    )
    open_appeals = case_appeals[case_appeals["outcome"].astype(str) == ""]
    if not open_appeals.empty:
        appeal_id = str(open_appeals.iloc[0]["appeal_id"])
        columns = st.columns([1, 1, 3])
        if columns[0].button("Accept appeal", type="primary"):
            with session(get_settings()) as con:
                resolve_appeal(con, appeal_id=appeal_id, actor_id="analyst.demo", accepted=True,
                               note="Evidence resolves the signals.")
            st.cache_data.clear()
            st.rerun()
        if columns[1].button("Reject appeal"):
            with session(get_settings()) as con:
                resolve_appeal(con, appeal_id=appeal_id, actor_id="analyst.demo", accepted=False,
                               note="Evidence does not resolve the signals.")
            st.cache_data.clear()
            st.rerun()
elif str(case["case_state"]) in ("resolved_held", "resolved_released"):
    with st.form("appeal"):
        columns = st.columns([1, 1, 2])
        claimant = columns[0].selectbox("Claimant", ["payer", "merchant"])
        evidence = columns[1].selectbox(
            "Evidence", ["travel_itinerary", "boarding_pass", "delivery_receipt", "bank_statement"]
        )
        evidence_note = columns[2].text_input("Note", placeholder="what the claimant supplied")
        filed = st.form_submit_button("File appeal")
    if filed:
        with session(get_settings()) as con:
            file_appeal(con, case_id=case_id, filed_by="support.demo", claimant=claimant,
                        evidence_type=evidence, evidence_note=evidence_note)
        st.cache_data.clear()
        st.rerun()
else:
    st.caption("A case can be appealed once it has been resolved.")

st.markdown("**Decision history**")
case_decisions = decisions[decisions["case_id"] == case_id].sort_values("decided_at")
if case_decisions.empty:
    st.caption("No decision has been committed on this case yet.")
else:
    st.dataframe(
        case_decisions[["decided_at", "actor_role", "actor_id", "action", "reason_code",
                        "ai_recommended_action", "agreed_with_ai", "previous_case_state",
                        "new_case_state", "note"]],
        use_container_width=True, hide_index=True,
        column_config={
            "decided_at": st.column_config.DatetimeColumn("When", format="YYYY-MM-DD HH:mm"),
            "agreed_with_ai": st.column_config.CheckboxColumn("Agreed with AI"),
        },
    )
