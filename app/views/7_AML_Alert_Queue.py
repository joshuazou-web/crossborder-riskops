"""AML Alert Queue - what a team of this size could actually open today.

The page is arranged around the constraint rather than around the alerts. A
queue that shows every case in priority order implies they will all be worked;
this one draws the capacity line explicitly and names what falls below it as
backlog, because pretending otherwise is how a monitoring system quietly becomes
a system that monitors nothing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import streamlit as st  # noqa: E402

from _i18n import t  # noqa: E402
from _shared import (  # noqa: E402
    BAND_COLOUR,
    kpi_row,
    neutral_chart_layout,
    page_setup,
    synthetic_banner,
)
from riskops.aml.typology import TYPOLOGY_BY_ID  # noqa: E402

frames = page_setup()
cases = frames.get("aml.cases", pd.DataFrame())
alerts = frames.get("aml.alerts", pd.DataFrame())

st.title(t("AML Alert Queue"))
st.caption(t(
    "Alerts grouped into cases, ordered by investigation priority, and cut at the review "
    "capacity a team of this size actually has."
))
synthetic_banner()

if cases.empty:
    st.warning(
        t("The AML layer has not been built yet. Run this once, then reload:")
        + "\n\n```\npython -m riskops aml build\n```"
    )
    st.stop()

capacity = int(cases["within_capacity"].sum())
backlog = len(cases) - capacity
open_cases = cases[cases["case_state"].isin(
    ("new", "queued", "investigating", "awaiting_information", "monitoring")
)]

kpi_row([
    (t("Alerts"), f"{len(alerts):,}",
     t("After duplicates were collapsed. Each one names the transfers it rests on.")),
    (t("Cases"), f"{len(cases):,}",
     t("An alert is not a case. A case is one subject over one window.")),
    (t("Within review capacity"), f"{capacity:,}",
     t("What a team of this size could open. Everything else waits.")),
    (t("Backlog"), f"{backlog:,}",
     t("Not cleared and not low-risk — simply not looked at yet.")),
])

st.info(t(
    "**A case below the capacity line has not been cleared.** It has not been reviewed. "
    "Some of them contain patterns the evaluation counts as missed for exactly this reason, "
    "and that number is reported rather than hidden."
), icon="ℹ️")

st.divider()

# --------------------------------------------------------------------------- #
# Shape of the queue
# --------------------------------------------------------------------------- #

left, right = st.columns(2)

with left:
    st.subheader(t("Priority bands"))
    band_counts = (
        cases["priority_band"].value_counts()
        .reindex(["critical", "high", "medium", "low"]).fillna(0).reset_index()
    )
    band_counts.columns = ["band", "cases"]
    figure = px.bar(
        band_counts, x="band", y="cases", color="band",
        color_discrete_map=BAND_COLOUR,
    )
    figure.update_layout(showlegend=False)
    st.plotly_chart(neutral_chart_layout(figure, height=280), use_container_width=True)
    st.caption(t(
        "Band cuts were calibrated against this seeded dataset, not taken from anywhere "
        "external. A different population would need them re-cut."
    ))

with right:
    st.subheader(t("Which typologies raised the alerts"))
    exploded = (
        cases["typologies"].astype(str).str.split("|").explode().str.strip()
    )
    exploded = exploded[exploded != ""]
    counts = exploded.value_counts().reset_index()
    counts.columns = ["typology_id", "cases"]
    counts["typology"] = counts["typology_id"].map(
        lambda k: t(TYPOLOGY_BY_ID[k].title) if k in TYPOLOGY_BY_ID else k
    )
    figure = px.bar(counts, x="cases", y="typology", orientation="h")
    figure.update_layout(yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(neutral_chart_layout(figure, height=280), use_container_width=True)
    st.caption(t(
        "A case can carry more than one typology. Several independent typologies on one "
        "account is the strongest thing this system is able to say."
    ))

st.divider()

# --------------------------------------------------------------------------- #
# The queue itself
# --------------------------------------------------------------------------- #

st.subheader(t("The queue"))

filters = st.columns([1, 1, 1, 2])
with filters[0]:
    band_filter = st.multiselect(
        t("Priority band"), ["critical", "high", "medium", "low"], default=[],
        format_func=lambda b: t(b),
    )
with filters[1]:
    state_filter = st.multiselect(
        t("Case state"), sorted(cases["case_state"].unique()), default=[],
        format_func=lambda s: t(s),
    )
with filters[2]:
    scope = st.radio(
        t("Show"), ["within_capacity", "backlog", "all"], index=0, horizontal=False,
        format_func=lambda s: {
            "within_capacity": t("Within capacity"),
            "backlog": t("Backlog"),
            "all": t("All cases"),
        }[s],
    )
with filters[3]:
    search = st.text_input(t("Account or case id"), "")

view = cases.copy()
if band_filter:
    view = view[view["priority_band"].isin(band_filter)]
if state_filter:
    view = view[view["case_state"].isin(state_filter)]
if scope == "within_capacity":
    view = view[view["within_capacity"]]
elif scope == "backlog":
    view = view[~view["within_capacity"]]
if search.strip():
    needle = search.strip().upper()
    view = view[
        view["case_id"].astype(str).str.upper().str.contains(needle)
        | view["subject_id"].astype(str).str.upper().str.contains(needle)
    ]

view = view.sort_values("queue_position")

st.caption(
    t("Showing {shown} of {total} cases.").format(shown=f"{len(view):,}", total=f"{len(cases):,}")
)

display = pd.DataFrame({
    t("#"): view["queue_position"],
    t("Case"): view["case_id"],
    t("Account"): view["subject_id"],
    t("Priority"): view["priority_score"].round(3),
    t("Band"): view["priority_band"].map(t),
    t("Typologies"): view["typology_count"],
    t("Alerts"): view["alert_count"],
    t("Transfers"): view["transfer_count"],
    t("Amount (USD)"): (view["total_usd_minor"] / 100).round(0),
    t("State"): view["case_state"].map(t),
    t("In capacity"): view["within_capacity"],
})
st.dataframe(display, use_container_width=True, hide_index=True, height=420)

st.caption(t(
    "Open a case in the Investigation Workbench to see its evidence, what argues against it, "
    "and what is missing."
))
