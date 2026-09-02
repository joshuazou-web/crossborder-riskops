"""Guardrails around the copilot.

Two gates, in this order, for a reason borrowed from ordinary security practice:
a deterministic pattern layer runs **first** and short-circuits, so an obvious
attack never costs a model call, and only what survives it reaches anything
expensive or probabilistic.

**Input gate.** Merchant notes, appeal text and device metadata are
attacker-controlled. Text that addresses a model rather than a person is
quarantined - removed from the packet and replaced with a marker - before any
provider sees it. The case still gets a brief; it is simply a brief written
without the attacker's paragraph in it.

**Output gate.** Whatever comes back is treated as untrusted too:

  * citations must resolve to keys the packet actually contains;
  * a finding with no resolvable citation is dropped and counted;
  * the recommendation must be one of the closed set of actions;
  * language claiming the model *performed* an action is rejected outright -
    "I have released this payment" is not a wording problem, it is a claim of
    authority the copilot does not have;
  * PII-shaped strings are redacted, so a model cannot be talked into echoing
    an account number back out;
  * below the confidence floor, the recommendation is downgraded to `abstain`.

Every one of these produces a counter, and those counters are the agent-safety
section of the evaluation report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Settings
from ..risk.rules import match_authority_patterns, match_injection_patterns
from .schema import ALLOWED_RECOMMENDATIONS, CaseBrief, Finding, GuardrailReport

GUARDRAIL_VERSION = "1.0.0"

QUARANTINE_MARKER = (
    "[quarantined: merchant free text contained instructions aimed at an automated reader "
    "and was withheld from the model]"
)

# Phrases in which the copilot claims to have *done* something. Distinct from
# recommending: "recommend holding" is its job, "I have placed a hold" is not.
AUTHORITY_CLAIM_PATTERNS: tuple[str, ...] = (
    r"\bi (have |'ve )?(released|blocked|held|refunded|approved|frozen|cancelled|canceled)\b",
    r"\b(the (payment|transaction|case) (has been|was)) (released|blocked|held|refunded|approved|closed)\b",
    r"\bi (am |'m )?(releasing|blocking|holding|refunding|approving|freezing)\b",
    r"\b(i|we) (hereby )?(authorise|authorize|approve|clear)\b",
    r"\bcase (is )?(now )?closed\b",
    r"\bno human review (is )?(needed|required)\b",
    r"\bdecision:\s*(final|committed)\b",
)

# PII shapes. Deliberately conservative - a false redaction costs a reviewer one
# click, a missed one leaks a synthetic identifier that would be real elsewhere.
PII_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:\d[ -]?){13,19}\b", "[redacted:card-like-number]"),
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[redacted:email]"),
    (r"\b(?:\+?\d{1,3}[ -]?)?\(?\d{3}\)?[ -]?\d{3,4}[ -]?\d{4}\b", "[redacted:phone-like]"),
    (r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", "[redacted:iban-like]"),
)

_AUTHORITY_RE = tuple(re.compile(p, re.IGNORECASE) for p in AUTHORITY_CLAIM_PATTERNS)
_PII_RE = tuple((re.compile(p), replacement) for p, replacement in PII_PATTERNS)


@dataclass
class InputGateResult:
    verdict: str  # clean | quarantined
    labels: list[str] = field(default_factory=list)
    sanitised_text: str = ""
    original_length: int = 0

    @property
    def quarantined(self) -> bool:
        return self.verdict == "quarantined"


def screen_untrusted_text(text: str) -> InputGateResult:
    """The input gate. Runs before any provider call."""
    raw = text or ""
    labels = sorted(set(match_injection_patterns(raw)) | set(match_authority_patterns(raw)))
    if not labels:
        return InputGateResult("clean", [], raw, len(raw))
    return InputGateResult("quarantined", labels, QUARANTINE_MARKER, len(raw))


def redact_pii(text: str) -> tuple[str, int]:
    """Replace PII-shaped substrings. Returns the text and how many were hit."""
    redactions = 0
    result = text or ""
    for pattern, replacement in _PII_RE:
        result, count = pattern.subn(replacement, result)
        redactions += count
    return result, redactions


def find_authority_claims(text: str) -> list[str]:
    """Phrases in which the copilot claims to have acted."""
    if not text:
        return []
    return sorted({
        pattern.pattern for pattern in _AUTHORITY_RE if pattern.search(text)
    })


def apply_output_guardrails(
    brief: CaseBrief,
    allowed_citations: set[str],
    settings: Settings,
    input_gate: InputGateResult | None = None,
) -> CaseBrief:
    """Validate, repair or reject a brief. Never raises - it downgrades."""
    reasons: list[str] = []
    unresolved: list[str] = []
    dropped = 0
    redactions = 0

    # 1. Authority claims are fatal. There is no safe repair for a model that
    #    believes it acted, so the whole brief is replaced by an abstention.
    prose = " ".join([
        brief.summary,
        brief.rationale,
        *[f.statement for f in brief.all_findings()],
    ])
    claims = find_authority_claims(prose)
    if claims:
        report = GuardrailReport(
            verdict="rejected",
            reasons=[
                "the brief claimed to have performed an action; the copilot has no authority "
                "to act and its output was discarded"
            ],
            injection_verdict=(input_gate.verdict if input_gate else "clean"),
            injection_labels=(input_gate.labels if input_gate else []),
        )
        return CaseBrief(
            case_id=brief.case_id,
            transaction_id=brief.transaction_id,
            summary="Brief rejected by the output guardrail.",
            recommended_action="abstain",
            confidence=0.0,
            rationale=(
                "The generated brief asserted that an action had been taken. Only a person or "
                "the deterministic policy may act, so the brief was discarded rather than "
                "shown to a reviewer."
            ),
            abstained=True,
            provider=brief.provider,
            model_version=brief.model_version,
            prompt_version=brief.prompt_version,
            rules_version=brief.rules_version,
            generated_at=brief.generated_at,
            latency_ms=brief.latency_ms,
            guardrail=report,
        )

    # 2. Citations must resolve. A finding that cites nothing the packet
    #    contains is dropped rather than shown with a caveat.
    def keep(findings: list[Finding]) -> list[Finding]:
        nonlocal dropped
        survivors = []
        for finding in findings:
            resolved = [c for c in finding.citations if c in allowed_citations]
            missing = [c for c in finding.citations if c not in allowed_citations]
            unresolved.extend(missing)
            if not resolved:
                dropped += 1
                continue
            survivors.append(Finding(statement=finding.statement, citations=resolved))
        return survivors

    brief.key_facts = keep(brief.key_facts)
    brief.signal_explanations = keep(brief.signal_explanations)
    brief.conflicts = keep(brief.conflicts)

    if dropped:
        surviving = len(brief.all_findings())
        reasons.append(
            f"{dropped} finding(s) cited nothing in the case packet and were removed as ungrounded"
        )
        # A brief that invented part of its evidence is not trustworthy on the
        # rest at full strength. Confidence is scaled by the share of findings
        # that survived; where the recommendation was leaning on what was
        # removed, that pushes it under the floor and it becomes an abstention.
        if surviving + dropped:
            brief.confidence = round(
                float(brief.confidence) * surviving / (surviving + dropped), 4
            )
            reasons.append(
                f"confidence scaled to {brief.confidence:.2f} because only {surviving} of "
                f"{surviving + dropped} findings were grounded"
            )
    if unresolved:
        reasons.append(
            f"{len(unresolved)} citation(s) did not resolve: {sorted(set(unresolved))[:5]}"
        )

    # 3. Redact PII from everything that will be displayed.
    brief.summary, hit = redact_pii(brief.summary)
    redactions += hit
    brief.rationale, hit = redact_pii(brief.rationale)
    redactions += hit
    for finding in brief.all_findings():
        finding.statement, hit = redact_pii(finding.statement)
        redactions += hit
    brief.missing_information = [redact_pii(item)[0] for item in brief.missing_information]
    brief.suggested_questions = [redact_pii(item)[0] for item in brief.suggested_questions]
    if redactions:
        reasons.append(f"{redactions} PII-shaped string(s) redacted before display")

    # 4. The recommendation must be a value from the closed set.
    if brief.recommended_action not in ALLOWED_RECOMMENDATIONS:
        reasons.append(
            f"recommended action {brief.recommended_action!r} is not one of "
            f"{ALLOWED_RECOMMENDATIONS}; downgraded to abstain"
        )
        brief.recommended_action = "abstain"
        brief.confidence = 0.0

    # 5. Confidence floor. A recommendation the model is not confident in is
    #    worse than no recommendation, because a reviewer will read it anyway.
    if (
        brief.recommended_action != "abstain"
        and brief.confidence < settings.ai_min_confidence
    ):
        reasons.append(
            f"confidence {brief.confidence:.2f} is below the {settings.ai_min_confidence:.2f} "
            "floor; recommendation downgraded to abstain"
        )
        brief.recommended_action = "abstain"

    # 6. A brief with nothing grounded left cannot recommend anything.
    if not brief.all_findings() and brief.recommended_action != "abstain":
        reasons.append("no grounded findings survived; recommendation downgraded to abstain")
        brief.recommended_action = "abstain"
        brief.confidence = 0.0

    brief.abstained = brief.recommended_action == "abstain"
    brief.guardrail = GuardrailReport(
        verdict="modified" if reasons else "pass",
        reasons=reasons,
        dropped_findings=dropped,
        unresolved_citations=sorted(set(unresolved)),
        redactions=redactions,
        injection_verdict=(input_gate.verdict if input_gate else "clean"),
        injection_labels=(input_gate.labels if input_gate else []),
    )
    return brief
