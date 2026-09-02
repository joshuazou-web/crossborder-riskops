"""The controlled vocabulary of CrossBorder RiskOps - the single source of truth.

Everything downstream reads these structures: the synthetic generator, the state
machine, the rule engine, the decision policy, the review workflow, the AI
guardrails, the dashboard filters and the generated data dictionary. A value
that is not defined here cannot be written to the warehouse.

Design rules applied to every dimension, inherited from the predecessor project:
  * values are mutually exclusive - one row gets exactly one value;
  * every dimension has an explicit escape hatch, so the pipeline never guesses;
  * every value carries a definition, a worked example and the boundary case
    that most often causes a mis-label;
  * aliases are declared, because upstream systems emit `CAPTURED`, `capture`
    and `Capture` for the same thing and three spellings are three states in
    any report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TAXONOMY_VERSION = "1.0.0"
TAXONOMY_EFFECTIVE_DATE = "2026-08-15"


def _norm_key(raw: object) -> str:
    return " ".join(str(raw).strip().lower().replace("_", " ").replace("-", " ").split())


@dataclass(frozen=True)
class TaxonomyValue:
    name: str
    definition: str
    example: str
    boundary: str
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    purpose: str
    fallback: str
    values: list[TaxonomyValue]

    @property
    def names(self) -> list[str]:
        return [v.name for v in self.values]

    def alias_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for value in self.values:
            mapping[_norm_key(value.name)] = value.name
            for alias in value.aliases:
                mapping[_norm_key(alias)] = value.name
        return mapping

    def normalise(self, raw: object) -> str:
        """Canonical name for `raw`, or the dimension's fallback."""
        return self.alias_map().get(_norm_key(raw), self.fallback)

    def get(self, name: str) -> TaxonomyValue | None:
        for value in self.values:
            if value.name == name:
                return value
        return None


# ---------------------------------------------------------------------------
# Dimension 1 - payment_state: where a transaction sits in its lifecycle
# ---------------------------------------------------------------------------
PAYMENT_STATE = Dimension(
    key="payment_state",
    label="Payment state",
    purpose="The lifecycle position of one cross-border transaction.",
    fallback="unknown",
    values=[
        TaxonomyValue(
            "initiated",
            "The payer's wallet has created the payment intent; no funds are reserved yet.",
            "A traveller taps a QR code at a merchant till and the wallet opens the confirm screen.",
            "An intent the payer abandoned stays `initiated` forever; it never becomes `failed`.",
            ["init", "created", "pending_init", "intent_created"],
        ),
        TaxonomyValue(
            "authorized",
            "The issuer has reserved the funds. Money has not moved.",
            "The issuing wallet returns an approval code and holds USD 62.40.",
            "Authorised is not captured. An expired authorisation releases the hold and never settles.",
            ["auth", "authorised", "approved", "hold_placed"],
        ),
        TaxonomyValue(
            "captured",
            "The merchant has claimed the authorised funds. The payer is now debited.",
            "The merchant batches the day's authorisations and captures them at close.",
            "A capture larger than its authorisation is an over-capture, not a new transaction.",
            ["capture", "debited", "claimed"],
        ),
        TaxonomyValue(
            "settled",
            "Funds have been paid to the merchant's acquirer, net of fees, in the settlement currency.",
            "T+2 settlement pays the merchant SGD 84.10 against a USD 62.40 capture.",
            "Settled is the first state where the FX rate is final; before it, the rate is a quote.",
            ["settle", "paid_out", "funded"],
        ),
        TaxonomyValue(
            "refunded",
            "The merchant has returned funds to the payer, in part or in full.",
            "The traveller returns the goods and the merchant refunds USD 62.40.",
            "A refund is merchant-initiated. A payer-initiated reversal is a chargeback.",
            ["refund", "returned", "credited_back"],
        ),
        TaxonomyValue(
            "chargeback",
            "The payer's issuer has forcibly reversed the payment through the scheme.",
            "The payer files 'goods not received'; the issuer debits the merchant.",
            "A chargeback can follow a refund - that is double credit, and a real ops problem.",
            ["dispute", "reversal", "cb", "charge_back"],
        ),
        TaxonomyValue(
            "failed",
            "The transaction ended without moving money: declined, expired or cancelled.",
            "The issuer declines for insufficient funds.",
            "A failed transaction still carries risk signal - repeated failures are enumeration.",
            ["declined", "rejected", "cancelled", "canceled", "expired"],
        ),
        TaxonomyValue(
            "under_review",
            "The transaction is held by the risk policy pending a human decision.",
            "A first-time wallet sends a high-value payment to a newly onboarded merchant.",
            "`under_review` is a *hold on the flow*, not an outcome. It always resolves to another state.",
            ["review", "on_hold", "manual_review", "held"],
        ),
    ],
)

