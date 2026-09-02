"""Overview - what the payment estate looks like and where the work is."""

import pandas as pd
import plotly.express as px
import streamlit as st

from _i18n import t  # noqa: E402
from _shared import kpi_row, neutral_chart_layout, page_setup, synthetic_banner

frames = page_setup("Overview")

overview = frames["marts.kpi_overview"].iloc[0]
transactions = frames["marts.fct_transactions"]
cases = frames["marts.fct_cases"]
quality = frames["marts.kpi_data_quality"].iloc[0]

st.title(t("Overview"))
st.caption(t(
    "One synthetic cross-border payment estate, the risk signals it produced, and the case "
    "queue that came out of it."
))
synthetic_banner()

# --- volume and money ------------------------------------------------------
kpi_row([
    (t("Transactions"), f"{int(overview['transactions']):,}",
     t("Every payment in the reporting window.")),
    (t("Cross-border"), f"{int(overview['cross_border_transactions']):,}",
     t("Payer and merchant in different countries, or two currencies involved.")),
    (t("Payment events"), f"{int(overview['payment_events']):,}",
     t("Individual lifecycle events replayed through the state machine.")),
    (t("Quarantined events"), f"{int(overview['quarantined_events']):,}",
     t("Events the state machine refused as illegal for the state they arrived in. "
       "They never reached a ledger.")),
    (t("Reconciliation breaks"), f"{int(overview['reconciliation_breaks']):,}",
     t("Money invariants that did not hold: FX, fees, authorisation gaps, double credits.")),
])

st.divider()

# --- the queue -------------------------------------------------------------
st.subheader(t("Risk operations"))
kpi_row([
    (t("Risk signals"), f"{int(overview['signals']):,}",
     t("Fired by deterministic rules. No language model participates in detection.")),
    (t("Cases opened"), f"{int(overview['cases']):,}",
     t("One per transaction the policy did not auto-release.")),
    (t("Auto-released"), f"{overview['auto_release_pct']:.1f}%",
     t("Nothing fired above low severity and residual risk was under threshold. "
       "No human looked at these, and the policy recorded why.")),
    (t("Routed to a person"), f"{overview['manual_review_pct']:.1f}%",
     t("The manual review rate - the cost side of the detection trade-off.")),
    (t("Median handling"), f"{overview['median_handling_minutes']:.0f} min",
     t("Simulated analyst handling time, opened to resolved.")),
    (t("P90 handling"), f"{overview['p90_handling_minutes']:.0f} min",
     t("The tail is what breaks an operations team, not the median.")),
])

st.caption(t(
    "Handling times and every human decision behind them are **simulated** with a fixed error "
    "rate. They describe the simulation's parameters, not real reviewer behaviour."
))

st.divider()

left, right = st.columns([3, 2])

with left:
    st.subheader(t("Volume and queue load"))
    daily = frames["marts.kpi_daily_volume"].copy()
    daily["created_date"] = pd.to_datetime(daily["created_date"])
    melted = daily.melt(
        id_vars="created_date",
        value_vars=["transactions", "routed_to_review"],
        var_name="series", value_name="count",
    )
    melted["series"] = melted["series"].map({
        "transactions": "Transactions", "routed_to_review": "Routed to review",
    })
    # Lines, not a stacked area. "Routed to review" is a *subset* of
    # "Transactions", and stacking them would draw the subset on top of the whole
    # and make the queue look larger than the traffic feeding it.
    figure = px.line(melted, x="created_date", y="count", color="series",
                     color_discrete_map={"Transactions": "#6b7c93",
                                         "Routed to review": "#c25c00"})
    figure.update_traces(line=dict(width=1.6))
    figure.update_layout(xaxis_title=None, yaxis_title=None, legend_title=None)
    st.plotly_chart(neutral_chart_layout(figure, 300), use_container_width=True)

with right:
    st.subheader(t("Where the policy sends things"))
    mix = frames["marts.kpi_policy_mix"].groupby("policy_action", as_index=False)[
        "transactions"].sum().sort_values("transactions", ascending=False)
    figure = px.bar(mix, x="transactions", y="policy_action", orientation="h",
                    color="policy_action",
                    color_discrete_map={
                        "auto_release": "#2e6b3e", "manual_review": "#8a6d00",
                        "request_information": "#4a6fa5", "auto_hold": "#b3261e"})
    figure.update_layout(xaxis_title=None, yaxis_title=None, showlegend=False)
    st.plotly_chart(neutral_chart_layout(figure, 300), use_container_width=True)
    st.caption(t(
        "`auto_hold` stops the payment immediately, and still opens a case: the money is "
        "safe by default, but the *outcome* is a person's to confirm."
    ))

