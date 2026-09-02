"""Case Queue - the work, ordered the way an operations team would take it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plotly.express as px  # noqa: E402
import streamlit as st  # noqa: E402

from _i18n import t  # noqa: E402
from _shared import kpi_row, neutral_chart_layout, page_setup, synthetic_banner  # noqa: E402

frames = page_setup()
cases = frames["marts.fct_cases"]

st.title(t("Case Queue"))
st.caption(t(
    "Every case the deterministic policy could not close on its own, with the reason it exists "
    "and the clock it is running against."
))
synthetic_banner()

open_cases = cases[cases["is_open"]]
kpi_row([
    (t("Open cases"), f"{len(open_cases):,}", t("Not yet resolved.")),
    (t("Critical + high"), f"{int(open_cases['risk_band'].isin(['critical', 'high']).sum()):,}",
     t("The part of the queue that cannot wait.")),
    (t("Past SLA"), f"{int(open_cases['sla_breached'].sum()):,}",
     t("SLA is set by risk band: 2h critical, 8h high, 24h medium, 72h low.")),
    (t("Awaiting information"),
     f"{int((open_cases['case_state'] == 'awaiting_information').sum()):,}",
     t("Blocked on a person, not on an analyst.")),
    (t("Appealed"), f"{int(cases['appeal_count'].sum()):,}",
     t("Payers and merchants contesting a resolved case.")),
    (t("Overturned holds"), f"{int(cases['is_false_positive'].sum()):,}",
     t("Holds an appeal proved wrong. This number existing at all is the point.")),
])

st.divider()

left, right = st.columns([2, 3])

with left:
    st.subheader(t("Queue shape"))
    shape = (
        cases.groupby(["risk_band", "case_state"], as_index=False)
        .size().rename(columns={"size": "cases"})
    )
    figure = px.bar(shape, x="cases", y="risk_band", color="case_state", orientation="h")
    figure.update_layout(xaxis_title=None, yaxis_title=None, legend_title=None)
    st.plotly_chart(neutral_chart_layout(figure, 300), use_container_width=True)

with right:
    st.subheader(t("SLA by risk band"))
    sla = frames["marts.kpi_sla"]
    st.dataframe(
        sla, use_container_width=True, hide_index=True,
        column_config={
            "risk_band": st.column_config.TextColumn("Band"),
            "cases": st.column_config.NumberColumn("Cases", format="%d"),
            "breached": st.column_config.NumberColumn("Breached", format="%d"),
            "breach_pct": st.column_config.ProgressColumn(
                "Breach rate", format="%.1f%%", min_value=0, max_value=100),
            "median_handling_minutes": st.column_config.NumberColumn("Median (min)", format="%.0f"),
            "p90_handling_minutes": st.column_config.NumberColumn("P90 (min)", format="%.0f"),
        },
    )
    st.caption(t(
        "Handling times come from **simulated** analyst behaviour with a fixed error rate, not "
        "from observed human work."
    ))

st.divider()

# --- filters ---------------------------------------------------------------
with st.container(border=True):
    columns = st.columns([1, 1, 1, 1, 1])
    band = columns[0].multiselect(t("Risk band"), ["critical", "high", "medium", "low"])
    state = columns[1].multiselect(t("Case state"), sorted(cases["case_state"].unique()))
    family = columns[2].multiselect(t("Reason family"),
                                    sorted(cases["primary_reason_family"].unique()))
    owner = columns[3].multiselect(t("Owning role"), sorted(cases["assigned_role"].unique()))
    scope = columns[4].selectbox(
        t("Scope"),
        ["Open only", "All cases", "Breached SLA", "Appealed"],
        format_func=t,
    )

view = cases.copy()
if scope == "Open only":
    view = view[view["is_open"]]
elif scope == "Breached SLA":
    view = view[view["sla_breached"]]
elif scope == "Appealed":
    view = view[view["appeal_count"] > 0]
if band:
    view = view[view["risk_band"].isin(band)]
if state:
    view = view[view["case_state"].isin(state)]
if family:
    view = view[view["primary_reason_family"].isin(family)]
if owner:
    view = view[view["assigned_role"].isin(owner)]

order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
view = view.assign(_order=view["risk_band"].map(order)).sort_values(
    ["_order", "sla_due_at"]
).drop(columns="_order")

st.caption(f"{len(view):,} cases")

selection = st.dataframe(
    view[[
        "case_id", "transaction_id", "opened_at", "risk_band", "risk_score", "case_state",
        "primary_reason_family", "max_severity", "signal_count", "assigned_role", "sla_due_at",
        "sla_breached", "ai_recommended_action", "ai_confidence", "resolution_action",
        "corridor", "captured_usd",
    ]].head(500),
    use_container_width=True, hide_index=True, height=420,
    on_select="rerun", selection_mode="single-row",
    column_config={
        "case_id": st.column_config.TextColumn("Case", width="small"),
        "transaction_id": st.column_config.TextColumn("Transaction", width="small"),
        "opened_at": st.column_config.DatetimeColumn("Opened", format="YYYY-MM-DD HH:mm"),
        "risk_score": st.column_config.NumberColumn("Score", format="%.2f"),
        "primary_reason_family": st.column_config.TextColumn("Reason family", width="medium"),
        "signal_count": st.column_config.NumberColumn("Signals", format="%d"),
        "sla_due_at": st.column_config.DatetimeColumn("SLA due", format="YYYY-MM-DD HH:mm"),
        "sla_breached": st.column_config.CheckboxColumn("Past SLA"),
        "ai_recommended_action": st.column_config.TextColumn("AI suggests", width="small"),
        "ai_confidence": st.column_config.NumberColumn("AI conf.", format="%.2f"),
        "resolution_action": st.column_config.TextColumn("Decided", width="small"),
        "captured_usd": st.column_config.NumberColumn("≈ USD", format="%.0f"),
    },
)

st.caption(t(
    "`AI suggests` is advisory and carries no authority. The column exists next to `Decided` on "
    "purpose: the gap between them is a number this product reports rather than hides."
))

rows = selection.get("selection", {}).get("rows", []) if selection else []
if rows:
    chosen = view.iloc[rows[0]]
    st.session_state["selected_case_id"] = str(chosen["case_id"])
    st.success(
        f"Case **{chosen['case_id']}** selected. Open **Case Detail** in the sidebar to work it."
    )

st.divider()
left, right = st.columns(2)
with left:
    st.subheader(t("Outcomes and reason codes"))
    st.dataframe(frames["marts.kpi_case_outcomes"], use_container_width=True, hide_index=True)
with right:
    st.subheader(t("Appeals"))
    appeals = frames["marts.kpi_appeals"]
    if appeals.empty:
        st.caption(t("No appeals in this dataset."))
    else:
        st.dataframe(appeals, use_container_width=True, hide_index=True)
    st.caption(t(
        "An accepted appeal closes the case as `closed_false_positive` - the only state that "
        "counts toward recovery. A wrong hold that nobody records is a wrong hold nobody fixes."
    ))
