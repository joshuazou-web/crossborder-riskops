"""AML Operations Overview - the shape of the workload, for whoever staffs it.

Written for a risk-operations lead rather than an investigator: how much is
arriving, how much of it can actually be worked, what is aging, and which rules
are producing the volume. Every figure is computed from the tables the pipeline
wrote for this seeded dataset; none is a benchmark, an industry figure, or a
claim about any real institution.
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
transfers = frames.get("aml.transfers", pd.DataFrame())
decisions = frames.get("aml.investigation_decisions", pd.DataFrame())

st.title(t("AML Operations Overview"))
st.caption(t(
    "Cross-border transfer volume, the alerts it produced, what aggregation did to them, and "
    "how much of the result a team of this size can actually work."
))
synthetic_banner()

if cases.empty or transfers.empty:
    st.warning(
        t("The AML layer has not been built yet. Run this once, then reload:")
        + "\n\n```\npython -m riskops aml build\n```"
    )
    st.stop()

cross_border = int(transfers["is_cross_border"].sum())
capacity = int(cases["within_capacity"].sum())
open_states = ("new", "queued", "investigating", "awaiting_information", "monitoring")
open_cases = cases[cases["case_state"].isin(open_states)]

kpi_row([
    (t("Transfers"), f"{len(transfers):,}",
     t("Synthetic cross-border remittances generated from a fixed seed.")),
    (t("Cross-border"), f"{cross_border:,}",
     t("Payer and beneficiary accounts in different countries.")),
    (t("Alerts"), f"{len(alerts):,}", t("After duplicate firings were collapsed.")),
    (t("Cases"), f"{len(cases):,}", t("Alerts grouped by subject and window.")),
])

kpi_row([
    (t("Open cases"), f"{len(open_cases):,}", t("Not yet dispositioned.")),
    (t("Review capacity"), f"{capacity:,}",
     t("How many a team of this size could open. The rest is backlog.")),
    (t("Backlog"), f"{len(cases) - capacity:,}",
     t("Unreviewed — not cleared, and not judged low-risk.")),
    (t("Decisions recorded"), f"{len(decisions):,}",
     t("Every one by a named person with a written reason.")),
])

st.divider()

# --------------------------------------------------------------------------- #
# What aggregation actually did
# --------------------------------------------------------------------------- #

st.subheader(t("From alerts to a workable queue"))
st.caption(t(
    "The number that matters operationally is not how many alerts fired, but how many "
    "separate things a person has to open."
))

duplicates_absorbed = int(alerts["duplicate_count"].fillna(0).sum())
raw_total = len(alerts) + duplicates_absorbed
funnel = pd.DataFrame({
    "stage": [
        t("Raw alert firings"),
        t("After duplicate collapse"),
        t("After grouping into cases"),
        t("Within review capacity"),
    ],
    "count": [raw_total, len(alerts), len(cases), capacity],
})
figure = px.bar(funnel, x="count", y="stage", orientation="h", text="count")
figure.update_layout(yaxis={"categoryorder": "array",
                            "categoryarray": funnel["stage"].tolist()[::-1]})
st.plotly_chart(neutral_chart_layout(figure, height=280), use_container_width=True)

ratios = st.columns(3)
ratios[0].metric(
    t("Duplicate reduction"),
    f"{duplicates_absorbed / raw_total * 100:.1f}%" if raw_total else "—",
    help=t("Share of raw firings that were repeats of a finding already reported."),
)
ratios[1].metric(
    t("Alerts per case"), f"{len(alerts) / len(cases):.2f}",
    help=t("1.00 would mean aggregation did nothing."),
)
ratios[2].metric(
    t("Multi-typology cases"), f"{int((cases['typology_count'] > 1).sum()):,}",
    help=t("Cases where independent typologies corroborate each other."),
)

st.divider()

# --------------------------------------------------------------------------- #
# Where the risk sits
# --------------------------------------------------------------------------- #

left, right = st.columns(2)

with left:
    st.subheader(t("Alerts by typology"))
    counts = alerts["typology_id"].value_counts().reset_index()
    counts.columns = ["typology_id", "alerts"]
    counts["typology"] = counts["typology_id"].map(
        lambda k: t(TYPOLOGY_BY_ID[k].title) if k in TYPOLOGY_BY_ID else k
    )
    figure = px.bar(counts, x="alerts", y="typology", orientation="h")
    figure.update_layout(yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(neutral_chart_layout(figure, height=300), use_container_width=True)
    st.caption(t(
        "A rule producing most of the volume is not necessarily the most useful rule — check "
        "it against per-typology recall in the evaluation before tuning it."
    ))

with right:
    st.subheader(t("Priority bands"))
    band_counts = (
        cases["priority_band"].value_counts()
        .reindex(["critical", "high", "medium", "low"]).fillna(0).reset_index()
    )
    band_counts.columns = ["band", "cases"]
    band_counts["label"] = band_counts["band"].map(t)
    figure = px.bar(band_counts, x="label", y="cases", color="band",
                    color_discrete_map=BAND_COLOUR)
    figure.update_layout(showlegend=False)
    st.plotly_chart(neutral_chart_layout(figure, height=300), use_container_width=True)

st.divider()

# --------------------------------------------------------------------------- #
# Corridors, channels and currencies
# --------------------------------------------------------------------------- #

st.subheader(t("Where the money moves"))
tabs = st.tabs([t("Corridors"), t("Channels"), t("Currencies"), t("Alert rate by corridor")])

alerted_transfers: set[str] = set()
for value in alerts["transfer_ids"]:
    alerted_transfers |= {x for x in str(value).split("|") if x}
transfers = transfers.assign(
    corridor=transfers["origin_country"] + " → " + transfers["destination_country"],
    alerted=transfers["transaction_id"].isin(alerted_transfers),
    usd=transfers["normalized_amount_usd_minor"] / 100,
)

with tabs[0]:
    top = (
        transfers.groupby("corridor", as_index=False)
        .agg(transfers=("transaction_id", "size"), usd=("usd", "sum"))
        .sort_values("usd", ascending=False).head(18)
    )
    figure = px.bar(top, x="usd", y="corridor", orientation="h")
    figure.update_layout(yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(neutral_chart_layout(figure, height=420), use_container_width=True)

with tabs[1]:
    by_channel = transfers.groupby("channel", as_index=False).agg(
        transfers=("transaction_id", "size"),
        alerted=("alerted", "sum"),
        usd=("usd", "sum"),
    )
    by_channel["alert_rate"] = (by_channel["alerted"] / by_channel["transfers"]).round(4)
    st.dataframe(
        pd.DataFrame({
            t("Channel"): by_channel["channel"],
            t("Transfers"): by_channel["transfers"],
            t("In an alert"): by_channel["alerted"],
            t("Alert rate"): by_channel["alert_rate"],
            t("USD"): by_channel["usd"].round(0),
        }),
        use_container_width=True, hide_index=True,
    )
    st.caption(t(
        "The agent cash-in channel carries more incomplete beneficiary data by construction, "
        "which is why the missing-information typology names the channel as counter-evidence."
    ))

with tabs[2]:
    by_currency = (
        transfers.groupby("currency", as_index=False)
        .agg(transfers=("transaction_id", "size"), usd=("usd", "sum"))
        .sort_values("usd", ascending=False)
    )
    figure = px.bar(by_currency, x="currency", y="usd")
    st.plotly_chart(neutral_chart_layout(figure, height=320), use_container_width=True)
    st.caption(t(
        "Amounts are compared on one normalised USD scale throughout, never on the "
        "presentment amount — otherwise a JPY transfer looks a hundred times larger than "
        "an equivalent USD one."
    ))

with tabs[3]:
    rate = (
        transfers.groupby("corridor", as_index=False)
        .agg(transfers=("transaction_id", "size"), alerted=("alerted", "sum"))
    )
    rate = rate[rate["transfers"] >= 30]
    rate["alert_rate"] = (rate["alerted"] / rate["transfers"]).round(4)
    rate = rate.sort_values("alert_rate", ascending=False).head(18)
    figure = px.bar(rate, x="alert_rate", y="corridor", orientation="h")
    figure.update_layout(yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(neutral_chart_layout(figure, height=420), use_container_width=True)
    st.caption(t(
        "Corridors with fewer than 30 transfers are excluded: a 100% alert rate over two "
        "transfers is noise, and showing it would invite a conclusion about a country."
    ))

st.divider()

# --------------------------------------------------------------------------- #
# Aging
# --------------------------------------------------------------------------- #

st.subheader(t("What is aging"))
aging = cases.copy()
aging["days_open"] = (
    pd.Timestamp.now().normalize() - pd.to_datetime(aging["opened_at"])
).dt.days.clip(lower=0)
buckets = pd.cut(
    aging["days_open"], bins=[-1, 1, 7, 30, 10_000],
    labels=[t("under a day"), t("1–7 days"), t("8–30 days"), t("over 30 days")],
)
aged = buckets.value_counts().reindex(buckets.cat.categories).reset_index()
aged.columns = ["bucket", "cases"]
figure = px.bar(aged, x="bucket", y="cases")
st.plotly_chart(neutral_chart_layout(figure, height=280), use_container_width=True)
st.caption(t(
    "A case opens when its most recent alert fired, not when the batch ran. Queue-waiting "
    "time is one of the eight factors in investigation priority, so an old case eventually "
    "rises whatever else it scores."
))