# ---------------------------------------------------------------------------
# Dimension 2 - signal_family: what kind of thing a risk rule noticed
# ---------------------------------------------------------------------------
SIGNAL_FAMILY = Dimension(
    key="signal_family",
    label="Signal family",
    purpose="Groups risk rules so a case can be explained in a sentence, not a rule list.",
    fallback="other",
    values=[
        TaxonomyValue(
            "integrity",
            "The transaction record contradicts itself or the ledger.",
            "The captured amount exceeds the authorised amount.",
            "Integrity is about the record, not the payer. A wrong amount is integrity, not fraud.",
            ["data_integrity", "ledger"],
        ),
        TaxonomyValue(
            "duplication",
            "The same economic payment appears more than once.",
            "Two captures share an idempotency key ten seconds apart.",
            "Two payments to the same merchant on the same day are not duplicates unless the key matches.",
            ["duplicate", "idempotency"],
        ),
        TaxonomyValue(
            "velocity",
            "The rate of activity for an entity is abnormal against its own history.",
            "A wallet makes eleven payments in four minutes after months of two per week.",
            "Velocity is per-entity and relative. A busy merchant is not a velocity signal.",
            ["frequency", "rate"],
        ),
        TaxonomyValue(
            "geo_device",
            "Location, device and wallet jurisdiction disagree.",
            "A Malaysian wallet pays from a device that was in Brazil eight minutes ago.",
            "A traveller legitimately crosses borders. The signal is impossible travel, not travel.",
            ["geo", "device", "location"],
        ),
        TaxonomyValue(
            "fx_fee",
            "The FX rate, fee split or settlement amount does not reconcile.",
            "Settlement is 3.1% away from the captured amount converted at the booked quote.",
            "A stale-but-valid quote is a warning; an amount outside quote tolerance is a signal.",
            ["fx", "pricing", "fee"],
        ),
        TaxonomyValue(
            "merchant_profile",
            "Behaviour does not match the merchant's declared category or history.",
            "A grocery MCC merchant suddenly takes single payments of USD 4,000.",
            "A merchant growing fast is not a signal; a merchant changing *shape* is.",
            ["mcc", "merchant"],
        ),
        TaxonomyValue(
            "dispute_abuse",
            "The refund or chargeback pattern looks abusive on either side.",
            "The same payer charges back a fourth payment after being refunded three times.",
            "One chargeback is noise. A rate against the payer's own volume is the signal.",
            ["refund_abuse", "chargeback"],
        ),
        TaxonomyValue(
            "network_linkage",
            "Entities that should be unrelated share an identifier.",
            "Six 'unrelated' wallets settle to the same beneficiary account.",
            "Family sharing a device is common. Sharing a payout account is not.",
            ["linkage", "ring", "graph"],
        ),
        TaxonomyValue(
            "completeness",
            "A field needed to decide the case is missing or contradictory.",
            "The transaction has no device fingerprint and no merchant category.",
            "Completeness is not a risk claim. It is the reason a decision must wait.",
            ["missing_data", "insufficient_information"],
        ),
    ],
)

# ---------------------------------------------------------------------------
# Dimension 3 - case_state: the workflow position of a risk case
# ---------------------------------------------------------------------------
CASE_STATE = Dimension(
    key="case_state",
    label="Case state",
    purpose="Where a risk case sits in the human workflow.",
    fallback="open",
    values=[
        TaxonomyValue(
            "open", "Created by the policy, not yet picked up.",
            "A velocity case lands in the queue at 02:14.",
            "Open is unassigned. A case someone is holding is `in_review`.", ["new", "queued"],
        ),
        TaxonomyValue(
            "in_review", "An analyst owns it and is working it.",
            "The analyst opens the case and requests the AI brief.",
            "Requesting the AI brief does not change the state; taking ownership does.", ["assigned", "wip"],
        ),
        TaxonomyValue(
            "awaiting_information", "Blocked on evidence from the merchant or payer.",
            "The case needs a shipping record before it can be decided.",
            "Blocked on a *person* is awaiting_information; blocked on a *system* is still in_review.",
            ["pending_info", "info_requested"],
        ),
        TaxonomyValue(
            "escalated", "Raised to a senior reviewer or another team.",
            "A suspected payout-account ring goes to the investigations team.",
            "Escalated is still open. It is not a decision.", ["raised"],
        ),
        TaxonomyValue(
            "resolved_released", "Decided: the payment proceeds.",
            "Evidence shows the traveller was genuinely abroad; the payment is released.",
            "Released after a hold still counts as a hold for latency metrics.", ["released", "approved"],
        ),
        TaxonomyValue(
            "resolved_held", "Decided: the payment is stopped and does not proceed.",
            "Impossible travel plus a first-use payout account; the payment is held.",
            "Held is a decision. `under_review` on the transaction is not.", ["blocked", "denied", "rejected"],
        ),
        TaxonomyValue(
            "appealed", "The payer or merchant contests a resolved case.",
            "The traveller submits a boarding pass after being held.",
            "An appeal reopens the case; it does not erase the original decision.", ["disputed", "reopened"],
        ),
        TaxonomyValue(
            "closed_false_positive", "Reopened, re-decided, and confirmed to have been a wrong hold.",
            "The boarding pass resolves the impossible-travel signal; the hold was wrong.",
            "This is the only state that counts toward false-positive recovery.",
            ["fp", "overturned"],
        ),
    ],
)

