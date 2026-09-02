"""The decision policy.

A pure function from (signals, model score) to an action. Pure on purpose: a
decision that moves money must not depend on a sampled token, a network call, or
the order rows happened to arrive in. Given the same evidence this returns the
same action today and in an audit six months from now.

The policy is the *only* component that may commit an action without a human,
and it may commit exactly two:

  * `auto_release` - nothing fired above medium severity and residual risk is
    low. Reviewing these by hand would spend the whole team's day on the 80% of
    traffic that is fine.
  * `auto_hold`    - stop the money now. Note what this does and does not do:
    it stops the *payment*, it does not close the *case*. A human still has to
    confirm the outcome, which is what makes a wrong hold recoverable.

Everything else routes to a person. The AI copilot appears nowhere in this file,
which is the design, not an omission.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..config import Settings
from ..taxonomy import SEVERITY_ORDER

POLICY_VERSION = "1.0.0"

# How the two evidence sources are combined. Rules dominate: they are precise
# and auditable, and the model exists to add context, not to overrule them.
RULE_WEIGHT = 0.70
MODEL_WEIGHT = 0.30

ACTIONS = ("auto_release", "manual_review", "request_information", "auto_hold")


@dataclass
class PolicyDecision:
    transaction_id: str
    action: str
    risk_score: float
    risk_band: str
    rule_score: float
    model_score: float
    max_severity: str
    signal_count: int
    primary_family: str
    rationale: str
    reason_codes: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, object]:
        return {
            "transaction_id": self.transaction_id,
            "policy_action": self.action,
            "risk_score": round(self.risk_score, 4),
            "risk_band": self.risk_band,
            "rule_score": round(self.rule_score, 4),
            "model_score": round(self.model_score, 4),
            "max_severity": self.max_severity,
            "signal_count": self.signal_count,
            "primary_reason_family": self.primary_family,
            "policy_rationale": self.rationale,
            "policy_version": POLICY_VERSION,
        }


def band_for_score(value: float) -> str:
    if value >= 0.80:
        return "critical"
    if value >= 0.55:
        return "high"
    if value >= 0.28:
        return "medium"
    return "low"


def rule_score(signals: list[dict]) -> float:
    """Combine rule weights with a noisy-OR, capped at 1.

    Summing weights would let four low-severity signals outrank one critical
    one, which is exactly backwards: a critical signal is critical regardless of
    how much small stuff surrounds it.
    """
    remaining = 1.0
    for signal in signals:
        remaining *= 1.0 - float(signal.get("weight", 0.0))
    return round(1.0 - remaining, 6)


def decide(
    settings: Settings,
    transaction_id: str,
    signals: list[dict],
    model_score: float,
) -> PolicyDecision:
    """The whole policy, in one readable pass."""
    severities = [str(s.get("severity", "low")) for s in signals]
    max_rank = max((SEVERITY_ORDER.get(s, 1) for s in severities), default=0)
    max_severity = next(
        (name for name, rank in SEVERITY_ORDER.items() if rank == max_rank), "none"
    ) if max_rank else "none"

    families = [str(s.get("signal_family", "other")) for s in signals]
    completeness_only = bool(families) and set(families) == {"completeness"}
    has_completeness = "completeness" in families
    has_untrusted_text = any(
        str(s.get("rule_id", "")) in ("R802_UNTRUSTED_INSTRUCTIONS", "R803_AUTHORITY_CLAIM")
        for s in signals
    )

    primary_family = "none"
    if signals:
        ranked = sorted(
            signals,
            key=lambda s: (SEVERITY_ORDER.get(str(s.get("severity", "low")), 1),
                           float(s.get("weight", 0.0))),
            reverse=True,
        )
        primary_family = str(ranked[0].get("signal_family", "other"))

    rules = rule_score(signals)
    combined = round(RULE_WEIGHT * rules + MODEL_WEIGHT * float(model_score), 6)
    # A critical deterministic signal cannot be averaged away by a calm model.
    if max_rank >= SEVERITY_ORDER["critical"]:
        combined = max(combined, 0.80)
    band = band_for_score(combined)

    high_or_worse = sum(1 for s in severities if SEVERITY_ORDER.get(s, 1) >= SEVERITY_ORDER["high"])

    # 1. Missing evidence beats everything short of a critical signal. You
    #    cannot judge what you cannot see, and guessing is the failure mode this
    #    product exists to avoid.
    if has_completeness and max_rank < SEVERITY_ORDER["critical"] and (
        completeness_only or high_or_worse <= 1
    ):
        detail = "evidence needed before this case can be decided"
        if has_untrusted_text:
            detail = (
                "merchant free text is untrusted and was quarantined; the case needs "
                "evidence from a person, not from the note"
            )
        return PolicyDecision(
            transaction_id, "request_information", combined, band, rules, model_score,
            max_severity, len(signals), primary_family,
            f"{detail} ({len(signals)} signal(s), highest severity {max_severity})",
            ["RC_INSUFFICIENT_EVIDENCE"],
        )

    # 2. Auto-hold: two independent high-or-worse signals, or one critical, with
    #    a combined score at the hold threshold. The payment stops; the case does
    #    not close.
    over_threshold = combined >= settings.auto_hold_at_or_above
    critical_pattern = max_rank >= SEVERITY_ORDER["critical"] and high_or_worse >= 2
    if over_threshold or critical_pattern:
        # Say which of the two branches actually fired. A rationale that always
        # quotes the score threshold is wrong half the time, and an analyst who
        # notices that stops trusting the rest of the screen.
        if over_threshold:
            why = (
                f"combined score {combined:.2f} is at or above the "
                f"{settings.auto_hold_at_or_above:.2f} auto-hold threshold"
            )
        else:
            why = (
                f"a critical signal alongside {high_or_worse - 1} other signal(s) at high "
                f"severity or above; combined score {combined:.2f}. Two independent strong "
                "signals stop the payment regardless of the score"
            )
        return PolicyDecision(
            transaction_id, "auto_hold", combined, band, rules, model_score,
            max_severity, len(signals), primary_family,
            f"{len(signals)} signal(s), highest severity {max_severity}: {why}. "
            "The payment is stopped and a human must confirm the outcome.",
            [],
        )

    # 3. Auto-release: quiet on both sources.
    if not signals or (
        combined < settings.auto_release_below and max_rank < SEVERITY_ORDER["high"]
    ):
        return PolicyDecision(
            transaction_id, "auto_release", combined, band, rules, model_score,
            max_severity, len(signals), primary_family,
            f"combined score {combined:.2f} is below the {settings.auto_release_below:.2f} "
            f"auto-release threshold and nothing fired above {max_severity or 'none'} severity",
            ["RC_POLICY_AUTO"],
        )

    # 4. Everything else is a person's call.
    return PolicyDecision(
        transaction_id, "manual_review", combined, band, rules, model_score,
        max_severity, len(signals), primary_family,
        f"{len(signals)} signal(s), highest severity {max_severity}, combined score "
        f"{combined:.2f} - between the auto-release and auto-hold thresholds, so a human decides",
        [],
    )


def decide_all(
    settings: Settings,
    transactions: pd.DataFrame,
    signals: pd.DataFrame,
    scores: pd.DataFrame,
) -> pd.DataFrame:
    """Apply the policy to every transaction."""
    if transactions.empty:
        return pd.DataFrame(columns=list(PolicyDecision("", "", 0, "", 0, 0, "", 0, "", "").as_row()))

    signals_by_txn: dict[str, list[dict]] = {}
    for record in signals.to_dict("records"):
        signals_by_txn.setdefault(str(record["transaction_id"]), []).append(record)

    score_by_txn = (
        dict(zip(scores["transaction_id"].astype(str), scores["score"].astype(float), strict=True))
        if not scores.empty else {}
    )

    rows = []
    for txn_id in transactions["transaction_id"].astype(str):
        decision = decide(
            settings,
            txn_id,
            signals_by_txn.get(txn_id, []),
            score_by_txn.get(txn_id, 0.0),
        )
        rows.append(decision.as_row())
    return pd.DataFrame(rows)
