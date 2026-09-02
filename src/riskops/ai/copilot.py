"""The Risk Investigation Copilot.

One function does the whole job, in a fixed order that the audit record mirrors:

    screen untrusted text -> build packet -> call provider -> parse -> guardrails
    -> record the invocation -> return an advisory brief

Everything that could go wrong ends in an abstention rather than an exception: a
provider timeout, malformed JSON, an attempted authority claim, a citation that
does not resolve. A risk queue that stops working because a model returned bad
JSON is worse than a risk queue with one blank brief in it.

The copilot returns a recommendation. It never returns a decision, and
`audit.log.record_decision` will refuse to write one on its behalf.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import duckdb

from ..config import Settings
from ..risk.rules import RULES_VERSION
from .guardrails import GUARDRAIL_VERSION, apply_output_guardrails
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, render_user_prompt
from .provider import LLMProvider, build_provider, input_digest
from .schema import CaseBrief, GuardrailReport, abstention

LOGGER = logging.getLogger(__name__)


def investigate(
    settings: Settings,
    *,
    case_id: str,
    transaction_id: str,
    packet: dict,
    allowed_citations: set[str],
    input_gate,
    provider: LLMProvider | None = None,
    requested_by: str = "unknown",
) -> CaseBrief:
    """Produce one advisory brief for one case."""
    engine = provider or build_provider(settings)
    user_prompt = render_user_prompt(packet)

    response = engine.complete(SYSTEM_PROMPT, user_prompt)

    if response.degraded or not response.text.strip():
        return abstention(
            case_id, transaction_id,
            "The model provider was unreachable or returned nothing, so no brief was produced. "
            "The case is unaffected: rules, policy and the human queue do not depend on it.",
            provider=response.provider,
            model_version=response.model_version,
            prompt_version=PROMPT_VERSION,
            guardrail=GuardrailReport(
                "rejected",
                [f"provider degraded: {response.error or 'empty response'}"],
                injection_verdict=input_gate.verdict,
                injection_labels=input_gate.labels,
            ),
        )

    payload = _parse(response.text)
    if payload is None:
        return abstention(
            case_id, transaction_id,
            "The model returned something that is not a valid brief, so nothing was shown. "
            "Malformed output is treated as no output, never as a partial recommendation.",
            provider=response.provider,
            model_version=response.model_version,
            prompt_version=PROMPT_VERSION,
            guardrail=GuardrailReport(
                "rejected",
                ["response was not parseable as the brief schema"],
                injection_verdict=input_gate.verdict,
                injection_labels=input_gate.labels,
            ),
        )

    payload.update({
        "case_id": case_id,
        "transaction_id": transaction_id,
        "provider": response.provider,
        "model_version": response.model_version,
        "prompt_version": PROMPT_VERSION,
        "rules_version": RULES_VERSION,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "latency_ms": response.latency_ms,
    })
    brief = CaseBrief.from_dict(payload)
    return apply_output_guardrails(brief, allowed_citations, settings, input_gate)


def _parse(text: str) -> dict | None:
    """Parse the provider's text, tolerating a fenced code block around it."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = cleaned.removeprefix("json").strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def record_invocation(
    con: duckdb.DuckDBPyConnection,
    brief: CaseBrief,
    *,
    packet: dict,
    requested_by: str,
    created_at: datetime | None = None,
) -> str:
    """Write the invocation to the audit schema.

    Everything needed to reproduce or challenge the brief is stored: provider,
    model version, prompt version, rules version, a digest of the exact input,
    both guardrail verdicts, and the brief itself.
    """
    when = created_at or datetime.now().replace(microsecond=0)
    digest = input_digest(render_user_prompt(packet))
    invocation_id = f"AIV_{brief.case_id}_{digest[:8]}"

    con.execute(
        """
        INSERT INTO audit.ai_invocations
            (invocation_id, case_id, transaction_id, requested_by, provider, model_version,
             prompt_version, rules_version, input_digest, injection_verdict, injection_patterns,
             guardrail_verdict, guardrail_reasons, recommended_action, confidence, abstained,
             citation_count, unresolved_citations, latency_ms, brief_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (invocation_id) DO NOTHING
        """,
        [
            invocation_id, brief.case_id, brief.transaction_id, requested_by, brief.provider,
            brief.model_version, brief.prompt_version, brief.rules_version, digest,
            brief.guardrail.injection_verdict, "|".join(brief.guardrail.injection_labels),
            brief.guardrail.verdict, " ; ".join(brief.guardrail.reasons),
            brief.recommended_action, float(brief.confidence), bool(brief.abstained),
            brief.citation_count(), len(brief.guardrail.unresolved_citations),
            float(brief.latency_ms), brief.to_json(), when,
        ],
    )
    return invocation_id


COPILOT_VERSIONS = {
    "prompt_version": PROMPT_VERSION,
    "guardrail_version": GUARDRAIL_VERSION,
    "rules_version": RULES_VERSION,
}
