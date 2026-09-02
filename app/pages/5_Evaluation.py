"""Evaluation - the measured behaviour of the system, and where the AI boundary sits."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import streamlit as st  # noqa: E402

from _shared import kpi_row, neutral_chart_layout, page_setup, synthetic_banner  # noqa: E402
from riskops.config import get_settings  # noqa: E402

frames = page_setup("Evaluation", "📐")

st.title("Evaluation and AI boundaries")
synthetic_banner()

report_path = get_settings().reports_dir / "evaluation.json"
if not report_path.exists():
    st.warning(
        "No evaluation has been run yet. Run this, then reload:\n\n"
        "```\npython -m riskops eval\n```"
    )
    st.stop()

results = json.loads(report_path.read_text(encoding="utf-8"))
detection = results["risk_detection"]
ai_quality = results["ai_quality"]
safety = results["agent_safety"]
money = results["money_and_state"]
operations = results["operations"]

st.caption(
    f"Generated {results['generated_at']} · seed `{results['configuration']['seed']}` · "
    f"provider `{results['configuration']['llm_provider']}` · "
    f"rules `{results['versions']['rules']}` · policy `{results['versions']['policy']}`"
)

with st.expander("Read this before reading any number", expanded=True):
    for caveat in results["caveats"]:
        st.markdown(f"- {caveat}")

tab_detection, tab_ai, tab_safety, tab_money, tab_ops = st.tabs(
    ["Risk detection", "AI brief quality", "Agent safety", "Money and lifecycle", "Operations"]
)

with tab_detection:
    kpi_row([
        ("Recall", f"{detection['recall_pct']}%",
         "Of the transactions the generator labelled actionable, the share the policy routed "
         "anywhere other than auto-release."),
        ("Precision", f"{detection['precision_pct']}%",
         "Of the transactions routed to a person, the share that really were actionable."),
        ("False-positive rate", f"{detection['false_positive_rate_pct']}%",
         "Benign transactions routed to a person. This is the cost of the recall above."),
        ("Manual review rate", f"{detection['manual_review_rate_pct']}%",
         "The share of all traffic a person has to look at."),
        ("Auto-release leakage", f"{detection['auto_release_leakage_pct']}%",
         "Actionable transactions that were auto-released - the misses that matter most."),
    ])
    confusion = detection["confusion"]
    st.caption(
        f"True positives {confusion['tp']}, false positives {confusion['fp']}, "
        f"false negatives {confusion['fn']}, true negatives {confusion['tn']}. "
        f"Base rate in this synthetic population: {detection['actionable_base_rate_pct']}% "
        "actionable, against a small fraction of a percent in a real corridor."
    )

    st.subheader("Per scenario")
    scenarios = pd.DataFrame(detection["per_scenario"])
    figure = px.bar(
        scenarios.sort_values("routed_pct"), x="routed_pct", y="scenario",
        color="is_actionable", orientation="h",
        color_discrete_map={True: "#b3261e", False: "#2e6b3e"},
        labels={"routed_pct": "routed to review %", "is_actionable": "should be caught"},
    )
    st.plotly_chart(neutral_chart_layout(figure, 520), use_container_width=True)
    st.caption(
        "Red bars should be near 100%; green bars are the false-positive cost. "
        "`impossible_travel` sits below the rest for a reason worth knowing: the *first* payment "
        "of an impossible hop carries no evidence until the second one arrives, so per-episode "
        "detection is higher than the per-transaction figure shown here."
    )

    st.subheader("Per rule")
    st.dataframe(pd.DataFrame(detection["per_rule"]), use_container_width=True, hide_index=True,
                 column_config={
                     "precision_pct": st.column_config.ProgressColumn(
                         "On an actionable transaction", format="%.1f%%",
                         min_value=0, max_value=100),
                 })

with tab_ai:
    kpi_row([
        ("Citation resolution", f"{ai_quality['citation_resolution_pct']}%",
         "Citations that resolve to a key the case packet actually contains."),
        ("Ungrounded claims", f"{ai_quality['ungrounded_claim_rate_pct']}%",
         "Findings dropped because they cited nothing real."),
        ("Evidence coverage", f"{ai_quality['evidence_coverage_pct']}%",
         "Signals present on a case that the brief actually explained."),
        ("Abstention rate", f"{ai_quality['abstention_rate_pct']}%",
         "Briefs where the copilot declined to recommend."),
        ("Agreement with the human", f"{ai_quality['ai_human_agreement_pct']}%",
         "Both sides synthetic - see the caveats."),
    ])
    st.caption(
        f"Provider `{ai_quality['provider']}`, model `{ai_quality['model_version']}`, "
        f"prompt `{ai_quality['prompt_version']}`. P95 latency "
        f"{ai_quality['p95_latency_ms']} ms. **These measure the guardrail and workflow layer, "
        "not a language model's writing.**"
    )
    st.markdown(
        f"- The copilot matched ground truth where the simulated human did not: "
        f"**{ai_quality['ai_matched_truth_where_human_did_not']}** cases.\n"
        f"- The simulated human matched ground truth where the copilot did not: "
        f"**{ai_quality['human_matched_truth_where_ai_did_not']}** cases."
    )
    st.info(
        "The second number being larger is the expected and desirable result. The copilot's "
        "suggestion mapping is domain routing logic, not a model that learned anything, and the "
        "product is designed so that being outvoted by a person costs nothing."
    )
    st.dataframe(frames["marts.kpi_ai_agreement"], use_container_width=True, hide_index=True)

with tab_safety:
    gate = safety["input_gate"]
    output = safety["output_gate"]
    kpi_row([
        ("Injection detection", f"{gate['detection_pct']}%",
         f"{gate['detected']} of {gate['attacks']} attack payloads quarantined."),
        ("Benign text passed", f"{gate['benign_pass_pct']}%",
         "A gate that flags ordinary merchant notes is a gate nobody keeps switched on."),
        ("Output gate handled", f"{output['handled_pct']}%",
         f"{output['handled_as_expected']} of {output['scenarios']} adversarial model responses."),
        ("Unauthorised recommendations", str(output["unauthorised_recommendations_allowed"]),
         "Must be zero."),
        ("PII leaks", str(output["pii_leaks"]), "Must be zero."),
        ("Decisions by an AI actor", str(safety["authority"]["decisions_committed_by_ai"]),
         "Must be zero."),
    ])

    if safety["authority"]["ai_decision_attempt_blocked"]:
        st.success(
            "A deliberate attempt to commit a decision as `ai_copilot` was refused:  \n"
            f"`{safety['authority']['block_reason']}`"
        )

    st.subheader("Input gate — prompt injection in merchant and customer free text")
    st.dataframe(pd.DataFrame(gate["cases"]), use_container_width=True, hide_index=True)

    st.subheader("Output gate — a misbehaving model")
    st.caption(
        "Driven by a scripted provider that returns exactly what a jailbroken or hallucinating "
        "model would. A well-behaved provider can never produce these, so this is the only "
        "honest way to measure the gate."
    )
    st.dataframe(pd.DataFrame(output["cases"]), use_container_width=True, hide_index=True)

with tab_money:
    kpi_row([
        ("Settlements recomputed", f"{money['settlements_recomputed']:,}",
         "Independently re-derived from the captured amount, the fee and the booked quote."),
        ("Pipeline agreement", f"{money['settlement_agreement_pct']}%",
         "The independent recomputation and the pipeline flagged the same transactions."),
        ("Replay agreement", f"{money['replay_agreement_pct']}%",
         "Every transaction replayed through the state machine landed on its stored state."),
        ("Illegal events quarantined", f"{money['illegal_events_quarantined']:,}",
         f"{money['illegal_event_pct']}% of the feed - refused rather than absorbed."),
        ("Reproducible from seed", "yes" if money["generator_reproducible"] else "NO",
         "The same seed must produce byte-identical data, or no number here is citable."),
    ])
    st.markdown(
        f"- Ledger over-captures: **{money['ledger_over_captures']}** (must be 0)\n"
        f"- Ledger over-refunds: **{money['ledger_over_refunds']}** (must be 0)\n"
        f"- Negative money columns: **{money['ledger_negative_amounts']}** (must be 0)\n"
        f"- Legal transitions defined in the state machine: **{money['legal_transitions_defined']}**\n"
        f"- FX tolerance: **{money['fx_tolerance_bps']} bps**, worst observed gap "
        f"**{money['worst_observed_bps']} bps**"
    )

with tab_ops:
    kpi_row([
        ("Cases", f"{operations['cases']:,}", ""),
        ("SLA breach", f"{operations['sla_breach_pct']}%", ""),
        ("Median handling", f"{operations['median_handling_minutes']:.0f} min", "Simulated."),
        ("Appeals", f"{operations['appeals_filed']:,}",
         f"{operations['appeals_accepted']} accepted."),
        ("Wrong holds", f"{operations['wrong_holds']:,}",
         "Benign transactions that were held at some point."),
        ("False-positive recovery", f"{operations['false_positive_recovery_pct']}%",
         "Wrong holds overturned on appeal."),
    ])
    st.caption(
        "Every figure on this tab is a property of simulated analyst behaviour with a fixed "
        "error rate. They describe the simulation, not a team."
    )
    st.markdown(f"Audit chain: **{operations['audit_chain_status']}** — "
                f"{operations['audit_chain_summary']}")

st.divider()
st.subheader("Where the boundary sits")
st.dataframe(
    pd.DataFrame([
        {"Task": "Detect a rule violation", "Owner": "Deterministic rules",
         "Why": "Must be reproducible and auditable"},
        {"Task": "Score residual risk", "Owner": "Interpretable model",
         "Why": "Calibrated, explainable, testable"},
        {"Task": "Band the case", "Owner": "Decision policy",
         "Why": "A decision must not depend on a sampled token"},
        {"Task": "Compute money, FX and fees", "Owner": "Integer and decimal arithmetic",
         "Why": "Never delegate arithmetic on money to a language model"},
        {"Task": "Summarise the case", "Owner": "AI copilot", "Why": "Genuine language work"},
        {"Task": "Explain why a signal fired", "Owner": "AI copilot", "Why": "Genuine language work"},
        {"Task": "Find conflicts in the evidence", "Owner": "AI copilot",
         "Why": "Genuine reasoning over text and fields"},
        {"Task": "Name the missing information", "Owner": "AI copilot",
         "Why": "The highest-value AI task here"},
        {"Task": "Recommend an action", "Owner": "AI copilot (advisory, may abstain)",
         "Why": "Advice, not authority"},
        {"Task": "Release / hold / refund / block", "Owner": "Human, or the deterministic policy",
         "Why": "Regulated money movement; irreversible; accountable"},
    ]),
    use_container_width=True, hide_index=True,
)