st.divider()

left, right = st.columns(2)

with left:
    st.subheader(t("Payment lifecycle"))
    states = frames["marts.kpi_state_mix"]
    figure = px.bar(states, x="transactions", y="payment_state", orientation="h",
                    text="share_pct")
    figure.update_traces(marker_color="#4a6fa5", texttemplate="%{text}%", textposition="outside")
    figure.update_layout(xaxis_title=None, yaxis_title=None)
    st.plotly_chart(neutral_chart_layout(figure, 340), use_container_width=True)

with right:
    st.subheader(t("Busiest cross-border corridors"))
    corridors = frames["marts.kpi_corridor"]
    # Domestic pairs are the largest single buckets by construction - one country
    # against itself concentrates volume that cross-border pairs spread over a
    # dozen destinations. Showing them here would bury the corridors this product
    # is about, so the chart is explicitly the cross-border view.
    corridors = corridors[corridors["is_cross_border"]].sort_values(
        "transactions", ascending=False).head(12)
    figure = px.bar(corridors, x="transactions", y="corridor", orientation="h",
                    color="case_rate_pct", color_continuous_scale="Oranges",
                    labels={"case_rate_pct": "case rate %"})
    figure.update_layout(xaxis_title=None, yaxis_title=None)
    st.plotly_chart(neutral_chart_layout(figure, 340), use_container_width=True)
    st.caption(t("Colour is the share of that corridor's payments that opened a case."))

st.divider()

st.subheader(t("Which rules are doing the work"))
signals = frames["marts.kpi_signal_frequency"].copy()
signals = signals.sort_values("fired", ascending=False)
st.dataframe(
    signals[["rule_id", "signal_family", "severity", "fired", "transactions",
             "precision_pct", "title"]],
    use_container_width=True, hide_index=True,
    column_config={
        "rule_id": st.column_config.TextColumn("Rule", width="medium"),
        "signal_family": st.column_config.TextColumn("Family"),
        "severity": st.column_config.TextColumn("Severity", width="small"),
        "fired": st.column_config.NumberColumn("Fired", format="%d"),
        "transactions": st.column_config.NumberColumn("Transactions", format="%d"),
        "precision_pct": st.column_config.ProgressColumn(
            "On an actionable transaction", format="%.1f%%", min_value=0, max_value=100,
        ),
        "title": st.column_config.TextColumn("What it means", width="large"),
    },
)
st.caption(t(
    "A low percentage is not automatically a bad rule. `R301_GEO_MISMATCH` is deliberately "
    "low-severity: most people paying from another country are on holiday, and it earns its place "
    "only in combination with something else."
))

st.divider()

left, right = st.columns(2)
with left:
    st.subheader(t("Feed health"))
    kpi_row([
        (t("Events quarantined"), f"{quality['quarantine_pct']:.2f}%",
         t("Illegal transitions the state machine refused.")),
        (t("No device fingerprint"), f"{quality['missing_device_pct']:.1f}%",
         t("These cannot be decided without asking someone.")),
    ])
    checks = frames["audit.validation_results"]
    if not checks.empty:
        latest_batch = checks.sort_values("checked_at").iloc[-1]["batch_id"]
        latest = checks[checks["batch_id"] == latest_batch]
        st.dataframe(
            latest[["check_name", "severity", "passed", "observed", "threshold", "detail"]]
            .sort_values(["passed", "severity"]),
            use_container_width=True, hide_index=True, height=260,
            column_config={
                "check_name": st.column_config.TextColumn("Check", width="medium"),
                "detail": st.column_config.TextColumn("Detail", width="large"),
            },
        )

with right:
    st.subheader(t("Reconciliation breaks"))
    breaks = frames["marts.kpi_reconciliation"]
    st.dataframe(breaks, use_container_width=True, hide_index=True,
                 column_config={
                     "break_type": st.column_config.TextColumn("Break", width="medium"),
                     "median_abs_bps": st.column_config.NumberColumn("Median |bps|"),
                     "max_abs_bps": st.column_config.NumberColumn("Worst |bps|"),
                 })
    st.caption(t(
        "Each break is a money invariant that failed, computed with integer and decimal "
        "arithmetic - never floating point. The rule engine reads these rows rather than "
        "recomputing the money, so a break always means exactly one thing."
    ))