# ---------------------------------------------------------------------------
# Dimension 4 - decision_action: what a human or the policy actually did
# ---------------------------------------------------------------------------
DECISION_ACTION = Dimension(
    key="decision_action",
    label="Decision action",
    purpose="The finite set of outcomes a case can be given. The AI may recommend from this list; only a human or the deterministic policy may commit one.",
    fallback="request_information",
    values=[
        TaxonomyValue(
            "release", "Let the payment continue through its normal lifecycle.",
            "Signals are explained by a documented travel pattern.",
            "Releasing does not clear the signals; it records that they were judged acceptable.",
            ["allow", "approve", "pass"],
        ),
        TaxonomyValue(
            "hold", "Stop the payment. Funds do not move.",
            "Two independent high-severity signals with no mitigating evidence.",
            "Hold is reversible on appeal; that is what makes recovery measurable.",
            ["block", "deny", "stop", "reject"],
        ),
        TaxonomyValue(
            "request_information", "Pause and ask a person for the missing evidence.",
            "No device fingerprint and no merchant category - nothing can be judged yet.",
            "This is the correct answer to an incomplete case. It is not indecision.",
            ["ask", "rfi", "pend"],
        ),
        TaxonomyValue(
            "escalate", "Hand to a senior reviewer or an investigations team.",
            "The case implicates six other wallets through one payout account.",
            "Escalation is for scope beyond one case, not for difficulty.",
            ["raise", "refer"],
        ),
        TaxonomyValue(
            "abstain", "State that there is not enough basis to recommend anything.",
            "The AI copilot cannot ground a recommendation in the evidence available.",
            "Only the AI may abstain. A human must choose one of the other four.",
            ["decline", "no_recommendation", "unknown"],
        ),
    ],
)

# ---------------------------------------------------------------------------
# Dimension 5 - reason_code: why the decision was made
# ---------------------------------------------------------------------------
REASON_CODE = Dimension(
    key="reason_code",
    label="Reason code",
    purpose="A structured why, so decisions can be aggregated, audited and argued with.",
    fallback="RC_OTHER",
    values=[
        TaxonomyValue(
            "RC_EVIDENCE_SUPPORTS_LEGITIMATE", "Evidence explains the signals as legitimate behaviour.",
            "Travel itinerary matches the geography that triggered the signal.",
            "Use when evidence *explains*; use RC_LOW_RESIDUAL_RISK when nothing explains but nothing worries.",
            [],
        ),
        TaxonomyValue(
            "RC_LOW_RESIDUAL_RISK", "Signals fired but severity and history do not justify a hold.",
            "A single medium velocity signal on a two-year-old wallet.",
            "Not an explanation - a judgement about proportion.", [],
        ),
        TaxonomyValue(
            "RC_CONFIRMED_DUPLICATE", "The payment is a genuine duplicate of another.",
            "Same idempotency key, same amount, eleven seconds apart.",
            "Duplicate is an integrity outcome, not a fraud outcome.", [],
        ),
        TaxonomyValue(
            "RC_SUSPECTED_ACCOUNT_TAKEOVER", "Device, geography and behaviour indicate the payer is not the account holder.",
            "New device, impossible travel, and a payout account first used today.",
            "Distinguish from RC_SUSPECTED_MERCHANT_ABUSE: this is about the payer's side.", [],
        ),
        TaxonomyValue(
            "RC_SUSPECTED_MERCHANT_ABUSE", "The merchant's behaviour, not the payer's, is the problem.",
            "A grocery MCC merchant processing 40x its usual ticket size overnight.",
            "Merchant abuse can look like payer fraud in the raw signals; the profile separates them.", [],
        ),
        TaxonomyValue(
            "RC_FX_OR_FEE_MISMATCH", "Settlement does not reconcile with the booked quote or fee schedule.",
            "Settlement differs from the quoted conversion by more than tolerance.",
            "An operations outcome. It rarely means fraud.", [],
        ),
        TaxonomyValue(
            "RC_INSUFFICIENT_EVIDENCE", "The case cannot be decided with what is on file.",
            "No device fingerprint, no merchant category, no prior history.",
            "The honest answer to an incomplete case, and the only reason valid with request_information.",
            [],
        ),
        TaxonomyValue(
            "RC_LINKED_ENTITY_RISK", "Risk comes from a linked entity rather than this transaction.",
            "Six wallets share one payout account, two already held.",
            "Requires an actual shared identifier, not a statistical resemblance.", [],
        ),
        TaxonomyValue(
            "RC_APPEAL_EVIDENCE_ACCEPTED", "New evidence from an appeal overturns the original decision.",
            "A boarding pass resolves the impossible-travel signal.",
            "The only reason code that may close a case as a false positive.", [],
        ),
        TaxonomyValue(
            "RC_APPEAL_EVIDENCE_REJECTED", "Appeal evidence does not resolve the signals.",
            "The submitted receipt post-dates the disputed payment.",
            "The original decision stands and the case closes as held.", [],
        ),
        TaxonomyValue(
            "RC_POLICY_AUTO", "Committed by the deterministic policy with no human involvement.",
            "A clean transaction with no signals is auto-released.",
            "Only the policy may use this code; a human decision always names a substantive reason.", [],
        ),
        TaxonomyValue(
            "RC_OTHER", "None of the above; the free-text note carries the reasoning.",
            "A genuinely novel pattern with no existing code.",
            "Frequent use of RC_OTHER is a signal that the code list needs a new value.", [],
        ),
    ],
)

