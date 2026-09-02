"""Robustness: how much of a headline number is the system, and how much is luck?

Three questions this module exists to answer, none of which a single run can:

1. **Would a different seed give a different answer?** A metric quoted to two
   decimal places from one run implies a precision nobody measured. Every
   headline figure here comes with a mean and a standard deviation across
   several independently generated worlds.
2. **What did each component actually contribute?** The product has two
   detectors - deterministic rules and a model - and reporting only their
   combined output makes "we added a model" an unevaluable action. Baselines
   compare rules-only, model-only and combined *at a matched review budget*,
   which is the only comparison an operations lead would accept.
3. **Which rules are load-bearing?** Leave-one-rule-out tells you which of the
   twenty are doing the work and which are decoration.

Everything runs in memory. No warehouse is touched, so a sweep cannot corrupt
the database a dashboard is reading, and the AI and review layers are skipped
because detection does not depend on them.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from ..config import Settings
from ..generator.synth import generate
from ..pipeline.build import build_core, normalise_events
from ..risk import policy, rules, scoring

ROBUSTNESS_VERSION = "1.0.0"

# The metrics a sweep reports a distribution for. Kept short on purpose: a
# spread on twenty metrics is a table nobody reads.
# Three of the model's fifteen features are the rule engine's own output. A
# "model only" baseline that keeps them is not model-against-rules; it is the
# rules, re-encoded, wearing a model's coat.
RULE_DERIVED_FEATURES = (
    "signal_count",
    "max_severity_rank",
    "distinct_signal_families",
)

SWEEP_METRICS = (
    "recall_pct",
    "precision_pct",
    "false_positive_rate_pct",
    "manual_review_rate_pct",
    "auto_release_leakage_pct",
    "actionable_base_rate_pct",
)


@dataclass
class DetectionFrames:
    """Everything needed to score detection, for one generated world."""

    seed: int
    transactions: pd.DataFrame
    signals: pd.DataFrame
    scores: pd.DataFrame
    breaks: pd.DataFrame
    merchants: pd.DataFrame = field(default_factory=pd.DataFrame)
    wallets: pd.DataFrame = field(default_factory=pd.DataFrame)
    model_metrics: dict[str, float] = field(default_factory=dict)

    @property
    def labels(self) -> pd.Series:
        return self.transactions["is_actionable_label"].astype(bool)


def build_detection_frames(settings: Settings, seed: int) -> DetectionFrames:
    """Generate a world and run it as far as scored signals, in memory."""
    as_of = datetime.fromisoformat(settings.as_of_date)
    world = generate(settings, seed=seed)
    events = normalise_events(world.events, f"SWEEP_{seed}")
    core = build_core(
        settings, events, world.labels, world.merchants, world.wallets,
        world.fx_quotes, f"SWEEP_{seed}", now=as_of,
    )
    transactions = core["core.transactions"]
    breaks = core["core.reconciliation_breaks"]

    context = rules.RuleContext(
        settings=settings, transactions=transactions, merchants=world.merchants,
        wallets=world.wallets, breaks=breaks, now=as_of,
    )
    signals = rules.evaluate(context)

    features = scoring.build_features(
        transactions, signals, world.merchants, world.wallets, breaks, as_of,
    )
    model = scoring.train(features, transactions["is_actionable_label"].astype(int), settings)
    scores = scoring.score(model, features, as_of)

    return DetectionFrames(
        seed=seed, transactions=transactions, signals=signals, scores=scores,
        breaks=breaks, merchants=world.merchants, wallets=world.wallets,
        model_metrics=model.metrics,
    )


def frames_from_warehouse(con, seed: int) -> DetectionFrames:
    """Read an already-built world instead of regenerating it.

    Baselines and ablation only need transactions, signals and scores, all of
    which the last refresh already wrote. Reading them costs a second, where
    regenerating costs a minute - and it has the further advantage of measuring
    the data that is actually on the dashboard rather than a fresh sample of it.
    """
    def read(table: str) -> pd.DataFrame:
        return con.execute(f"SELECT * FROM {table}").fetch_df()

    return DetectionFrames(
        seed=seed,
        transactions=read("core.transactions"),
        signals=read("risk.signals"),
        scores=read("risk.model_scores"),
        breaks=read("core.reconciliation_breaks"),
        merchants=read("core.merchants"),
        wallets=read("core.wallets"),
    )


# ---------------------------------------------------------------------------
# Scoring one set of routing decisions
# ---------------------------------------------------------------------------

def score_routing(labels: pd.Series, routed: pd.Series) -> dict[str, float]:
    """Detection metrics for one boolean 'was this routed to a person' vector.

    One definition, used by the sweep, the baselines, the ablation and the
    threshold explorer - so a number cannot mean one thing on one screen and
    something else on another.
    """
    labels = labels.reset_index(drop=True).astype(bool)
    routed = routed.reset_index(drop=True).astype(bool)

    tp = int((routed & labels).sum())
    fp = int((routed & ~labels).sum())
    fn = int((~routed & labels).sum())
    tn = int((~routed & ~labels).sum())
    total = len(labels)

    def pct(numerator: int, denominator: int) -> float:
        return round(100.0 * numerator / denominator, 2) if denominator else 0.0

    return {
        "recall_pct": pct(tp, tp + fn),
        "precision_pct": pct(tp, tp + fp),
        "false_positive_rate_pct": pct(fp, fp + tn),
        "manual_review_rate_pct": pct(tp + fp, total),
        "auto_release_leakage_pct": pct(fn, fn + tn),
        "actionable_base_rate_pct": pct(tp + fn, total),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def _routed_by_policy(settings: Settings, frames: DetectionFrames,
                      use_model: bool = True, use_rules: bool = True) -> pd.Series:
    """Run the real policy with one evidence source optionally muted."""
    signals = frames.signals if use_rules else frames.signals.iloc[0:0]
    scores = frames.scores.copy()
    if not use_model:
        scores["score"] = 0.0
    decisions = policy.decide_all(settings, frames.transactions, signals, scores)
    order = dict(zip(decisions["transaction_id"], decisions["policy_action"], strict=True))
    return frames.transactions["transaction_id"].map(order).ne("auto_release")


# ---------------------------------------------------------------------------
# 1. Seed sweep
# ---------------------------------------------------------------------------

def run_seed_sweep(settings: Settings, seeds: list[int]) -> dict[str, Any]:
    """Rebuild the world once per seed and report the spread of each metric."""
    runs: list[dict[str, Any]] = []
    for seed in seeds:
        frames = build_detection_frames(settings, seed)
        routed = _routed_by_policy(settings, frames)
        metrics = score_routing(frames.labels, routed)
        metrics["seed"] = seed
        metrics["transactions"] = int(len(frames.transactions))
        metrics["model_test_recall"] = frames.model_metrics.get("test_recall", 0.0)
        runs.append(metrics)

    spread: dict[str, dict[str, float]] = {}
    for metric in SWEEP_METRICS:
        values = [float(run[metric]) for run in runs]
        spread[metric] = {
            "mean": round(statistics.mean(values), 2),
            "stdev": round(statistics.stdev(values), 2) if len(values) > 1 else 0.0,
            "min": round(min(values), 2),
            "max": round(max(values), 2),
        }

    return {
        "seeds": seeds,
        "runs": runs,
        "spread": spread,
        "note": (
            "Each seed is an independently generated world of the same size and scenario mix. "
            "The spread is the variance of this system over that generator, not over real "
            "payment traffic - a different generator would produce a different spread."
        ),
    }


def format_spread(spread: dict[str, dict[str, float]], metric: str) -> str:
    """`96.5% ± 1.2%` - the form a headline number should be quoted in."""
    entry = spread.get(metric)
    if not entry:
        return "n/a"
    return f"{entry['mean']}% ± {entry['stdev']}%"


# ---------------------------------------------------------------------------
# 2. Baselines
# ---------------------------------------------------------------------------

def _model_only_routing(frames: DetectionFrames, threshold: float) -> pd.Series:
    scores = dict(zip(frames.scores["transaction_id"], frames.scores["score"], strict=True))
    return frames.transactions["transaction_id"].map(scores).fillna(0.0) >= threshold


def _threshold_for_review_rate(frames: DetectionFrames, target_rate: float) -> float:
    """The model score cut that reviews the same share of traffic as the target.

    Comparing detectors at their own natural thresholds compares two different
    budgets and tells you nothing. Matching the review rate is what makes
    "which detector finds more, for the same analyst time" a real question.
    """
    scores = frames.scores["score"].sort_values(ascending=False).reset_index(drop=True)
    if scores.empty:
        return 1.0
    index = min(int(len(scores) * target_rate), len(scores) - 1)
    return float(scores.iloc[index])


def run_baselines(settings: Settings, frames: DetectionFrames) -> dict[str, Any]:
    """Rules only, model only, and the two together - compared honestly.

    The subtlety that makes or breaks this comparison: the shipped policy scores
    `0.70 x rule_score + 0.30 x model_score` against a single threshold. Forcing
    the model to zero therefore knocks up to 0.30 off every transaction, and the
    rules face a threshold that was never calibrated for them alone. Reporting
    only that number would credit the model with a gap the arithmetic created.

    So `rules only` is reported twice - once with the thresholds untouched, which
    answers "what happens the day the model is switched off", and once with the
    threshold rescaled by the rule weight, which is what actually isolates the
    model's contribution. The second is the one `model_contribution` uses.
    """
    combined_routed = _routed_by_policy(settings, frames)
    combined = score_routing(frames.labels, combined_routed)

    # (a) Naive: delete the model, change nothing else.
    rules_naive = score_routing(
        frames.labels, _routed_by_policy(settings, frames, use_model=False)
    )

    # (b) Fair: rescale the threshold so a rule score faces the same effective
    #     cut it faced inside the blend.
    rescaled = _with_thresholds(
        settings,
        auto_release_below=settings.auto_release_below * policy.RULE_WEIGHT,
        auto_hold_at_or_above=settings.auto_hold_at_or_above * policy.RULE_WEIGHT,
    )
    rules_fair = score_routing(
        frames.labels, _routed_by_policy(rescaled, frames, use_model=False)
    )

    # (c) Model alone, at the threshold that reviews the same share of traffic.
    target_rate = combined["manual_review_rate_pct"] / 100.0
    threshold = _threshold_for_review_rate(frames, target_rate)
    model_only = score_routing(frames.labels, _model_only_routing(frames, threshold))
    model_only["matched_threshold"] = round(threshold, 4)

    # (d) Model alone with the rules taken out of its features - the only
    #     configuration that answers "can the model find risk the rules did not".
    blind = _model_without_rule_features(settings, frames, target_rate)

    rows = [
        {"configuration": "rules only, thresholds unchanged", **rules_naive},
        {"configuration": "rules only, threshold rescaled", **rules_fair},
        {"configuration": "model only, matched review rate", **model_only},
        {"configuration": "model only without rule features", **blind},
        {"configuration": "rules + model (shipped)", **combined},
    ]

    return {
        "configurations": rows,
        "rule_weight": policy.RULE_WEIGHT,
        "rescaled_release_threshold": round(
            settings.auto_release_below * policy.RULE_WEIGHT, 4
        ),
        "model_contribution": {
            "recall_delta_pct": round(combined["recall_pct"] - rules_fair["recall_pct"], 2),
            "review_rate_delta_pct": round(
                combined["manual_review_rate_pct"] - rules_fair["manual_review_rate_pct"], 2
            ),
            "precision_delta_pct": round(
                combined["precision_pct"] - rules_fair["precision_pct"], 2
            ),
            "measured_against": "rules only, threshold rescaled",
        },
        "naive_comparison": {
            "recall_delta_pct": round(combined["recall_pct"] - rules_naive["recall_pct"], 2),
            "why_it_overstates": (
                "Muting the model removes up to 0.30 from every combined score while the "
                "threshold stays where it was, so part of this gap is the blend arithmetic "
                "rather than anything the model found."
            ),
        },
        "rule_derived_features": list(RULE_DERIVED_FEATURES),
        "note": (
            "`rules only` runs the real policy with the model score forced to zero. `model only` "
            "ignores every rule and cuts on the model score at the threshold that reviews the "
            "same share of traffic as the shipped system - comparing detectors at their own "
            "natural thresholds compares two different budgets and answers nothing. "
            "`model only without rule features` retrains after dropping "
            f"{', '.join(RULE_DERIVED_FEATURES)}, because a model fed the rule engine's own "
            "output is not an independent detector and scoring it as one flatters both."
        ),
    }


def _model_without_rule_features(
    settings: Settings, frames: DetectionFrames, target_rate: float
) -> dict[str, Any]:
    """Retrain on context only, then cut at the matched review rate."""
    as_of = datetime.fromisoformat(settings.as_of_date)
    if frames.merchants.empty or frames.wallets.empty:
        return {"unavailable": True}

    features = scoring.build_features(
        frames.transactions, frames.signals, frames.merchants, frames.wallets,
        frames.breaks, as_of,
    )
    keep = [name for name in scoring.FEATURE_NAMES if name not in RULE_DERIVED_FEATURES]
    # Zeroing rather than dropping keeps the fitted pipeline's shape identical,
    # so the only thing that changes between the two models is the information.
    blinded = features.copy()
    for name in RULE_DERIVED_FEATURES:
        blinded[name] = 0.0

    model = scoring.train(
        blinded, frames.transactions["is_actionable_label"].astype(int), settings
    )
    scored = scoring.score(model, blinded, as_of)
    lookup = dict(zip(scored["transaction_id"], scored["score"], strict=True))
    values = frames.transactions["transaction_id"].map(lookup).fillna(0.0)

    ordered = values.sort_values(ascending=False).reset_index(drop=True)
    index = min(int(len(ordered) * target_rate), max(len(ordered) - 1, 0))
    cut = float(ordered.iloc[index]) if len(ordered) else 1.0

    metrics = score_routing(frames.labels, values >= cut)
    metrics["matched_threshold"] = round(cut, 4)
    metrics["features_used"] = len(keep)
    metrics["model_test_recall"] = model.metrics.get("test_recall", 0.0)
    return metrics


# ---------------------------------------------------------------------------
# 3. Leave-one-rule-out ablation
# ---------------------------------------------------------------------------

def run_ablation(settings: Settings, frames: DetectionFrames) -> dict[str, Any]:
    """Remove one rule at a time and report what routing loses."""
    baseline_routed = _routed_by_policy(settings, frames)
    baseline = score_routing(frames.labels, baseline_routed)

    rows: list[dict[str, Any]] = []
    for rule_id in sorted(frames.signals["rule_id"].unique()) if not frames.signals.empty else []:
        without = frames.signals[frames.signals["rule_id"] != rule_id]
        decisions = policy.decide_all(settings, frames.transactions, without, frames.scores)
        order = dict(zip(decisions["transaction_id"], decisions["policy_action"], strict=True))
        routed = frames.transactions["transaction_id"].map(order).ne("auto_release")
        metrics = score_routing(frames.labels, routed)
        rows.append({
            "rule_id": rule_id,
            "severity": str(rules.RULES[rule_id].severity) if rule_id in rules.RULES else "?",
            "fired": int((frames.signals["rule_id"] == rule_id).sum()),
            "recall_without_pct": metrics["recall_pct"],
            "recall_lost_pct": round(baseline["recall_pct"] - metrics["recall_pct"], 2),
            "review_rate_without_pct": metrics["manual_review_rate_pct"],
            "review_rate_saved_pct": round(
                baseline["manual_review_rate_pct"] - metrics["manual_review_rate_pct"], 2
            ),
        })

    rows.sort(key=lambda row: row["recall_lost_pct"], reverse=True)
    return {
        "baseline": baseline,
        "rules": rows,
        "note": (
            "Ablation removes the rule's signals from the *policy* while keeping the model as it "
            "was trained - with that rule's features included. A full ablation would retrain per "
            "rule and take twenty times as long. So `recall lost` is the rule's direct "
            "contribution to routing, and slightly understates its total contribution."
        ),
    }


# ---------------------------------------------------------------------------
# 4. Threshold sweep - shared with the tuning UI
# ---------------------------------------------------------------------------

def sweep_thresholds(
    settings: Settings,
    transactions: pd.DataFrame,
    signals: pd.DataFrame,
    scores: pd.DataFrame,
    release_grid: list[float],
) -> list[dict[str, Any]]:
    """The recall-against-review-rate curve as the auto-release threshold moves.

    The tuning page calls this too, so the curve a product manager drags a
    slider along is computed by exactly the same code as the curve in the
    evaluation report.
    """
    labels = transactions["is_actionable_label"].astype(bool)
    curve: list[dict[str, Any]] = []
    for release_at in release_grid:
        tuned = _with_thresholds(settings, auto_release_below=release_at)
        decisions = policy.decide_all(tuned, transactions, signals, scores)
        order = dict(zip(decisions["transaction_id"], decisions["policy_action"], strict=True))
        routed = transactions["transaction_id"].map(order).ne("auto_release")
        metrics = score_routing(labels, routed)
        curve.append({"auto_release_below": round(release_at, 3), **metrics})
    return curve


def _with_thresholds(
    settings: Settings,
    auto_release_below: float | None = None,
    auto_hold_at_or_above: float | None = None,
) -> Settings:
    """A copy of settings with the policy thresholds moved. Settings is frozen."""
    from dataclasses import replace

    changes: dict[str, float] = {}
    if auto_release_below is not None:
        changes["auto_release_below"] = float(auto_release_below)
    if auto_hold_at_or_above is not None:
        changes["auto_hold_at_or_above"] = float(auto_hold_at_or_above)
    return replace(settings, **changes)


def policy_mix(
    settings: Settings,
    transactions: pd.DataFrame,
    signals: pd.DataFrame,
    scores: pd.DataFrame,
    auto_release_below: float,
    auto_hold_at_or_above: float,
) -> dict[str, Any]:
    """Full outcome of one threshold pair. Used by the tuning page."""
    tuned = _with_thresholds(settings, auto_release_below, auto_hold_at_or_above)
    decisions = policy.decide_all(tuned, transactions, signals, scores)
    order = dict(zip(decisions["transaction_id"], decisions["policy_action"], strict=True))
    actions = transactions["transaction_id"].map(order)
    labels = transactions["is_actionable_label"].astype(bool)
    metrics = score_routing(labels, actions.ne("auto_release"))

    counts = actions.value_counts().to_dict()
    held = actions.eq("auto_hold")
    return {
        "auto_release_below": round(float(auto_release_below), 3),
        "auto_hold_at_or_above": round(float(auto_hold_at_or_above), 3),
        **metrics,
        "action_counts": {str(k): int(v) for k, v in counts.items()},
        # The number that decides whether a threshold is shippable: benign
        # payments stopped by the machine with nobody having looked first.
        "wrong_auto_holds": int((held & ~labels).sum()),
        "auto_holds": int(held.sum()),
    }
