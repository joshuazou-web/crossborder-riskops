"""Policy Tuning - the thresholds as a product decision, not a config value.

`auto_release_below` and `auto_hold_at_or_above` are usually buried in a config
file, which quietly turns the most consequential product choice in the system
into an engineering detail. They decide how much traffic a person has to look
at, how much actionable traffic slips past unlooked-at, and how many people get
their payment stopped by a machine before anyone has read the case.

So they belong on a screen, with the cost of each setting visible while you move
it. Every number here is recomputed by the same `policy.decide` the pipeline
runs, over the signals and scores already in the warehouse - nothing on this
page is an approximation of the real policy.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from _shared import kpi_row, neutral_chart_layout, page_setup, synthetic_banner  # noqa: E402
from riskops.config import get_settings  # noqa: E402
from riskops.eval.robustness import policy_mix, sweep_thresholds  # noqa: E402

frames = page_setup("Policy Tuning", "🎚️")
settings = get_settings()

transactions = frames["core.transactions"]
signals = frames["risk.signals"]
scores = frames["risk.model_scores"]

st.title("Policy Tuning")
st.caption(
    "Where the two automatic thresholds sit, and what each setting costs. "
    "Recomputed live over all "
    f"{len(transactions):,} transactions using the same decision policy the pipeline runs."
)
synthetic_banner()

st.info(
    "**This is the only screen where a product decision is made rather than a case decision.** "
    "Moving these thresholds does not touch the warehouse — it shows you what *would* have "
    "happened. Committing a change means setting `RISKOPS_AUTO_RELEASE_BELOW` / "
    "`RISKOPS_AUTO_HOLD_AT` and rebuilding, so the change is versioned and appears in the "
    "refresh log rather than being made silently in a UI.",
    icon="🎚️",
)

# --- the two levers ---------------------------------------------------------
with st.container(border=True):
    left, right = st.columns(2)
    release_below = left.slider(
        "Auto-release below", min_value=0.05, max_value=0.75,
        value=float(settings.auto_release_below), step=0.01,
        help="Under this combined score, and with nothing above low severity, the payment is "
             "released without a human. Raising it frees analyst time and lets more actionable "
             "traffic through unlooked-at.",
    )
    hold_at = right.slider(
        "Auto-hold at or above", min_value=0.50, max_value=1.00,
        value=float(settings.auto_hold_at_or_above), step=0.01,
        help="At or above this, the machine stops the payment before anyone reads the case. "
             "The case still opens and a person still confirms the outcome — but the payer's "
             "money is already stopped, so a wrong setting here has a real cost to real people.",
    )
    left.caption(f"shipped default: `{settings.auto_release_below}`")
    right.caption(f"shipped default: `{settings.auto_hold_at_or_above}`")

current = policy_mix(settings, transactions, signals, scores,
                     release_below, hold_at)
shipped = policy_mix(settings, transactions, signals, scores,
                     settings.auto_release_below, settings.auto_hold_at_or_above)


def delta(key: str, invert: bool = False) -> str | None:
    """Difference against the shipped setting, or nothing when unchanged."""
    change = round(float(current[key]) - float(shipped[key]), 2)
    if change == 0:
        return None
    return f"{change:+}" if not invert else f"{change:+}"


st.subheader("What this setting produces")
kpi_row([
    ("Recall", f"{current['recall_pct']}%",
     "Actionable transactions the policy routed to a person or held."),
    ("Precision", f"{current['precision_pct']}%",
     "Of everything routed, how much really was actionable."),
    ("Review rate", f"{current['manual_review_rate_pct']}%",
     "The share of all traffic a person has to look at. This is the analyst-time bill."),
    ("Leakage", f"{current['auto_release_leakage_pct']}%",
     "Actionable traffic released with nobody looking. These are the misses that matter."),
    ("Auto-holds", f"{current['auto_holds']:,}",
     "Payments stopped by the machine before a human read the case."),
    ("Wrong auto-holds", f"{current['wrong_auto_holds']:,}",
     "Benign payments stopped by the machine. This is the number that decides whether a "
     "threshold is shippable."),
])

if release_below != settings.auto_release_below or hold_at != settings.auto_hold_at_or_above:
    changes = []
    for label, key in [("recall", "recall_pct"), ("precision", "precision_pct"),
                       ("review rate", "manual_review_rate_pct"),
                       ("leakage", "auto_release_leakage_pct")]:
        value = delta(key)
        if value:
            changes.append(f"{label} {value} pp")
    wrong_holds_delta = int(current["wrong_auto_holds"]) - int(shipped["wrong_auto_holds"])
    if wrong_holds_delta:
        changes.append(f"wrong auto-holds {wrong_holds_delta:+}")
    st.warning(
        "Against the shipped setting: " + ", ".join(changes) if changes
        else "Identical outcome to the shipped setting.",
        icon="⚖️",
    )

st.divider()

# --- the trade-off curve ----------------------------------------------------
st.subheader("The trade-off")
st.caption(
    "Every point is a real run of the decision policy at that auto-release threshold. "
    "The choice is not 'which is best' — it is how much analyst time this team has, and how "
    "much unlooked-at leakage the business will accept."
)

@st.cache_data(ttl=300, show_spinner="Computing the trade-off curve...")
def _curve(_transactions, _signals, _scores, grid: tuple[float, ...], cache_key: str):
    """The curve does not depend on the slider - only the marker on it does.

    Recomputing 36 policy runs over 6,000 transactions on every slider drag
    would make the page unusable for the one thing it exists for. The leading
    underscores tell Streamlit not to try to hash the frames; `cache_key`
    carries the identity that actually matters.
    """
    return pd.DataFrame(
        sweep_thresholds(settings, _transactions, _signals, _scores, list(grid))
    )


grid = tuple(round(x / 100, 2) for x in range(5, 76, 2))
curve = _curve(transactions, signals, scores, grid,
               cache_key=f"{len(transactions)}:{len(signals)}:{len(scores)}")

figure = go.Figure()
figure.add_trace(go.Scatter(
    x=curve["manual_review_rate_pct"], y=curve["recall_pct"],
    mode="lines", name="Recall against review rate",
    line=dict(color="#4a6fa5", width=2),
    customdata=curve["auto_release_below"],
    hovertemplate="release below %{customdata}<br>review rate %{x:.1f}%<br>"
                  "recall %{y:.1f}%<extra></extra>",
))
figure.add_trace(go.Scatter(
    x=[shipped["manual_review_rate_pct"]], y=[shipped["recall_pct"]],
    mode="markers+text", name="Shipped default",
    marker=dict(color="#2e6b3e", size=13, symbol="circle"),
    text=["shipped"], textposition="bottom center",
    hovertemplate="shipped: review %{x:.1f}%, recall %{y:.1f}%<extra></extra>",
))
figure.add_trace(go.Scatter(
    x=[current["manual_review_rate_pct"]], y=[current["recall_pct"]],
    mode="markers+text", name="This setting",
    marker=dict(color="#c25c00", size=15, symbol="diamond"),
    text=["you are here"], textposition="top center",
    hovertemplate="selected: review %{x:.1f}%, recall %{y:.1f}%<extra></extra>",
))
figure.update_layout(xaxis_title="Review rate (% of all traffic a person looks at)",
                     yaxis_title="Recall (% of actionable traffic caught)")
st.plotly_chart(neutral_chart_layout(figure, 420), use_container_width=True)

st.caption(
    "The knee of this curve is the argument to have with an operations lead: past it, each "
    "additional point of recall costs several points of analyst time. Below it, you are leaving "
    "cheap detection on the table."
)

st.divider()

left, right = st.columns([3, 2])

with left:
    st.subheader("Where the traffic goes")
    mix = pd.DataFrame(
        [{"action": action, "transactions": count}
         for action, count in sorted(current["action_counts"].items(),
                                     key=lambda item: -item[1])]
    )
    mix["share_pct"] = (100 * mix["transactions"] / mix["transactions"].sum()).round(2)
    st.dataframe(
        mix, use_container_width=True, hide_index=True,
        column_config={
            "action": st.column_config.TextColumn("Policy action"),
            "transactions": st.column_config.NumberColumn("Transactions", format="%d"),
            "share_pct": st.column_config.ProgressColumn(
                "Share", format="%.1f%%", min_value=0, max_value=100),
        },
    )

with right:
    st.subheader("The number to watch")
    st.metric("Wrong auto-holds", f"{current['wrong_auto_holds']:,}",
              delta=f"{int(current['wrong_auto_holds']) - int(shipped['wrong_auto_holds']):+}"
              if current["wrong_auto_holds"] != shipped["wrong_auto_holds"] else None,
              delta_color="inverse")
    st.caption(
        "Benign payments the machine stopped before any person read the case. Recall and "
        "precision are aggregate statistics; this one is a count of people who could not pay "
        "for something. It is recoverable through the appeal flow, which is why the product has "
        "one — but a threshold that grows this number needs an argument, not a slider."
    )

st.divider()
st.subheader("Full curve")
st.dataframe(
    curve[["auto_release_below", "recall_pct", "precision_pct", "false_positive_rate_pct",
           "manual_review_rate_pct", "auto_release_leakage_pct", "tp", "fp", "fn"]],
    use_container_width=True, hide_index=True, height=300,
    column_config={
        "auto_release_below": st.column_config.NumberColumn("Release below", format="%.2f"),
        "recall_pct": st.column_config.NumberColumn("Recall %", format="%.2f"),
        "precision_pct": st.column_config.NumberColumn("Precision %", format="%.2f"),
        "false_positive_rate_pct": st.column_config.NumberColumn("FP rate %", format="%.2f"),
        "manual_review_rate_pct": st.column_config.NumberColumn("Review rate %", format="%.2f"),
        "auto_release_leakage_pct": st.column_config.NumberColumn("Leakage %", format="%.2f"),
    },
)
st.caption(
    "Ground truth is the generator's `is_actionable_label`, on a population where roughly a "
    "fifth of transactions are actionable — about a hundred times a real corridor's base rate. "
    "**The shape of this curve transfers; the numbers on its axes do not.**"
)
