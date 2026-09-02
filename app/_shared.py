"""Shared plumbing for the CrossBorder RiskOps workbench.

Two rules shape this module.

1. **One definition of every metric.** Everything derived - SLA breach, handling
   time, corridor, USD equivalents - is computed once in
   `src/riskops/sql/marts.sql` and lands on a mart table. These pages filter and
   aggregate those tables; they never re-implement a definition. If the overview
   and the queue disagree about what "breached" means, both numbers are worthless.

2. **Never hold the warehouse open.** Tables are read into memory and the file
   connection closes immediately, so `python -m riskops refresh` can still write
   while somebody has the dashboard open.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from riskops.config import get_settings  # noqa: E402
from riskops.taxonomy import TAXONOMY_VERSION  # noqa: E402

CACHE_TTL_SECONDS = 30

TABLES = [
    "marts.fct_transactions",
    "marts.fct_cases",
    "marts.kpi_overview",
    "marts.kpi_daily_volume",
    "marts.kpi_state_mix",
    "marts.kpi_corridor",
    "marts.kpi_signal_frequency",
    "marts.kpi_case_queue",
    "marts.kpi_case_outcomes",
    "marts.kpi_sla",
    "marts.kpi_ai_agreement",
    "marts.kpi_ai_guardrails",
    "marts.kpi_appeals",
    "marts.kpi_reconciliation",
    "marts.kpi_data_quality",
    "marts.kpi_policy_mix",
    "risk.signals",
    "risk.cases",
    "risk.model_scores",
    "risk.policy_decisions",
    "core.reconciliation_breaks",
    "core.payment_events",
    "core.merchants",
    "core.wallets",
    "core.transactions",
    "audit.audit_log",
    "audit.decisions",
    "audit.appeals",
    "audit.ai_invocations",
    "audit.validation_results",
    "audit.refresh_log",
]

# Colour is used for one thing only: risk severity. Everything else stays
# neutral, so a red cell always means the same thing wherever it appears.
BAND_COLOUR = {
    "critical": "#b3261e",
    "high": "#c25c00",
    "medium": "#8a6d00",
    "low": "#2e6b3e",
    "none": "#5a5a5a",
}


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_tables() -> dict[str, pd.DataFrame]:
    """Read every table the pages need, then close the connection."""
    settings = get_settings()
    if not settings.db_path.exists():
        return {}
    frames: dict[str, pd.DataFrame] = {}
    con = duckdb.connect(str(settings.db_path), read_only=True)
    try:
        for table in TABLES:
            try:
                frames[table] = con.execute(f"SELECT * FROM {table}").fetch_df()
            except duckdb.Error:
                frames[table] = pd.DataFrame()
    finally:
        con.close()
    return frames


def page_setup(title: str, icon: str = "🛡️") -> dict[str, pd.DataFrame]:
    st.set_page_config(page_title=f"{title} · CrossBorder RiskOps", page_icon=icon,
                       layout="wide", initial_sidebar_state="expanded")
    _inject_css()
    frames = load_tables()
    if not frames or frames.get("marts.fct_transactions", pd.DataFrame()).empty:
        st.title("CrossBorder RiskOps")
        st.warning(
            "The warehouse has not been built yet. Run this once, then reload:\n\n"
            "```\npython -m riskops demo\n```"
        )
        st.stop()
    _sidebar(frames)
    return frames


def _inject_css() -> None:
    st.markdown(
        """
        <style>
          .block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1500px; }
          [data-testid="stMetricValue"] { font-size: 1.55rem; }
          [data-testid="stMetricLabel"] { font-size: 0.78rem; opacity: 0.85; }
          .synthetic-banner {
              border-left: 4px solid #c25c00; background: rgba(194, 92, 0, 0.08);
              padding: 0.6rem 0.9rem; border-radius: 4px; font-size: 0.86rem;
              margin-bottom: 1.1rem; line-height: 1.5;
          }
          .advisory-box {
              border: 1px solid rgba(128,128,128,0.35); border-left: 4px solid #4a6fa5;
              border-radius: 6px; padding: 0.9rem 1.1rem; margin: 0.5rem 0 1rem 0;
          }
          .advisory-box h4 { margin: 0 0 0.4rem 0; font-size: 0.95rem; }
          .pill {
              display: inline-block; padding: 0.12rem 0.55rem; border-radius: 999px;
              font-size: 0.74rem; font-weight: 600; color: #fff; margin-right: 0.3rem;
          }
          .evidence { font-size: 0.82rem; opacity: 0.75; font-family: ui-monospace, monospace; }
          .stDataFrame { font-size: 0.86rem; }
          section[data-testid="stSidebar"] { min-width: 280px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def synthetic_banner(extra: str = "") -> None:
    st.markdown(
        f"""<div class="synthetic-banner">
        <strong>Synthetic data · simulated environment.</strong>
        Every transaction, merchant, wallet, device and risk label on this screen was generated by a
        seeded script. Nothing here has touched a real payment network, bank, merchant or customer,
        and no figure describes a real business outcome. {extra}
        </div>""",
        unsafe_allow_html=True,
    )


def _sidebar(frames: dict[str, pd.DataFrame]) -> None:
    settings = get_settings()
    refreshes = frames.get("audit.refresh_log", pd.DataFrame())
    with st.sidebar:
        st.markdown("### CrossBorder RiskOps")
        st.caption("Cross-border payment risk operations workbench")
        st.divider()
        if not refreshes.empty:
            latest = refreshes.sort_values("started_at").iloc[-1]
            st.caption(
                f"**Last build** {pd.Timestamp(latest['started_at']):%Y-%m-%d %H:%M}  \n"
                f"seed `{latest['seed']}` · validation `{latest['validation_status']}`  \n"
                f"rules `{latest['rules_version']}` · model `{latest['model_version']}`"
            )
        st.caption(f"taxonomy `{TAXONOMY_VERSION}` · reporting cut-off `{settings.as_of_date}`")
        st.divider()
        st.caption(
            "**AI boundary.** The copilot organises evidence and *recommends*. "
            "It cannot release, hold, refund or close anything. Those are committed by a person "
            "or by the deterministic policy, and the audit log refuses to record an AI actor on a "
            "decision."
        )


def pill(text: str, band: str) -> str:
    colour = BAND_COLOUR.get(band, BAND_COLOUR["none"])
    return f'<span class="pill" style="background:{colour}">{text}</span>'


def money(minor: object, currency: object) -> str:
    """Format a minor-unit amount for display. Never used in a comparison."""
    from riskops.money import Money, MoneyError

    try:
        return Money(int(minor), str(currency)).format()
    except (MoneyError, TypeError, ValueError):
        return "-"


def kpi_row(items: list[tuple[str, object, str]]) -> None:
    """A row of metrics: (label, value, help)."""
    columns = st.columns(len(items))
    for column, (label, value, helptext) in zip(columns, items, strict=True):
        column.metric(label, value, help=helptext)


def neutral_chart_layout(figure, height: int = 320):
    figure.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=30, b=10),
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
    )
    return figure


def case_link(case_id: str) -> None:
    """Send the reader to Case Detail with this case selected."""
    st.session_state["selected_case_id"] = case_id
