"""Investigation Workbench - one case, everything needed to judge it, and nothing that judges it.

The page is laid out around one argument: an investigator under time pressure
reads top-down and stops early, so whatever they most need must not be at the
bottom. What argues *against* the alert therefore sits next to the evidence
rather than below it, and what is missing is stated as its own section rather
than left as an absence the reader has to notice.

Nothing on this page decides anything. The priority breakdown explains an
ordering, the typology explanations describe patterns, and the disposition
control at the end requires a person, a choice and a written reason before it
will record anything at all.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime  # noqa: E402

import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import streamlit as st  # noqa: E402

from _i18n import current_language, t, translate_reason  # noqa: E402
from _shared import (  # noqa: E402
    BAND_COLOUR,
    neutral_chart_layout,
    page_setup,
    pill,
    synthetic_banner,
    write_session,
)
from riskops.aml.aggregate import render_merge_rationale  # noqa: E402
from riskops.aml.investigation import (  # noqa: E402
    DISPOSITIONS,
    DispositionError,
    claim_case,
    record_disposition,
    validate,
)
from riskops.aml.priority import parse_contributions  # noqa: E402
from riskops.aml.typology import (  # noqa: E402
    TYPOLOGY_BY_ID,
    parse_features,
    render_explanation,
)

frames = page_setup()
cases = frames.get("aml.cases", pd.DataFrame())
alerts = frames.get("aml.alerts", pd.DataFrame())
transfers = frames.get("aml.transfers", pd.DataFrame())
accounts = frames.get("aml.accounts", pd.DataFrame())
customers = frames.get("aml.customers", pd.DataFrame())
owners = frames.get("aml.beneficial_owners", pd.DataFrame())
decisions = frames.get("aml.investigation_decisions", pd.DataFrame())

st.title(t("Investigation Workbench"))
synthetic_banner()

if cases.empty:
    st.warning(
        t("The AML layer has not been built yet. Run this once, then reload:")
        + "\n\n```\npython -m riskops aml build\n```"
    )
    st.stop()

# --------------------------------------------------------------------------- #
# Case selection
# --------------------------------------------------------------------------- #

ordered = cases.sort_values("queue_position")
requested = st.query_params.get("case")
options = ordered["case_id"].tolist()
default_index = options.index(requested) if requested in options else 0

selected = st.selectbox(
    t("Case"), options, index=default_index,
    format_func=lambda cid: "{}  ·  {}  ·  {}".format(
        cid,
        ordered.loc[ordered["case_id"] == cid, "subject_id"].iloc[0],
        t(str(ordered.loc[ordered["case_id"] == cid, "priority_band"].iloc[0])),
    ),
)
case = ordered[ordered["case_id"] == selected].iloc[0].to_dict()

case_alerts = alerts[alerts["case_id"] == selected].copy()
transfer_ids: set[str] = set()
for value in case_alerts["transfer_ids"]:
    transfer_ids |= {x for x in str(value).split("|") if x}
case_transfers = transfers[transfers["transaction_id"].isin(transfer_ids)].copy()
case_transfers = case_transfers.sort_values("timestamp")

subject_account = accounts[accounts["account_id"] == case["subject_id"]]
subject_account = subject_account.iloc[0].to_dict() if not subject_account.empty else {}
customer = customers[customers["customer_id"] == case["subject_customer_id"]]
customer = customer.iloc[0].to_dict() if not customer.empty else {}
customer_owners = owners[owners["customer_id"] == case["subject_customer_id"]]

# --------------------------------------------------------------------------- #
# 1. Summary
# --------------------------------------------------------------------------- #

header = st.columns([3, 1])
with header[0]:
    st.subheader(t("Case summary"))
    st.markdown(
        f"**{case['case_id']}** · {t('account')} `{case['subject_id']}` · "
        f"{t('customer')} `{case['subject_customer_id']}` "
        + pill(t(str(case["priority_band"])), str(case["priority_band"])),
        unsafe_allow_html=True,
    )
with header[1]:
    st.metric(t("Priority"), f"{float(case['priority_score']):.3f}")
    st.caption(t("Queue position {n}").format(n=int(case["queue_position"])))

summary = st.columns(4)
summary[0].metric(t("Alerts"), int(case["alert_count"]))
summary[1].metric(t("Typologies"), int(case["typology_count"]))
summary[2].metric(t("Transfers"), int(case["transfer_count"]))
summary[3].metric(t("Amount (USD)"), f"{int(case['total_usd_minor']) / 100:,.0f}")

if not case["within_capacity"]:
    st.warning(t(
        "This case sits below today's review capacity line. It is not cleared and not "
        "low-risk; nobody has opened it."
    ), icon="⚠️")

st.caption(t("Why these alerts were treated as one case:"))
st.info(render_merge_rationale(case, language=current_language()))

st.divider()

# --------------------------------------------------------------------------- #
# 2. Priority breakdown — the ordering, explained
# --------------------------------------------------------------------------- #

st.subheader(t("Why this case is where it is in the queue"))
st.caption(t(
    "Priority is an ordering, not a verdict. It answers 'which case should a person open "
    "next', and says nothing about whether anything wrong happened here."
))

contributions = parse_contributions(str(case["priority_contributions"]))
if contributions:
    breakdown = pd.DataFrame(contributions)
    breakdown["label"] = breakdown["label"].map(t)
    chart = px.bar(
        breakdown, x="points", y="label", orientation="h",
        hover_data={"detail": True, "weight": ":.2f", "points": ":.4f", "label": False},
    )
    chart.update_layout(
        yaxis={"categoryorder": "total ascending", "title": None},
        xaxis={"title": t("Contribution to the priority score")},
    )
    st.plotly_chart(neutral_chart_layout(chart, height=300), use_container_width=True)

    st.dataframe(
        pd.DataFrame({
            t("Factor"): [t(c["label"]) for c in contributions],
            t("Points"): [round(c["points"], 4) for c in contributions],
            t("Max weight"): [round(c["weight"], 2) for c in contributions],
            t("What it read"): [c["detail"] for c in contributions],
        }).astype({t("What it read"): "string"}),
        use_container_width=True, hide_index=True,
    )

st.divider()

# --------------------------------------------------------------------------- #
# 3. Customer and account
# --------------------------------------------------------------------------- #

st.subheader(t("Customer and account"))
profile = st.columns(2)
with profile[0]:
    st.dataframe(
        pd.DataFrame({
            t("Field"): [
                t("Customer"), t("Type"), t("Home country"), t("Industry"),
                t("Onboarding risk level"), t("Expected monthly volume (USD)"),
                t("Profile last reviewed"),
            ],
            t("Value"): [
                str(customer.get("customer_name", "—")),
                t(str(customer.get("customer_type", "—"))),
                str(customer.get("home_country", "—")),
                str(customer.get("industry", "—")),
                t(str(customer.get("customer_risk_level", "—"))),
                f"{float(customer.get('expected_monthly_usd', 0)):,.0f}",
                str(customer.get("profile_reviewed_at", "—"))[:10],
            ],
        }).astype({t("Value"): "string"}),
        use_container_width=True, hide_index=True,
    )
with profile[1]:
    st.dataframe(
        pd.DataFrame({
            t("Field"): [
                t("Account"), t("Account country"), t("Currency"),
                t("Opened"), t("Account age (days)"), t("Account type"),
            ],
            t("Value"): [
                str(subject_account.get("account_id", "—")),
                str(subject_account.get("account_country", "—")),
                str(subject_account.get("currency", "—")),
                str(subject_account.get("opened_at", "—"))[:10],
                str(subject_account.get("account_age_days", "—")),
                t(str(subject_account.get("account_type", "—"))),
            ],
        }).astype({t("Value"): "string"}),
        use_container_width=True, hide_index=True,
    )

if not customer_owners.empty:
    st.caption(t("Beneficial owners on file"))
    st.dataframe(
        pd.DataFrame({
            t("Owner reference"): customer_owners["owner_reference"],
            t("Ownership %"): customer_owners["ownership_pct"],
            t("Verification"): customer_owners["verification_status"].map(t),
        }),
        use_container_width=True, hide_index=True,
    )
else:
    st.caption(t(
        "No beneficial owners recorded. For an individual customer that is expected; for a "
        "business it is itself a gap."
    ))

st.divider()

# --------------------------------------------------------------------------- #
# 4. Evidence, and what argues against it — side by side, deliberately
# --------------------------------------------------------------------------- #

st.subheader(t("What was found, and what would argue against it"))

for row in case_alerts.to_dict("records"):
    typology = TYPOLOGY_BY_ID.get(str(row["typology_id"]))
    title = t(typology.title) if typology else str(row["typology_id"])
    with st.expander(
        f"{title}  ·  {t(str(row['severity']))}  ·  {row['transfer_count']} {t('transfers')}",
        expanded=True,
    ):
        if typology:
            st.caption(t(typology.question))

        evidence, counter = st.columns(2)
        with evidence:
            st.markdown(f"**{t('Evidence')}**")
            # Re-rendered from the stored measurements rather than shown as
            # written, so a Chinese reader gets a Chinese sentence with the same
            # numbers in it. The stored English stays canonical for the audit.
            if typology:
                st.write(render_explanation(
                    typology, parse_features(str(row["feature_values"])),
                    language=current_language(),
                ))
            else:
                st.write(row["explanation"])
            st.caption(f"{t('Rule')} `{row['typology_id']}` "
                       f"{t('version')} `{row['typology_version']}`")
            st.caption(f"{t('Thresholds')}: {row['threshold_values']}")
            st.caption(f"{t('Measured')}: {row['feature_values']}")
            if int(row.get("duplicate_count") or 0):
                st.caption(t(
                    "This alert absorbed {n} duplicate firing(s) of the same finding."
                ).format(n=int(row["duplicate_count"])))
        with counter:
            st.markdown(f"**{t('What would argue against this')}**")
            for hint in str(row["counter_evidence"]).split(" | "):
                if hint.strip():
                    st.markdown(f"- {t(hint.strip())}")
            st.caption(t(
                "An unusual transaction is not a laundered transaction. These are the "
                "ordinary explanations to rule out before treating the pattern as a finding."
            ))

st.divider()

# --------------------------------------------------------------------------- #
# 5. Timeline
# --------------------------------------------------------------------------- #

st.subheader(t("Cross-border transfer timeline"))
if case_transfers.empty:
    st.caption(t("No transfers resolved for this case."))
else:
    plot = case_transfers.copy()
    plot["usd"] = plot["normalized_amount_usd_minor"] / 100
    plot["corridor"] = plot["origin_country"] + " → " + plot["destination_country"]
    plot["direction"] = plot["payer_account"].map(
        lambda a: t("outbound") if a == case["subject_id"] else t("inbound")
    )
    figure = px.scatter(
        plot, x="timestamp", y="usd", color="direction", size="usd",
        hover_data=["transaction_id", "corridor", "declared_purpose", "channel"],
    )
    st.plotly_chart(neutral_chart_layout(figure, height=320), use_container_width=True)

    st.dataframe(
        pd.DataFrame({
            t("Transfer"): case_transfers["transaction_id"],
            t("When"): case_transfers["timestamp"],
            t("From"): case_transfers["payer_account"],
            t("To"): case_transfers["beneficiary_account"],
            t("Corridor"): (case_transfers["origin_country"] + " → "
                            + case_transfers["destination_country"]),
            t("Amount"): case_transfers["amount_minor"] / 100,
            t("Currency"): case_transfers["currency"],
            t("USD"): (case_transfers["normalized_amount_usd_minor"] / 100).round(0),
            t("Purpose"): case_transfers["declared_purpose"].replace("", "—"),
            t("Channel"): case_transfers["channel"],
            t("Beneficiary info"): case_transfers["beneficiary_information_status"].map(t),
        }),
        use_container_width=True, hide_index=True, height=280,
    )

# --------------------------------------------------------------------------- #
# 6. Relationships
# --------------------------------------------------------------------------- #

st.subheader(t("Who this account moved money with"))
if case_transfers.empty:
    st.caption(t("No counterparties to show."))
else:
    subject = str(case["subject_id"])
    rows = []
    for record in case_transfers.to_dict("records"):
        other = (str(record["beneficiary_account"]) if str(record["payer_account"]) == subject
                 else str(record["payer_account"]))
        rows.append({
            "counterparty": other,
            "direction": (t("sent to") if str(record["payer_account"]) == subject
                          else t("received from")),
            "usd": int(record["normalized_amount_usd_minor"]) / 100,
            "country": (str(record["destination_country"])
                        if str(record["payer_account"]) == subject
                        else str(record["origin_country"])),
            "device": str(record["device_id"]),
        })
    network = pd.DataFrame(rows)
    grouped = network.groupby(
        ["counterparty", "direction", "country"], as_index=False
    ).agg(transfers=("usd", "size"), usd=("usd", "sum"))
    grouped = grouped.sort_values("usd", ascending=False)
    st.dataframe(
        pd.DataFrame({
            t("Counterparty account"): grouped["counterparty"],
            t("Direction"): grouped["direction"],
            t("Country"): grouped["country"],
            t("Transfers"): grouped["transfers"],
            t("USD"): grouped["usd"].round(0),
        }),
        use_container_width=True, hide_index=True, height=240,
    )
    shared_devices = network.groupby("device")["counterparty"].nunique()
    shared = shared_devices[shared_devices > 1]
    if not shared.empty:
        st.caption(t(
            "{n} device(s) appear across more than one counterparty in this case. A shared "
            "device is a link worth checking, and also the ordinary result of a family or an "
            "agent using one terminal."
        ).format(n=len(shared)))

st.divider()

# --------------------------------------------------------------------------- #
# 7. What is missing
# --------------------------------------------------------------------------- #

st.subheader(t("What we cannot see"))
gaps: list[str] = []
if not case_transfers.empty:
    incomplete = int(case_transfers["beneficiary_information_status"].isin(
        ("missing", "partial")
    ).sum())
    if incomplete:
        gaps.append(t(
            "{n} of {total} transfers carry incomplete beneficiary information."
        ).format(n=incomplete, total=len(case_transfers)))
    blank = int((case_transfers["declared_purpose"].fillna("").str.strip() == "").sum())
    if blank:
        gaps.append(t("{n} transfers declare no purpose at all.").format(n=blank))
if not customer_owners.empty and (customer_owners["verification_status"] != "verified").any():
    gaps.append(t("At least one beneficial owner on this customer is unverified."))
if customer.get("customer_type") == "business" and customer_owners.empty:
    gaps.append(t("This is a business customer with no beneficial owner recorded."))
if not gaps:
    gaps.append(t("No structural information gap was detected on this case."))

for gap in gaps:
    st.markdown(f"- {gap}")

st.caption(t(
    "A gap is a reason to request information, not on its own a reason to escalate. Missing "
    "data is very often a defect at the sending institution."
))

st.divider()

# --------------------------------------------------------------------------- #
# 8. Disposition — the only place anything is decided, and only by a person
# --------------------------------------------------------------------------- #

st.subheader(t("Record an investigation decision"))
st.caption(t(
    "This system organises evidence and orders a queue. It does not decide. It cannot freeze "
    "an account, block a payment, file anything with any authority, or conclude that money "
    "was laundered — no such action exists in the code, not merely in the interface."
))

if not decisions.empty:
    history = decisions[decisions["case_id"] == selected]
    if not history.empty:
        st.caption(t("Decisions already recorded on this case"))
        st.dataframe(
            pd.DataFrame({
                t("When"): history["decided_at"],
                t("Disposition"): history["disposition"].map(t),
                t("By"): history["actor_id"],
                t("Role"): history["actor_role"].map(t),
                t("Reason"): history["reason"],
            }).astype({t("Reason"): "string"}),
            use_container_width=True, hide_index=True,
        )

with st.form("aml_disposition"):
    controls = st.columns([1, 1])
    with controls[0]:
        actor_role = st.selectbox(
            t("Your role"), ["aml_investigator", "risk_ops_lead", "admin_auditor"],
            format_func=t,
        )
        actor_id = st.text_input(t("Your identifier"), "investigator.demo")
    with controls[1]:
        disposition_key = st.selectbox(
            t("Disposition"), [d.key for d in DISPOSITIONS],
            format_func=lambda k: t(next(d.title for d in DISPOSITIONS if d.key == k)),
        )
        chosen = next(d for d in DISPOSITIONS if d.key == disposition_key)
        st.caption(t(chosen.description))

    reason = st.text_area(
        t("Reason (required)"),
        placeholder=t(
            "What did you conclude, and from what? Name a transfer, an account, a typology "
            "or a document."
        ),
        height=110,
    )
    notes = st.text_area(t("Working notes (optional)"), height=70)
    evidence_ids = st.multiselect(
        t("Alerts this decision rests on"),
        case_alerts["alert_id"].tolist(),
        default=case_alerts["alert_id"].tolist(),
    )
    submitted = st.form_submit_button(t("Record decision"), type="primary")

if submitted:
    try:
        validate(disposition_key, reason, actor_role)
    except DispositionError as exc:
        st.error(translate_reason(str(exc)))
    else:
        try:
            with write_session() as con:
                record_disposition(
                    con,
                    case_id=selected,
                    disposition_key=disposition_key,
                    reason=reason,
                    actor_role=actor_role,
                    actor_id=actor_id.strip() or "unnamed",
                    now=datetime.now().replace(microsecond=0),
                    notes=notes,
                    evidence_alert_ids=evidence_ids,
                )
            st.success(t(
                "Recorded. The case moved to {state}, and the decision, its reason and your "
                "identifier are now in the hash-chained audit log."
            ).format(state=t(chosen.next_state)))
            st.cache_data.clear()
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            st.error(f"{type(exc).__name__}: {exc}")

st.caption(t(
    "Claiming a case starts the SLA clock without recording an outcome — use it when you "
    "begin work, so a case correctly left open awaiting information is not measured as slow."
))
if st.button(t("Claim this case")):
    try:
        with write_session() as con:
            claim_case(
                con, case_id=selected, actor_role="aml_investigator",
                actor_id="investigator.demo",
                now=datetime.now().replace(microsecond=0),
            )
        st.success(t("Case claimed."))
        st.cache_data.clear()
    except Exception as exc:  # noqa: BLE001
        st.error(f"{type(exc).__name__}: {exc}")

st.divider()
st.caption(
    t("Priority band colours") + ": "
    + " ".join(pill(t(band), band) for band in BAND_COLOUR if band != "none"),
    unsafe_allow_html=True,
)
