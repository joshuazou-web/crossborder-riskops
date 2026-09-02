"""Transaction Explorer - find one payment and see everything about it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from _shared import money, page_setup, pill, synthetic_banner  # noqa: E402

frames = page_setup("Transaction Explorer", "🔎")
transactions = frames["marts.fct_transactions"]
signals = frames["risk.signals"]
events = frames["core.payment_events"]
breaks = frames["core.reconciliation_breaks"]

st.title("Transaction Explorer")
st.caption("Every payment, its lifecycle, its money and the signals it produced.")
synthetic_banner()

# --- filters ---------------------------------------------------------------
with st.container(border=True):
    row1 = st.columns([2, 1, 1, 1])
    search = row1[0].text_input(
        "Search", placeholder="transaction, wallet, merchant or idempotency key",
        label_visibility="collapsed",
    )
    state = row1[1].multiselect("Payment state", sorted(transactions["payment_state"].unique()))
    action = row1[2].multiselect("Policy action", sorted(transactions["policy_action"].unique()))
    band = row1[3].multiselect("Risk band", ["critical", "high", "medium", "low"])

    row2 = st.columns([1, 1, 1, 1])
    currency = row2[0].multiselect(
        "Presentment currency", sorted(transactions["presentment_currency"].unique())
    )
    corridor = row2[1].multiselect(
        "Corridor", sorted(transactions["corridor"].dropna().unique())
    )
    severity = row2[2].multiselect("Highest severity",
                                   ["critical", "high", "medium", "low", "none"])
    only_breaks = row2[3].checkbox("Only with reconciliation breaks")

view = transactions.copy()
if search:
    needle = search.strip().lower()
    mask = (
        view["transaction_id"].str.lower().str.contains(needle)
        | view["wallet_id"].str.lower().str.contains(needle)
        | view["merchant_id"].str.lower().str.contains(needle)
    )
    core_txn = frames["core.transactions"]
    keys = core_txn[core_txn["idempotency_key"].str.lower().str.contains(needle)]
    mask = mask | view["transaction_id"].isin(keys["transaction_id"])
    view = view[mask]
if state:
    view = view[view["payment_state"].isin(state)]
if action:
    view = view[view["policy_action"].isin(action)]
if band:
    view = view[view["risk_band"].isin(band)]
if currency:
    view = view[view["presentment_currency"].isin(currency)]
if corridor:
    view = view[view["corridor"].isin(corridor)]
if severity:
    view = view[view["max_severity"].isin(severity)]
if only_breaks:
    view = view[view["reconciliation_break_count"] > 0]

st.caption(f"{len(view):,} of {len(transactions):,} transactions")

display = view.sort_values("created_at", ascending=False).head(400).copy()
display["amount"] = [
    money(r.captured_minor, r.presentment_currency) for r in display.itertuples()
]
display["settled"] = [
    money(r.settled_minor, r.settlement_currency) for r in display.itertuples()
]

selection = st.dataframe(
    display[[
        "transaction_id", "created_at", "payment_state", "corridor", "amount", "settled",
        "policy_action", "risk_band", "risk_score", "max_severity", "signal_count",
        "reconciliation_break_count", "merchant_id", "mcc_description", "case_id",
    ]],
    use_container_width=True, hide_index=True, height=380,
    on_select="rerun", selection_mode="single-row",
    column_config={
        "transaction_id": st.column_config.TextColumn("Transaction", width="medium"),
        "created_at": st.column_config.DatetimeColumn("Created", format="YYYY-MM-DD HH:mm"),
        "payment_state": st.column_config.TextColumn("State", width="small"),
        "risk_score": st.column_config.NumberColumn("Score", format="%.2f"),
        "signal_count": st.column_config.NumberColumn("Signals", format="%d"),
        "reconciliation_break_count": st.column_config.NumberColumn("Breaks", format="%d"),
        "mcc_description": st.column_config.TextColumn("Merchant category", width="medium"),
    },
)

rows = selection.get("selection", {}).get("rows", []) if selection else []
if not rows:
    st.info("Select a row to open the full transaction record.")
    st.stop()

record = display.iloc[rows[0]]
txn_id = str(record["transaction_id"])

st.divider()
st.subheader(txn_id)
st.markdown(
    pill(str(record["risk_band"]).upper(), str(record["risk_band"]))
    + pill(str(record["payment_state"]), "none")
    + pill(str(record["policy_action"]), "none"),
    unsafe_allow_html=True,
)

detail = frames["core.transactions"]
full = detail[detail["transaction_id"] == txn_id].iloc[0]

left, middle, right = st.columns(3)
with left:
    st.markdown("**Money**")
    st.write(pd.DataFrame([
        {"Field": "Authorised", "Value": money(full["authorized_minor"], full["presentment_currency"])},
        {"Field": "Captured", "Value": money(full["captured_minor"], full["presentment_currency"])},
        {"Field": "Fee", "Value": money(full["fee_minor"], full["presentment_currency"])},
        {"Field": "Refunded", "Value": money(full["refunded_minor"], full["presentment_currency"])},
        {"Field": "Charged back", "Value": money(full["charged_back_minor"], full["presentment_currency"])},
        {"Field": "Settled", "Value": money(full["settled_minor"], full["settlement_currency"])},
    ]).set_index("Field"))
with middle:
    st.markdown("**FX**")
    st.write(pd.DataFrame([
        {"Field": "Presentment", "Value": full["presentment_currency"]},
        {"Field": "Settlement", "Value": full["settlement_currency"]},
        {"Field": "Quoted rate", "Value": full["quoted_fx_rate"] or "-"},
        {"Field": "Applied rate", "Value": full["applied_fx_rate"] or "-"},
        {"Field": "Quote id", "Value": full["fx_quote_id"] or "-"},
        {"Field": "Quoted at", "Value": str(full["fx_quoted_at"])[:19]},
    ]).set_index("Field"))
with right:
    st.markdown("**Context**")
    st.write(pd.DataFrame([
        {"Field": "Wallet", "Value": full["wallet_id"]},
        {"Field": "Wallet country", "Value": full["wallet_country"]},
        {"Field": "Payer country", "Value": full["payer_country"]},
        {"Field": "IP country", "Value": full["ip_country"]},
        {"Field": "Merchant country", "Value": full["merchant_country"]},
        {"Field": "Device", "Value": full["device_id"] or "(missing)"},
        {"Field": "Idempotency key", "Value": full["idempotency_key"]},
    ]).set_index("Field"))

st.markdown("**Lifecycle**")
timeline = events[events["transaction_id"] == txn_id].sort_values("occurred_at")
timeline_view = timeline[[
    "occurred_at", "event_type", "from_state", "to_state", "amount_minor", "currency",
    "accepted", "reject_reason",
]].copy()
st.dataframe(
    timeline_view, use_container_width=True, hide_index=True,
    column_config={
        "occurred_at": st.column_config.DatetimeColumn("When", format="YYYY-MM-DD HH:mm:ss"),
        "event_type": st.column_config.TextColumn("Event", width="small"),
        "amount_minor": st.column_config.NumberColumn("Amount (minor units)", format="%d"),
        "accepted": st.column_config.CheckboxColumn("Accepted"),
        "reject_reason": st.column_config.TextColumn("Why it was rejected", width="large"),
    },
)
if not timeline[~timeline["accepted"].astype(bool)].empty:
    st.warning(
        "This transaction received events the state machine refused. They were quarantined and "
        "never reached the ledger - the money below is still correct."
    )

left, right = st.columns(2)
with left:
    st.markdown("**Risk signals**")
    txn_signals = signals[signals["transaction_id"] == txn_id]
    if txn_signals.empty:
        st.caption("No rule fired on this transaction.")
    else:
        for record_signal in txn_signals.sort_values("weight", ascending=False).to_dict("records"):
            st.markdown(
                pill(record_signal["severity"], record_signal["severity"])
                + f"**{record_signal['rule_id']}** — {record_signal['title']}",
                unsafe_allow_html=True,
            )
            st.markdown(f"<div style='margin:0 0 .6rem .2rem'>{record_signal['detail']}</div>",
                        unsafe_allow_html=True)
            st.markdown(
                f"<div class='evidence'>evidence: {record_signal['evidence_fields']}</div>",
                unsafe_allow_html=True,
            )

with right:
    st.markdown("**Reconciliation**")
    txn_breaks = breaks[breaks["transaction_id"] == txn_id]
    if txn_breaks.empty:
        st.caption("The money reconciles.")
    else:
        for record_break in txn_breaks.to_dict("records"):
            st.markdown(f"**{record_break['break_type']}** ({record_break['difference_bps']} bps)")
            st.markdown(
                f"expected `{record_break['expected_minor']}` · observed "
                f"`{record_break['observed_minor']}` {record_break['currency']}"
            )
            st.caption(record_break["detail"])

    note = str(full["merchant_note"] or "")
    if note:
        st.markdown("**Merchant free text** (untrusted)")
        st.code(note, language=None)
        st.caption(
            "Merchant-supplied text is attacker-controlled. It is screened before any model "
            "reads it; see the Case Detail page for what the copilot was actually shown."
        )

if str(record.get("case_id") or ""):
    st.info(
        f"This transaction opened case **{record['case_id']}**. Open the Case Queue or Case "
        "Detail page to work it."
    )
