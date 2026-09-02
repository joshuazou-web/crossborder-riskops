"""The contract between the copilot and the rest of the product.

A free-text answer cannot be audited, cannot be scored, and cannot be stopped
from claiming authority it does not have. So the copilot's output is a typed
structure with three properties the product depends on:

  * **Every claim carries citations.** A citation is a key from the case
    packet - `txn.captured_minor`, `signal.R101_DUPLICATE_IDEMPOTENCY`. A claim
    with no resolvable citation is an ungrounded claim, and that is a number in
    the evaluation report rather than an impression.
  * **The recommendation is a value from a closed set**, and `abstain` is one of
    the values. A copilot that must always answer will always answer, including
    when it should not.
  * **There is no field in which the copilot can commit an action.** It cannot
    say "released". The schema has no place to put it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime

from ..taxonomy import DECISION_ACTION

BRIEF_SCHEMA_VERSION = "1.0.0"

# What the copilot is allowed to recommend. Identical to the human action set
# plus `abstain`, which only it may use.
ALLOWED_RECOMMENDATIONS: tuple[str, ...] = tuple(DECISION_ACTION.names)


@dataclass
class Finding:
    """One claim plus the case-packet keys it is grounded in."""

    statement: str
    citations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"statement": self.statement, "citations": list(self.citations)}


@dataclass
class GuardrailReport:
    """What the guardrails did, kept beside the brief rather than in a log line."""

    verdict: str  # pass | modified | rejected
    reasons: list[str] = field(default_factory=list)
    dropped_findings: int = 0
    unresolved_citations: list[str] = field(default_factory=list)
    redactions: int = 0
    injection_verdict: str = "clean"  # clean | quarantined
    injection_labels: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class CaseBrief:
    """The copilot's whole output. Advisory, cited, and never authoritative."""

    case_id: str
    transaction_id: str
    summary: str
    key_facts: list[Finding] = field(default_factory=list)
    signal_explanations: list[Finding] = field(default_factory=list)
    conflicts: list[Finding] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    suggested_questions: list[str] = field(default_factory=list)
    recommended_action: str = "abstain"
    confidence: float = 0.0
    rationale: str = ""
    abstained: bool = True
    # Provenance. A brief without these cannot be reproduced or challenged.
    provider: str = "mock"
    model_version: str = ""
    prompt_version: str = ""
    rules_version: str = ""
    generated_at: str = ""
    latency_ms: float = 0.0
    guardrail: GuardrailReport = field(default_factory=lambda: GuardrailReport("pass"))

    # The copilot never commits an action. This is a constant, not a field the
    # model can set, and the UI reads it to label the recommendation.
    authority: str = "advisory_only"

    def all_findings(self) -> list[Finding]:
        return [*self.key_facts, *self.signal_explanations, *self.conflicts]

    def citation_count(self) -> int:
        return sum(len(f.citations) for f in self.all_findings())

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": BRIEF_SCHEMA_VERSION,
            "case_id": self.case_id,
            "transaction_id": self.transaction_id,
            "summary": self.summary,
            "key_facts": [f.as_dict() for f in self.key_facts],
            "signal_explanations": [f.as_dict() for f in self.signal_explanations],
            "conflicts": [f.as_dict() for f in self.conflicts],
            "missing_information": list(self.missing_information),
            "suggested_questions": list(self.suggested_questions),
            "recommended_action": self.recommended_action,
            "confidence": round(float(self.confidence), 4),
            "rationale": self.rationale,
            "abstained": self.abstained,
            "authority": self.authority,
            "provider": self.provider,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "rules_version": self.rules_version,
            "generated_at": self.generated_at,
            "latency_ms": round(float(self.latency_ms), 2),
            "guardrail": self.guardrail.as_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, payload: dict) -> CaseBrief:
        guard = payload.get("guardrail") or {}
        return cls(
            case_id=str(payload.get("case_id", "")),
            transaction_id=str(payload.get("transaction_id", "")),
            summary=str(payload.get("summary", "")),
            key_facts=[_finding(item) for item in payload.get("key_facts", [])],
            signal_explanations=[_finding(item) for item in payload.get("signal_explanations", [])],
            conflicts=[_finding(item) for item in payload.get("conflicts", [])],
            missing_information=[str(x) for x in payload.get("missing_information", [])],
            suggested_questions=[str(x) for x in payload.get("suggested_questions", [])],
            recommended_action=str(payload.get("recommended_action", "abstain")),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            rationale=str(payload.get("rationale", "")),
            abstained=bool(payload.get("abstained", False)),
            provider=str(payload.get("provider", "")),
            model_version=str(payload.get("model_version", "")),
            prompt_version=str(payload.get("prompt_version", "")),
            rules_version=str(payload.get("rules_version", "")),
            generated_at=str(payload.get("generated_at", "")),
            latency_ms=float(payload.get("latency_ms", 0.0) or 0.0),
            guardrail=GuardrailReport(
                verdict=str(guard.get("verdict", "pass")),
                reasons=[str(x) for x in guard.get("reasons", [])],
                dropped_findings=int(guard.get("dropped_findings", 0) or 0),
                unresolved_citations=[str(x) for x in guard.get("unresolved_citations", [])],
                redactions=int(guard.get("redactions", 0) or 0),
                injection_verdict=str(guard.get("injection_verdict", "clean")),
                injection_labels=[str(x) for x in guard.get("injection_labels", [])],
            ),
        )


def _finding(payload: object) -> Finding:
    if isinstance(payload, dict):
        return Finding(
            statement=str(payload.get("statement", "")),
            citations=[str(c) for c in payload.get("citations", [])],
        )
    return Finding(statement=str(payload), citations=[])


def abstention(
    case_id: str,
    transaction_id: str,
    reason: str,
    *,
    provider: str = "",
    model_version: str = "",
    prompt_version: str = "",
    guardrail: GuardrailReport | None = None,
) -> CaseBrief:
    """The brief returned when the copilot must not answer.

    An abstention is a first-class result, not an error. It still carries
    provenance, so "the copilot declined, here is why" is auditable.
    """
    return CaseBrief(
        case_id=case_id,
        transaction_id=transaction_id,
        summary=reason,
        recommended_action="abstain",
        confidence=0.0,
        rationale=reason,
        abstained=True,
        provider=provider,
        model_version=model_version,
        prompt_version=prompt_version,
        generated_at=datetime.now().replace(microsecond=0).isoformat(),
        guardrail=guardrail or GuardrailReport("pass", [reason]),
    )
