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
        # A packet carrying a question is a follow-up; one without is a brief.
        payload = (
            _answer_from_packet(packet) if packet.get("question")
            else _brief_from_packet(packet)
        )
        latency = (time.perf_counter() - started) * 1000
        return ProviderResponse(
            text=json.dumps(payload, ensure_ascii=False),
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


# ---------------------------------------------------------------------------
# The mock's follow-up answering.
#
# Deliberately narrow: it recognises a handful of intents it can ground in the
# packets and **declines everything else by name**. That is not a shortcut -
# declining well is the behaviour the evaluation measures, and a mock that
# improvised an answer to every question would hide exactly the failure the
# guardrails exist to catch.
# ---------------------------------------------------------------------------

# Fields a question can plausibly ask for that this system provably does not
# hold. Checked FIRST, and they win over every intent below.
#
# This list is the difference between a useful assistant and a dangerous one.
# "What is the payer's credit score?" contains the word "payer", and a naive
# keyword router answers it with the wallet's payment history - fluent, cited,
# and an answer to a different question. A reviewer skimming at 02:14 reads the
# confident paragraph, not the mismatch between it and what they asked.
_UNAVAILABLE_CONCEPTS: tuple[tuple[str, str], ...] = (
    ("credit score", "a credit score"),
    ("credit rating", "a credit rating"),
    ("sanction", "sanctions or watchlist screening"),
    ("watchlist", "sanctions or watchlist screening"),
    ("blacklist", "a blocklist"),
    ("blocklist", "a blocklist"),
    ("pep ", "politically-exposed-person status"),
    ("criminal", "criminal records"),
    ("court", "court records"),
    ("kyc document", "KYC documents"),
    ("passport", "identity documents"),
    ("id document", "identity documents"),
    ("phone number", "contact details"),
    ("email address", "contact details"),
    ("home address", "contact details"),
    ("real name", "the payer's identity"),
    ("who is the customer", "the payer's identity"),
    ("income", "income data"),
    ("salary", "income data"),
    ("social media", "social media data"),
    ("ip address", "raw IP addresses (only the country is retained)"),
    ("chargeback ratio", "a merchant chargeback ratio"),
    ("信用分", "a credit score"),
    ("制裁", "sanctions or watchlist screening"),
    ("黑名单", "a blocklist"),
    ("身份证", "identity documents"),
    ("真实姓名", "the payer's identity"),
    ("手机号", "contact details"),
)

# Intents, ordered most specific first. Order matters and is the fix for a real
# bug: "explain the signal in language I can send to the merchant" and "which
# merchants share this payout account" both contain "merchant", so the generic
# entity intents have to be checked last or they swallow everything.
#
# Matched in English and Chinese, because the workbench is used in both and an
# analyst types in whichever they are thinking in.
_INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("explain_signal", ("explain", "why did", "why does", "what does this signal",
                        "in plain", "in language", "解释", "为什么", "什么意思")),
    ("payout_group", ("payout", "beneficiary", "settle to", "same account",
                      "share this account", "shared account", "ring",
                      "收款账户", "结算账户", "关联账户")),
    ("appeal", ("appeal", "申诉", "上诉")),
    ("decision_history", ("who decided", "decision history", "decision trail",
                          "previously decided", "audit trail", "谁决定", "决策记录")),
    ("missing_info", ("missing", "what should i ask", "what do i need", "evidence needed",
                      "what evidence", "缺什么", "补充材料", "需要什么")),
    ("recommendation", ("recommend", "what would you", "your view", "should i",
                        "建议", "你认为", "怎么看")),
    ("money", ("amount", "how much", "settled", "settle", "fee", "fx", "exchange rate",
               "reconcil", "金额", "多少钱", "结算", "手续费", "汇率")),
    ("wallet_geography", ("countr", "travel", "where has", "geograph",
                          "国家", "地区", "旅行", "在哪")),
    ("merchant_history", ("merchant", "seller", "this shop", "商户", "商家")),
    ("wallet_history", ("wallet", "payer", "this customer", "been here before",
                        "seen before", "钱包", "付款人", "之前", "历史")),
)


def _match_intent(question: str) -> str:
    """The intent, or `unavailable:<what>` when the packets provably lack it."""
    lowered = question.lower()
    for concept, description in _UNAVAILABLE_CONCEPTS:
        if concept in lowered:
            return f"unavailable:{description}"
    for intent, keywords in _INTENT_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return intent
    return "unknown"


def _decline(reason: str, needed: str) -> dict:
    return {
        "answered": False,
        "answer": "",
        "citations": [],
        "intent": "declined",
        "decline_reason": f"{reason} {needed}".strip(),
    }