# ---------------------------------------------------------------------------
# Dimension 6 - actor_role: who may do what
# ---------------------------------------------------------------------------
ACTOR_ROLE = Dimension(
    key="actor_role",
    label="Actor role",
    purpose="Who is acting, and therefore what they are permitted to commit.",
    fallback="system",
    values=[
        TaxonomyValue(
            "risk_analyst", "Works the case queue and decides fraud outcomes.",
            "Decides release or hold with a reason code.",
            "May not close an appeal - that is a separate pair of eyes.", ["analyst"],
        ),
        TaxonomyValue(
            "payment_ops", "Owns money correctness: reconciliation, duplicates, FX and fees.",
            "Resolves a settlement break and requests information from the acquirer.",
            "May not decide a high-severity fraud case.", ["ops", "operations"],
        ),
        TaxonomyValue(
            "customer_support", "Handles payer and merchant appeals against a redacted case view.",
            "Files an appeal with a boarding pass attached.",
            "Never sees raw PII and never decides a case.", ["support", "cs", "agent"],
        ),
        TaxonomyValue(
            "admin_auditor", "Reads everything, verifies the audit chain, exports evidence.",
            "Verifies the hash chain before an internal review.",
            "Read-only over decisions by design - an auditor who can edit is not an auditor.",
            ["auditor", "admin"],
        ),
        TaxonomyValue(
            "system", "The deterministic policy engine acting without a human.",
            "Auto-releases a transaction with no signals.",
            "The AI copilot is NOT this role - it is `ai_copilot` and commits nothing.", ["policy", "engine"],
        ),
        TaxonomyValue(
            "ai_copilot", "The advisory model layer. Produces briefs and recommendations only.",
            "Writes a case brief with citations and a suggested action.",
            "Holds no authority: it may never appear as the actor on a committed decision.",
            ["copilot", "llm", "assistant"],
        ),
    ],
)

DIMENSIONS: tuple[Dimension, ...] = (
    PAYMENT_STATE,
    SIGNAL_FAMILY,
    CASE_STATE,
    DECISION_ACTION,
    REASON_CODE,
    ACTOR_ROLE,
)

DIMENSION_BY_KEY = {d.key: d for d in DIMENSIONS}

# Roles that may commit a decision. The AI copilot is deliberately absent, and
# `review.workflow` enforces this rather than trusting a caller.
DECIDING_ROLES = ("risk_analyst", "payment_ops", "admin_auditor", "system")

# Actions a human must choose from. `abstain` is reserved for the AI.
HUMAN_ACTIONS = ("release", "hold", "request_information", "escalate")

# Reason codes only the deterministic policy may use.
SYSTEM_ONLY_REASON_CODES = ("RC_POLICY_AUTO",)

# Case states that mean the case is finished.
TERMINAL_CASE_STATES = ("resolved_released", "resolved_held", "closed_false_positive")

RISK_BANDS = ("low", "medium", "high", "critical")

SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def total_value_count() -> int:
    return sum(len(d.values) for d in DIMENSIONS)
