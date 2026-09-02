"""Model provider abstraction.

Two providers ship:

  * **`mock`** - the default, and the one every test and the whole demo use. It
    is deterministic: same case packet, same brief, no network, no key. That is
    not a stub standing in for the real thing; it is what makes the *workflow and
    guardrail* layer measurable independently of a model's mood on the day.
  * **`openai_compatible`** - any OpenAI-shaped chat-completions endpoint,
    configured entirely by environment variable. Opt-in, never required.

The product must run end to end with no API key. Nothing in the case flow,
the dashboard or the evaluation depends on a paid service being reachable.

What the mock provider is not: it is not a language model, and no number
produced under it should be described as a measurement of one. The evaluation
report labels every metric with the provider that produced it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from ..config import Settings

LOGGER = logging.getLogger(__name__)

MOCK_MODEL_VERSION = "mock-deterministic-v1"


@dataclass
class ProviderResponse:
    text: str
    model_version: str
    provider: str
    latency_ms: float
    degraded: bool = False
    error: str = ""


class LLMProvider(Protocol):
    name: str

    def complete(self, system_prompt: str, user_prompt: str) -> ProviderResponse:
        ...


class MockProvider:
    """A deterministic stand-in that reasons over the packet with plain code.

    It reads the same case packet the real prompt carries and assembles a brief
    from it. Everything it states is therefore grounded by construction, which
    is the point: it gives the guardrails, the workflow and the UI a realistic
    input without pretending to be a language model.
    """

    name = "mock"

    def __init__(self, settings: Settings):
        self.settings = settings

    def complete(self, system_prompt: str, user_prompt: str) -> ProviderResponse:
        started = time.perf_counter()
        try:
            packet = json.loads(user_prompt)
        except json.JSONDecodeError:
            packet = {}
        brief = _brief_from_packet(packet)
        latency = (time.perf_counter() - started) * 1000
        return ProviderResponse(
            text=json.dumps(brief, ensure_ascii=False),
            model_version=MOCK_MODEL_VERSION,
            provider=self.name,
            latency_ms=latency,
        )


class OpenAICompatibleProvider:
    """Any OpenAI-shaped `/chat/completions` endpoint.

    Uses `urllib` rather than adding an SDK dependency: one HTTP POST with a
    JSON body does not justify a package, and a reviewer can read exactly what
    leaves the process.
    """

    name = "openai_compatible"

    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.llm_api_key:
            raise ValueError(
                "RISKOPS_LLM_PROVIDER=openai_compatible needs RISKOPS_LLM_API_KEY. "
                "Leave the provider as `mock` to run without any key."
            )
        self.base_url = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")

    def complete(self, system_prompt: str, user_prompt: str) -> ProviderResponse:
        started = time.perf_counter()
        body = json.dumps({
            "model": self.settings.llm_model,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.llm_api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.llm_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = payload["choices"][0]["message"]["content"]
            return ProviderResponse(
                text=text,
                model_version=str(payload.get("model", self.settings.llm_model)),
                provider=self.name,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
            # Fail closed but loudly: the caller turns a degraded response into
            # an abstention, so an unreachable model can never be mistaken for a
            # confident recommendation.
            LOGGER.warning("provider call failed: %s", exc)
            return ProviderResponse(
                text="",
                model_version=self.settings.llm_model,
                provider=self.name,
                latency_ms=(time.perf_counter() - started) * 1000,
                degraded=True,
                error=str(exc),
            )


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockProvider(settings)
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleProvider(settings)
    raise ValueError(
        f"unknown provider {settings.llm_provider!r}; expected 'mock' or 'openai_compatible'"
    )


# ---------------------------------------------------------------------------
# The mock's reasoning. Kept here, not in the copilot, so the copilot's
# guardrail and assembly path is identical whichever provider produced the text.
# ---------------------------------------------------------------------------

_SEVERITY_SENTENCE = {
    "critical": "This is the strongest signal on the case and cannot be averaged away by the others.",
    "high": "This is a substantive signal that needs an explanation before the payment proceeds.",
    "medium": "This is context rather than proof; it matters in combination.",
    "low": "This is a data-quality observation, not a risk claim.",
}


def _brief_from_packet(packet: dict) -> dict:
    txn = packet.get("transaction", {})
    signals = packet.get("signals", [])
    breaks = packet.get("reconciliation_breaks", [])
    missing = packet.get("missing_information", [])
    merchant = packet.get("merchant", {})
    wallet = packet.get("wallet", {})
    model = packet.get("model", {})

    key_facts = []
    if txn.get("amount_display"):
        key_facts.append({
            "statement": (
                f"A {txn.get('corridor', 'payment')} of {txn['amount_display']} reached "
                f"{txn.get('payment_state', 'an unknown state')} on merchant "
                f"{txn.get('merchant_id')} ({merchant.get('mcc_description', 'category unknown')})."
            ),
            "citations": ["txn.captured_minor", "txn.presentment_currency", "txn.payment_state",
                          "txn.merchant_id", "merchant.mcc_description"],
        })
    if txn.get("ip_country") and txn.get("wallet_country"):
        key_facts.append({
            "statement": (
                f"The wallet is registered in {txn['wallet_country']} and the payment came from "
                f"{txn['ip_country']}; the merchant is in {txn.get('merchant_country', 'unknown')}."
            ),
            "citations": ["txn.wallet_country", "txn.ip_country", "txn.merchant_country"],
        })
    if wallet.get("kyc_level"):
        key_facts.append({
            "statement": (
                f"The payer's wallet has {wallet['kyc_level']} verification and about "
                f"{wallet.get('lifetime_txn_count', 0)} lifetime payments."
            ),
            "citations": ["wallet.kyc_level", "wallet.lifetime_txn_count"],
        })

    explanations = []
    for signal in signals:
        explanations.append({
            "statement": (
                f"{signal.get('title')}: {signal.get('detail')}. "
                + _SEVERITY_SENTENCE.get(str(signal.get("severity")), "")
            ),
            "citations": [f"signal.{signal.get('rule_id')}"] + [
                f"txn.{name}" for name in str(signal.get("evidence_fields", "")).split("|") if name
            ],
        })

    conflicts = []
    for record in breaks:
        conflicts.append({
            "statement": (
                f"{record.get('break_type')}: expected {record.get('expected_display')} but the "
                f"record shows {record.get('observed_display')} "
                f"({record.get('difference_bps')} bps apart)."
            ),
            "citations": [f"break.{record.get('break_type')}"],
        })
    if txn.get("quoted_fx_rate") and txn.get("applied_fx_rate") and \
            txn["quoted_fx_rate"] != txn["applied_fx_rate"]:
        conflicts.append({
            "statement": (
                f"The quote on file is {txn['quoted_fx_rate']} but settlement was computed at "
                f"{txn['applied_fx_rate']}. Those are two different prices for one payment."
            ),
            "citations": ["txn.quoted_fx_rate", "txn.applied_fx_rate"],
        })

    questions = list(packet.get("suggested_questions", []))

    severities = [str(s.get("severity")) for s in signals]
    rule_ids = {str(s.get("rule_id")) for s in signals}
    critical = severities.count("critical")
    high = severities.count("high")
    model_score = float(model.get("score", 0.0)) if model else 0.0
    rule_strength = _rule_strength(severities)

    action, confidence, rationale = _suggest(
        missing=missing,
        rule_ids=rule_ids,
        critical=critical,
        high=high,
        has_signals=bool(signals),
        rule_strength=rule_strength,
        model_score=model_score,
    )

    summary_bits = [
        f"{len(signals)} rule signal(s)",
        f"{len(breaks)} reconciliation break(s)",
        f"residual model score {model.get('score', 0):.2f}" if model else "",
    ]
    # Rank, not lexicographic order: `max(["critical", "high", "medium"])` is
    # "medium", which would report a critical case as a moderate one.
    highest = max(severities, key=lambda s: _SEVERITY_WEIGHT.get(s, 0.0), default="none")
    summary = (
        f"{txn.get('transaction_id', 'This transaction')}: "
        + ", ".join(bit for bit in summary_bits if bit)
        + f". Highest severity {highest}."
    )

    return {
        "summary": summary,
        "key_facts": key_facts,
        "signal_explanations": explanations,
        "conflicts": conflicts,
        "missing_information": missing,
        "suggested_questions": questions,
        "recommended_action": action,
        "confidence": confidence,
        "rationale": rationale,
    }


# Which action each finding *suggests to a reviewer*. This is ordinary product
# routing logic - who owns this kind of problem - and it is what a real prompt
# would encode too.
#
# Be careful reading the agreement metric that comes out of this. The generator
# derives each scenario's `expected_action` from the same domain reasoning, so
# agreement between the two measures internal consistency, not model skill. The
# evaluation report states that where the number appears.
RULE_SUGGESTION: dict[str, str] = {
    # An operations problem: the money is wrong and somebody has to explain it.
    "R102_AUTH_CAPTURE_GAP": "request_information",
    "R103_QUARANTINED_EVENTS": "request_information",
    "R401_FX_OUT_OF_TOLERANCE": "request_information",
    "R402_FEE_OFF_SCHEDULE": "request_information",
    "R403_STALE_FX_QUOTE": "request_information",
    "R801_MISSING_EVIDENCE": "request_information",
    "R802_UNTRUSTED_INSTRUCTIONS": "request_information",
    # Stop the money: a real customer is being debited twice or defrauded now.
    "R101_DUPLICATE_IDEMPOTENCY": "hold",
    "R201_WALLET_VELOCITY": "hold",
    "R202_AMOUNT_ANOMALY": "hold",
    "R302_DEVICE_HOPPING": "hold",
    "R303_IMPOSSIBLE_TRAVEL": "hold",
    "R602_DOUBLE_CREDIT": "hold",
    # Somebody is trying to talk the system into a release. That is a reason to
    # stop, not a reason to ask.
    "R803_AUTHORITY_CLAIM": "hold",
    # Wider than one payment: a jurisdiction question, a merchant changing shape,
    # a laundering pattern, a ring. These belong with an investigator.
    "R304_JURISDICTION_CONFLICT": "escalate",
    "R501_MCC_TICKET_ANOMALY": "escalate",
    "R601_REFUND_CHAIN": "escalate",
    "R701_SHARED_PAYOUT_ACCOUNT": "escalate",
    "R702_SHARED_FUNDING_ACCOUNT": "escalate",
    # Context only.
    "R301_GEO_MISMATCH": "release",
}

# Escalation outranks a hold. Escalating already stops the payment pending an
# investigator, and it carries the extra fact that the problem is wider than this
# transaction - which a plain hold would throw away.
_ACTION_PRIORITY = ("escalate", "hold", "request_information", "release")

_SEVERITY_WEIGHT = {"low": 0.10, "medium": 0.25, "high": 0.45, "critical": 0.70}

# The band in which the residual-risk model is genuinely undecided. Outside it
# the model has an opinion, even if a weak one.
AMBIGUOUS_MODEL_BAND = (0.35, 0.65)


def _rule_strength(severities: list[str]) -> float:
    remaining = 1.0
    for severity in severities:
        remaining *= 1.0 - _SEVERITY_WEIGHT.get(severity, 0.10)
    return round(1.0 - remaining, 4)


def _suggest(
    *,
    missing: list,
    rule_ids: set[str],
    critical: int,
    high: int,
    has_signals: bool,
    rule_strength: float,
    model_score: float,
) -> tuple[str, float, str]:
    """The mock's recommendation, its confidence, and why."""
    # 1. Thin evidence in the model's undecided band is not a tie to be broken by
    #    picking the louder side. It is a case where the honest answer is that
    #    the evidence does not point anywhere, and a reviewer should not be
    #    anchored by a recommendation that is really a coin flip.
    #
    #    Note what this deliberately does *not* do: it does not abstain merely
    #    because `rule_strength` and `model_score` are numerically far apart.
    #    Those two are on different scales - one is a severity weight, the other
    #    a probability - and treating the gap between them as disagreement makes
    #    the copilot abstain on every ordinary medium-severity case.
    low_band, high_band = AMBIGUOUS_MODEL_BAND
    if (
        has_signals
        and critical == 0
        and high == 0
        and rule_strength <= 0.30
        and low_band <= model_score <= high_band
        and not missing
    ):
        return (
            "abstain", 0.0,
            f"Only context-level signals fired (combined strength {rule_strength:.2f}) and the "
            f"residual-risk model is undecided at {model_score:.2f}. Nothing here points either "
            "way, and a recommendation at this point would be a coin flip a reviewer would "
            "reasonably mistake for a judgement.",
        )

    # 2. Missing evidence beats everything except a signal that stops the money.
    stop_now = {"R101_DUPLICATE_IDEMPOTENCY", "R303_IMPOSSIBLE_TRAVEL", "R602_DOUBLE_CREDIT",
                "R803_AUTHORITY_CLAIM"}
    if missing and not (rule_ids & stop_now):
        return (
            "request_information", 0.72,
            "The case is missing evidence a decision depends on. Asking is the correct next "
            "step; recommending release or hold here would be a guess dressed as a judgement.",
        )

    if not has_signals:
        return "release", 0.55, "No rule fired on this transaction."

    # 3. Otherwise the strongest suggestion among the findings wins.
    suggestions = {RULE_SUGGESTION.get(rule_id, "request_information") for rule_id in rule_ids}
    action = next((a for a in _ACTION_PRIORITY if a in suggestions), "request_information")

    if action == "hold":
        confidence = 0.81 if (critical >= 1 and high >= 1) else 0.66 if high >= 1 else 0.60
        rationale = (
            f"{critical} critical and {high} high-severity signal(s) that stop the money, with "
            "nothing on file explaining them. A person still has to commit this."
        )
    elif action == "escalate":
        confidence = 0.70 if high >= 1 else 0.62
        rationale = (
            "The risk extends past this transaction - a linked entity, a jurisdiction question "
            "or a merchant changing shape - so it is wider than one case."
        )
    elif action == "request_information":
        confidence = 0.72
        rationale = (
            "The money does not reconcile, or the record cannot be judged as it stands. Someone "
            "has to explain the gap before the case can be decided."
        )
    else:
        confidence = 0.58
        rationale = (
            "Only context-level signals fired, and each has an ordinary explanation consistent "
            "with the payer's history."
        )
    return action, confidence, rationale


def input_digest(payload: str) -> str:
    """A digest of what was sent, so a brief can be tied to its exact input."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