def _answer_from_packet(packet: dict) -> dict:
    question = str(packet.get("question", ""))
    intent = _match_intent(question)

    wallet = packet.get("wallet_ctx", {})
    merchant = packet.get("merchant_ctx", {})
    group = packet.get("payout_group", {})
    txn = packet.get("transaction", {})
    signals = packet.get("signals", [])
    missing = packet.get("missing_information", [])

    if intent.startswith("unavailable:"):
        wanted = intent.split(":", 1)[1]
        return _decline(
            f"This system does not hold {wanted}, so I have nothing to answer that from.",
            "Answering it from the payment history I do have would be answering a different "
            "question than the one you asked, which is worse than saying no.",
        )

    if intent == "unknown":
        return _decline(
            "That is not something the case packet or the entity context contains, so answering "
            "it would mean inventing it.",
            "I can speak to this wallet's and merchant's history, the payout group, the signals "
            "on this case, the money and FX, what evidence is missing, and the decision and "
            "appeal trail.",
        )

    if intent == "wallet_history":
        total = int(wallet.get("transactions_total", 0))
        if total <= 1:
            return _decline(
                "This wallet has no history in the current window - this is the only payment on "
                "file for it.",
                "That absence is itself worth noting: nothing about this payer can be called "
                "abnormal, because there is no baseline to compare it against.",
            )
        outcomes = wallet.get("prior_case_outcomes", {})
        outcome_text = (
            ", ".join(f"{count} {action}" for action, count in outcomes.items())
            if outcomes else "no case has been resolved yet"
        )
        return {
            "answered": True,
            "intent": intent,
            "answer": (
                f"This wallet has {total} payments in the window, first seen "
                f"{wallet.get('first_seen', 'unknown')} and last seen "
                f"{wallet.get('last_seen', 'unknown')}. It has opened "
                f"{wallet.get('cases_opened', 0)} case(s), resolving as: {outcome_text}. "
                f"It has paid from {wallet.get('devices_used', 0)} distinct device(s)."
            ),
            "citations": ["wallet_ctx.transactions_total", "wallet_ctx.cases_opened",
                          "wallet_ctx.prior_case_outcomes", "wallet_ctx.first_seen",
                          "wallet_ctx.last_seen", "wallet_ctx.devices_used"],
            "decline_reason": "",
        }

    if intent == "wallet_geography":
        countries = wallet.get("countries_paid_from", [])
        if not countries:
            return _decline("No IP geography is on file for this wallet's payments.",
                            "A device fingerprint or IP record would be needed.")
        recent = wallet.get("recent_transactions", [])
        trail = "; ".join(
            f"{row.get('date', '')[:10]} from {row.get('ip_country', '?')}"
            for row in recent[:5]
        )
        return {
            "answered": True,
            "intent": intent,
            "answer": (
                f"This wallet has paid from {len(countries)} countr"
                f"{'y' if len(countries) == 1 else 'ies'}: {', '.join(countries)}. "
                f"The wallet itself is registered in {txn.get('wallet_country', 'unknown')}. "
                f"Most recent payments: {trail}."
            ),
            "citations": ["wallet_ctx.countries_paid_from", "wallet_ctx.recent_transactions",
                          "txn.wallet_country"],
            "decline_reason": "",
        }

    if intent == "merchant_history":
        outcomes = merchant.get("prior_case_outcomes", {})
        outcome_text = (
            ", ".join(f"{count} {action}" for action, count in outcomes.items())
            if outcomes else "nothing resolved yet"
        )
        return {
            "answered": True,
            "intent": intent,
            "answer": (
                f"{merchant.get('merchant_id', 'This merchant')} is a "
                f"{merchant.get('risk_tier', 'unknown')}-tier "
                f"{merchant.get('mcc_description', 'uncategorised')} merchant, onboarded "
                f"{merchant.get('onboarded_at', 'unknown')[:10]}. It has "
                f"{merchant.get('transactions_total', 0)} payments in the window and "
                f"{merchant.get('cases_opened', 0)} case(s): {outcome_text}."
            ),
            "citations": ["merchant_ctx.merchant_id", "merchant_ctx.risk_tier",
                          "merchant_ctx.mcc_description", "merchant_ctx.onboarded_at",
                          "merchant_ctx.transactions_total", "merchant_ctx.cases_opened",
                          "merchant_ctx.prior_case_outcomes"],
            "decline_reason": "",
        }

    if intent == "payout_group":
        if not group.get("is_shared"):
            return {
                "answered": True,
                "intent": intent,
                "answer": (
                    f"The payout account {group.get('payout_account_id', 'on file')} is used by "
                    "this merchant only. There is no shared-beneficiary linkage on this case."
                ),
                "citations": ["payout_group.payout_account_id", "payout_group.is_shared"],
                "decline_reason": "",
            }
        peers = [m for m in group.get("merchant_ids", []) if m != merchant.get("merchant_id")]
        return {
            "answered": True,
            "intent": intent,
            "answer": (
                f"Payout account {group.get('payout_account_id')} is shared by "
                f"{group.get('merchant_count', 0)} merchants. Besides this one: "
                f"{', '.join(peers[:8]) or 'none listed'}. Worth saying plainly: a franchise "
                "group and a fraud ring look identical at this level, so this is a reason to "
                "look wider, not a finding on its own."
            ),
            "citations": ["payout_group.payout_account_id", "payout_group.merchant_count",
                          "payout_group.merchant_ids", "payout_group.is_shared"],
            "decline_reason": "",
        }

    if intent == "explain_signal":
        if not signals:
            return _decline("No rule fired on this transaction, so there is no signal to explain.",
                            "")
        strongest = signals[0]
        return {
            "answered": True,
            "intent": intent,
            "answer": (
                f"{strongest.get('title')} ({strongest.get('rule_id')}, "
                f"{strongest.get('severity')} severity). What it found: "
                f"{strongest.get('detail')}. In plainer terms, this is a "
                f"{strongest.get('family', 'risk')} observation, and it is computed from "
                f"{strongest.get('evidence_fields', '').replace('|', ', ')} - so it can be "
                "shown to the merchant field by field."
            ),
            "citations": [f"signal.{strongest.get('rule_id')}"],
            "decline_reason": "",
        }

    if intent == "missing_info":
        if not missing:
            return {
                "answered": True,
                "intent": intent,
                "answer": (
                    "Nothing required is missing from this case. Device fingerprint, merchant "
                    "category and payer history are all present, so it can be decided as it "
                    "stands."
                ),
                "citations": ["txn.device_present", "merchant.mcc"],
                "decline_reason": "",
            }
        return {
            "answered": True,
            "intent": intent,
            "answer": "The case is missing: " + " ".join(missing),
            "citations": ["txn.device_present", "merchant.mcc", "wallet.lifetime_txn_count"],
            "decline_reason": "",
        }

    if intent == "appeal":
        history = packet.get("appeal_history", [])
        if not history:
            return _decline("No appeal has been filed on this case.",
                            "An appeal can be filed once the case is resolved.")
        latest = history[-1]
        return {
            "answered": True,
            "intent": intent,
            "answer": (
                f"{len(history)} appeal(s) on this case. The most recent was filed "
                f"{latest.get('filed_at', '')[:10]} by the {latest.get('claimant', 'claimant')} "
                f"with {latest.get('evidence_type', 'evidence')}, outcome: "
                f"{latest.get('outcome', 'open')}."
            ),
            "citations": ["appeal_history"],
            "decline_reason": "",
        }

    if intent == "decision_history":
        history = packet.get("case_history", [])
        if not history:
            return _decline("No decision has been committed on this case yet.",
                            "The decision controls below are where that happens.")
        parts = [
            f"{row.get('decided_at', '')[:16]} - {row.get('actor_role', '')} "
            f"{row.get('action', '')} ({row.get('reason_code', '')})"
            for row in history
        ]
        return {
            "answered": True,
            "intent": intent,
            "answer": "Decision trail: " + "; ".join(parts) + ".",
            "citations": ["case_history"],
            "decline_reason": "",
        }

    if intent == "recommendation":
        severities = [str(s.get("severity")) for s in signals]
        rule_ids = {str(s.get("rule_id")) for s in signals}
        model_score = float(packet.get("model", {}).get("score", 0.0) or 0.0)
        action, confidence, rationale = _suggest(
            missing=missing, rule_ids=rule_ids,
            critical=severities.count("critical"), high=severities.count("high"),
            has_signals=bool(signals), rule_strength=_rule_strength(severities),
            model_score=model_score,
        )
        if action == "abstain":
            return {
                "answered": True,
                "intent": intent,
                "answer": f"I would not recommend an action here. {rationale}",
                "citations": ["model.score"] + [f"signal.{r}" for r in sorted(rule_ids)][:3],
                "decline_reason": "",
            }
        return {
            "answered": True,
            "intent": intent,
            "answer": (
                f"I would suggest **{action}** at confidence {confidence:.2f}. {rationale} "
                "That is a recommendation, not a decision - committing it is yours to do, and "
                "the audit log will record it under your name, not mine."
            ),
            "citations": ["model.score", "case.policy_action"]
            + [f"signal.{r}" for r in sorted(rule_ids)][:3],
            "decline_reason": "",
        }

    if intent == "money":
        return {
            "answered": True,
            "intent": intent,
            "answer": (
                f"Authorised {txn.get('authorized_display', '-')}, captured "
                f"{txn.get('amount_display', '-')}, fee {txn.get('fee_display', '-')}, settled "
                f"{txn.get('settled_display', '-')}. The quote on file is "
                f"{txn.get('quoted_fx_rate', '-')} and settlement was computed at "
                f"{txn.get('applied_fx_rate', '-')}. These are quoted from the record - I do not "
                "recompute money."
            ),
            "citations": ["txn.authorized_display", "txn.amount_display", "txn.fee_display",
                          "txn.settled_display", "txn.quoted_fx_rate", "txn.applied_fx_rate"],
            "decline_reason": "",
        }

    return _decline("I could not ground an answer to that in the packets available.", "")


def input_digest(payload: str) -> str:
    """A digest of what was sent, so a brief can be tied to its exact input."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
