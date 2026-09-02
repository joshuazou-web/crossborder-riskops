"""Audit Log - who did what, when, and whether the record can be trusted."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plotly.express as px  # noqa: E402
import streamlit as st  # noqa: E402

from _shared import kpi_row, neutral_chart_layout, page_setup, synthetic_banner  # noqa: E402
from riskops.audit.log import AuditLog  # noqa: E402
from riskops.config import get_settings  # noqa: E402
from riskops.db import session  # noqa: E402

frames = page_setup("Audit Log", "🔐")
entries = frames["audit.audit_log"]
decisions = frames["audit.decisions"]
invocations = frames["audit.ai_invocations"]

st.title("Audit Log")
st.caption("Append-only and hash-chained. Nothing here is ever updated or deleted.")
synthetic_banner()

# Read-write like every other connection in this app; see app/_shared.py.
with session(get_settings()) as con:
    chain = AuditLog(con).verify_chain()

if chain.status == "verified":
    st.success(f"**Chain verified.** {chain.summary()}")
elif chain.status == "unverifiable":
    st.warning(f"**Unverifiable.** {chain.summary()}")
else:
    st.error(f"**Chain broken.** {chain.summary()}")

st.caption(
    "What this proves: each entry hashes the previous entry's digest, so editing entry *N* "
    "invalidates *N* and everything after it — a partial edit is detectable. "
    "**What it does not prove:** someone who can rewrite the whole table can recompute every "
    "link. Real non-repudiation needs the head anchored somewhere the editor does not control. "
    "This project does not do that, and says so rather than implying a guarantee it has not earned."
)

st.divider()

ai_decisions = int((decisions["actor_role"] == "ai_copilot").sum()) if not decisions.empty else 0
kpi_row([
    ("Audit entries", f"{len(entries):,}", "Every consequential act."),
    ("Human decisions", f"{len(decisions):,}", "Committed outcomes with a reason code."),
    ("AI briefs", f"{len(invocations):,}", "Advisory only."),
    ("Decisions by an AI actor", str(ai_decisions),
     "This must be zero. `audit.log.record_decision` refuses any AI actor."),
    ("Distinct actors", f"{entries['actor_id'].nunique() if not entries.empty else 0}",
     "People, the policy engine and the pipeline."),
])

if ai_decisions:
    st.error("An AI actor appears on a committed decision. That is a contract violation.")
else:
    st.success(
        "No AI actor appears on any committed decision — enforced in code, not by convention."
    )

st.divider()

left, right = st.columns([3, 2])

with left:
    st.subheader("Where the copilot and the human disagreed")
    if decisions.empty:
        st.caption("No decisions yet.")
    else:
        compared = decisions[decisions["ai_recommended_action"].astype(str) != ""]
        if compared.empty:
            st.caption("No decision has an AI recommendation to compare against.")
        else:
            matrix = (
                compared.groupby(["ai_recommended_action", "action"], as_index=False)
                .size().rename(columns={"size": "decisions"})
            )
            figure = px.density_heatmap(
                matrix, x="action", y="ai_recommended_action", z="decisions",
                color_continuous_scale="Blues", text_auto=True,
            )
            figure.update_layout(xaxis_title="Human decided", yaxis_title="AI suggested")
            st.plotly_chart(neutral_chart_layout(figure, 330), use_container_width=True)
            agreement = float((compared["ai_recommended_action"] == compared["action"]).mean())
            st.caption(
                f"Agreement: **{agreement:.1%}**. Both sides of this comparison are synthetic — "
                "the copilot is a deterministic mock by default, and the human decisions are "
                "simulated with a fixed error rate. It measures consistency between two "
                "synthetic processes, not human trust in a model."
            )

with right:
    st.subheader("Guardrail outcomes")
    guardrails = frames["marts.kpi_ai_guardrails"]
    if guardrails.empty:
        st.caption("No AI invocations recorded.")
    else:
        st.dataframe(guardrails, use_container_width=True, hide_index=True,
                     column_config={
                         "guardrail_verdict": st.column_config.TextColumn("Output gate"),
                         "injection_verdict": st.column_config.TextColumn("Input gate"),
                         "mean_latency_ms": st.column_config.NumberColumn("Mean ms", format="%.0f"),
                     })
        quarantined = int(
            (invocations["injection_verdict"] == "quarantined").sum()
        ) if not invocations.empty else 0
        st.caption(
            f"{quarantined} brief(s) were generated from a case whose merchant free text was "
            "quarantined as untrusted before the model saw it."
        )

st.divider()

st.subheader("The log")
with st.container(border=True):
    columns = st.columns([1, 1, 1, 2])
    role = columns[0].multiselect("Actor role", sorted(entries["actor_role"].unique()))
    action = columns[1].multiselect("Action", sorted(entries["action"].unique()))
    object_type = columns[2].multiselect("Object", sorted(entries["object_type"].unique()))
    search = columns[3].text_input("Search summary or object id", placeholder="CASE_ or TXN_")

view = entries.copy()
if role:
    view = view[view["actor_role"].isin(role)]
if action:
    view = view[view["action"].isin(action)]
if object_type:
    view = view[view["object_type"].isin(object_type)]
if search:
    needle = search.strip().lower()
    view = view[
        view["summary"].str.lower().str.contains(needle)
        | view["object_id"].str.lower().str.contains(needle)
    ]

view = view.sort_values("seq", ascending=False)
st.caption(f"{len(view):,} of {len(entries):,} entries")

st.dataframe(
    view[["seq", "occurred_at", "actor_role", "actor_id", "action", "object_type", "object_id",
          "summary", "entry_hash"]].head(600),
    use_container_width=True, hide_index=True, height=420,
    column_config={
        "seq": st.column_config.NumberColumn("#", format="%d", width="small"),
        "occurred_at": st.column_config.DatetimeColumn("When", format="YYYY-MM-DD HH:mm:ss"),
        "summary": st.column_config.TextColumn("Summary", width="large"),
        "entry_hash": st.column_config.TextColumn("Hash", width="medium"),
    },
)

st.download_button(
    "Export the filtered log as CSV",
    view.to_csv(index=False).encode("utf-8"),
    file_name="audit_log.csv",
    mime="text/csv",
)

st.divider()
st.subheader("Refresh history")
st.dataframe(
    frames["audit.refresh_log"].sort_values("started_at", ascending=False),
    use_container_width=True, hide_index=True,
)
st.caption(
    "Every build records its seed and the taxonomy, rules and model versions that produced it. "
    "A metric without its versions is not reproducible."
)
