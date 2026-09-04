"""AML evaluation - the measured results, with what they cannot support beside them.

Every number here is read from `reports/aml_evaluation.json`, written by
`python -m riskops aml eval`. Nothing on this page is typed in. If the file is
missing the page says so rather than showing a plausible blank.

The page is ordered by what a sceptical reader should check first, which is not
the same as what is most flattering. The headline comparison comes before the
per-typology table, and the row that would invalidate everything else - recall
on scenarios built deliberately outside the thresholds - is given its own
section rather than buried in a table.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402

import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import streamlit as st  # noqa: E402

from _i18n import t  # noqa: E402
from _shared import kpi_row, neutral_chart_layout, page_setup, synthetic_banner  # noqa: E402
from riskops.aml.typology import TYPOLOGY_BY_ID  # noqa: E402
from riskops.config import get_settings  # noqa: E402

page_setup()

st.title(t("AML evaluation"))
synthetic_banner()

report_path = get_settings().reports_dir / "aml_evaluation.json"
if not report_path.exists():
    st.warning(
        t("No AML evaluation has been run yet. Run this, then reload:")
        + "\n\n```\npython -m riskops aml eval\n```"
    )
    st.stop()

report = json.loads(report_path.read_text(encoding="utf-8"))
headline = report["headline"]
dataset = report["dataset"]
first_run = report["runs"][0]


def spread(key: str) -> str:
    value = headline.get(key)
    if not isinstance(value, dict) or not value:
        return "—"
    return f"{value['mean']:.3f} ± {value['stdev']:.3f}"


st.caption(
    f"{t('Generated')} {report['generated_at']} · "
    f"{t('seeds')} `{', '.join(str(s) for s in dataset['seeds'])}` · "
    f"{dataset['transfers_per_seed']:,} {t('transfers per seed')} · "
    f"{t('typology')} `{report['typology_version']}` · "
    f"{t('priority')} `{report['priority_version']}`"
)

with st.expander(t("Read this before reading any number"), expanded=True):
    # The caveat is written once, in English, into the report. Translated here
    # for the same reason the alert explanations are: the file stays canonical.
    st.markdown(f"- {t(dataset['caveat'])}")
    st.markdown("- " + t(
        "**An unusual transaction is not a laundered transaction.** Recall here means the "
        "detectors found a pattern the generator planted. It does not mean anything was "
        "laundered, and no figure on this page is a detection rate for crime."
    ))
    st.markdown("- " + t(
        "Precision is measured against injected scenarios, not against ground truth. An "
        "alert counted as a false positive may describe genuinely unusual behaviour."
    ))
    st.markdown("- " + t(
        "These figures describe this generator's population. Comparing them with a "
        "published industry figure would be meaningless."
    ))

st.divider()

# --------------------------------------------------------------------------- #
# The comparison the product exists to make
# --------------------------------------------------------------------------- #

st.subheader(t("What prioritisation is worth"))

raw = headline["alert_precision_on_injections"]["mean"]
at_capacity = headline["precision_at_review_capacity"]["mean"]

kpi_row([
    (t("Alert precision, raw"), f"{raw:.1%}",
     t("Share of alerts overlapping a planted pattern. Low, and expected to be.")),
    (t("Precision at review capacity"), f"{at_capacity:.1%}",
     t("Of the cases a team of this size could actually open, the share touching one.")),
    (t("Difference"), f"+{(at_capacity - raw) * 100:.1f}pp",
     t("What the eight-factor ordering is worth on this dataset.")),
    (t("Left unreviewed"), f"{first_run['queue']['injected_patterns_left_unreviewed']:,}",
     t("Planted patterns sitting in the backlog. Not cleared — not looked at.")),
])

st.info(t(
    "The alerts are mostly noise, as they are in every real monitoring system. The ordering "
    "is what makes a day of work worth doing. The honest half of that claim is the last "
    "number: the cases below the capacity line were **not** cleared."
))

st.divider()

# --------------------------------------------------------------------------- #
# The row that would invalidate the rest
# --------------------------------------------------------------------------- #

st.subheader(t("The number to check first"))
st.caption(t(
    "Scenarios are injected at three difficulties. A high recall on the third would mean the "
    "detectors fire on anything, and every other figure on this page would be worthless."
))

difficulty = first_run["detection"]["by_difficulty"]
rows = []
for level, label, reading in (
    ("clear", t("Clear — comfortably inside the thresholds"), t("Should be high")),
    ("borderline", t("Borderline — just inside"), t("What the thresholds actually buy")),
    ("below_threshold", t("Below threshold — built outside on purpose"),
     t("Should be LOW")),
):
    entry = difficulty[level]
    rows.append({
        t("Difficulty"): label,
        t("Injected"): entry["injected"],
        t("Detected"): entry["detected"],
        t("Recall"): entry["recall"],
        t("How to read it"): reading,
    })
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.caption(t(
    "The first version of the generator built every scenario squarely inside its detector's "
    "thresholds and scored 100% on all six typologies. That number measured nothing — a "
    "detector that fired on everything would have scored identically."
))

st.divider()

# --------------------------------------------------------------------------- #
# Per typology
# --------------------------------------------------------------------------- #

st.subheader(t("Recall by typology"))

per_typology = []
for typology_id, value in report["per_typology_recall"].items():
    typology = TYPOLOGY_BY_ID.get(typology_id)
    per_typology.append({
        "typology": t(typology.title) if typology else typology_id,
        "recall": value.get("mean", 0.0),
        "stdev": value.get("stdev", 0.0),
    })
frame = pd.DataFrame(per_typology)

chart = px.bar(frame, x="recall", y="typology", orientation="h", error_x="stdev")
chart.update_layout(
    yaxis={"categoryorder": "total ascending", "title": None},
    xaxis={"title": t("Recall (mean over seeds)"), "range": [0, 1]},
)
st.plotly_chart(neutral_chart_layout(chart, height=300), use_container_width=True)

st.divider()

# --------------------------------------------------------------------------- #
# Everything else, as a table
# --------------------------------------------------------------------------- #

st.subheader(t("The rest of the headline"))

st.dataframe(
    pd.DataFrame({
        t("Metric"): [
            t("Scenario recall (all difficulties)"),
            t("Case aggregation rate"),
            t("Duplicate alert reduction"),
            t("Evidence traceability"),
            t("Counter-evidence rate"),
            t("Unsupported-claim rate"),
            t("Boundary probe failures"),
        ],
        t("Value"): [
            spread("scenario_recall"),
            spread("case_aggregation_rate"),
            spread("duplicate_alert_reduction"),
            spread("evidence_traceability_rate"),
            spread("counter_evidence_rate"),
            str(headline["unsupported_claim_rate"]),
            str(headline["boundary_probe_failures"]),
        ],
        t("What it means"): [
            t("Injected patterns the detectors found, across every difficulty."),
            t("Alerts per case. 1.00 would mean aggregation did nothing."),
            t("Share of raw firings that were repeats of a finding already reported."),
            t("Alerts whose every cited transfer resolves to a row that exists."),
            t("Alerts carrying what would argue against them."),
            t("Adversarial probes against the disposition boundary that succeeded."),
            t("Attempts to decide as a non-human, without a reason, or naming an action "
              "this system cannot perform."),
        ],
    }).astype({t("What it means"): "string"}),
    use_container_width=True, hide_index=True,
)

st.divider()

# --------------------------------------------------------------------------- #
# What it cannot tell you
# --------------------------------------------------------------------------- #

st.subheader(t("What this evaluation cannot tell you"))
for line in (
    t("Whether these typologies match real laundering behaviour. They are implementations "
      "of publicly described patterns, tested against a generator written by the same author."),
    t("What the false-positive rate would be on real traffic. The base rate here is "
      "enriched by orders of magnitude."),
    t("Whether an investigator would agree with the priority ordering. No investigator has "
      "used this."),
    t("Anything at all about a real institution's controls, staffing or effectiveness."),
):
    st.markdown(f"- {line}")

st.caption(t(
    "Full report, including the degradation cases and every boundary probe: "
    "docs/AML_EVALUATION_REPORT.md — generated, never edited by hand."
))
