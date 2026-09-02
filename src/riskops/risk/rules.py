"""Deterministic rule engine.

Every risk signal in this product starts here, in ordinary code that a risk
analyst could read out loud in a policy review. No model, and certainly no
language model, participates in *detection*.

Three properties each rule must have:

  * **Reproducible** - same input, same signal, forever. No sampling, no
    thresholds hidden in a fitted object.
  * **Grounded** - every signal names the exact fields it was computed from, in
    `evidence_fields`. This is what makes "the copilot made an ungrounded claim"
    a computable metric later, rather than a matter of taste.
  * **Explainable in one sentence** - `title` is what appears in the queue, and
    `detail` carries the numbers that justify it.

Thresholds live in `Settings`, so tuning them is a config change with a visible
effect on the evaluation report, not an edit buried in a function.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from ..config import Settings
from ..generator.synth import PER_USD, haversine_km
from ..money import Money
from ..taxonomy import SEVERITY_ORDER

RULES_VERSION = "1.0.0"

SEVERITY_WEIGHT: dict[str, float] = {
    "low": 0.10,
    "medium": 0.25,
    "high": 0.45,
    "critical": 0.70,
}

# The fastest commercial aircraft cruise is ~950 km/h. Anything faster than this
# between two payments is not travel; it is two people, or a proxy.
MAX_PLAUSIBLE_SPEED_KMH = 950.0


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    family: str
    severity: str
    title: str
    rationale: str
    evidence_fields: tuple[str, ...]

    @property
    def weight(self) -> float:
        return SEVERITY_WEIGHT[self.severity]


RULES: dict[str, RuleSpec] = {
    spec.rule_id: spec
    for spec in (
        RuleSpec(
            "R101_DUPLICATE_IDEMPOTENCY", "duplication", "high",
            "Duplicate payment under one idempotency key",
            "Two payments share an idempotency key, a wallet, a merchant and an amount inside "
            "the duplicate window. A retry created a second real debit.",
            ("idempotency_key", "captured_minor", "wallet_id", "merchant_id", "created_at"),
        ),
        RuleSpec(
            "R102_AUTH_CAPTURE_GAP", "integrity", "medium",
            "Captured amount does not match the authorisation",
            "The gap between authorisation and capture is beyond any ordinary tip or "
            "adjustment, so the two records disagree about how much the payer owes.",
            ("authorized_minor", "captured_minor", "presentment_currency"),
        ),
        RuleSpec(
            "R103_QUARANTINED_EVENTS", "integrity", "low",
            "The feed sent events this transaction could not accept",
            "One or more events were illegal for the state the transaction was in and were "
            "quarantined. The ledger is intact, but the upstream feed is not.",
            ("rejected_event_count",),
        ),
        RuleSpec(
            "R201_WALLET_VELOCITY", "velocity", "high",
            "Payment velocity far above this wallet's own baseline",
            "The wallet made more payments inside the velocity window than the policy allows, "
            "measured against its own history rather than a global average.",
            ("wallet_id", "created_at", "baseline_weekly_count"),
        ),
        RuleSpec(
            "R202_AMOUNT_ANOMALY", "velocity", "high",
            "Amount far above this wallet's usual ticket",
            "The payment is many times the wallet's typical value. Large is not suspicious; "
            "large *for this payer* is.",
            ("captured_minor", "presentment_currency", "wallet_id"),
        ),
        RuleSpec(
            "R301_GEO_MISMATCH", "geo_device", "low",
            "IP country differs from the wallet's registered country",
            "The payment came from outside the wallet's home jurisdiction. Most people who do "
            "this are on holiday. Severity is deliberately low: routing every foreign-IP payment "
            "to a human would bury the queue in tourists and teach analysts to click through. It "
            "carries weight only in combination with something else.",
            ("ip_country", "wallet_country"),
        ),
        RuleSpec(
            "R302_DEVICE_HOPPING", "geo_device", "high",
            "One wallet paying from several devices in one hour",
            "Three or more distinct device fingerprints inside an hour is a pattern of "
            "credential sharing or automated use, not of one person paying.",
            ("device_id", "wallet_id", "created_at"),
        ),
        RuleSpec(
            "R303_IMPOSSIBLE_TRAVEL", "geo_device", "critical",
            "Impossible travel between consecutive payments",
            "The distance between two consecutive payments requires a speed no aircraft "
            "reaches. The two payments cannot both have been made by a person in transit.",
            ("ip_country", "created_at", "wallet_id"),
        ),
        RuleSpec(
            "R304_JURISDICTION_CONFLICT", "geo_device", "high",
            "Wallet, payer and IP jurisdictions all disagree on a low-KYC wallet",
            "Three different countries on a wallet that has only basic verification. Each "
            "alone is ordinary; together on a thin identity they are not.",
            ("wallet_country", "payer_country", "ip_country", "kyc_level"),
        ),
        RuleSpec(
            "R401_FX_OUT_OF_TOLERANCE", "fx_fee", "high",
            "Settlement is outside FX tolerance of the booked quote",
            "The merchant was paid an amount that does not follow from the captured amount at "
            "the quote on file. Someone is short, and it is either the merchant or the book.",
            ("settled_minor", "captured_minor", "fee_minor", "quoted_fx_rate",
             "settlement_currency"),
        ),
        RuleSpec(
            "R402_FEE_OFF_SCHEDULE", "fx_fee", "medium",
            "Fee does not match the merchant's schedule",
            "The fee charged is not what this merchant's tier and corridor produce.",
            ("fee_minor", "captured_minor", "risk_tier", "is_cross_border"),
        ),
        RuleSpec(
            "R403_STALE_FX_QUOTE", "fx_fee", "medium",
            "Capture executed against an expired FX quote",
            "The quote had expired before the capture. The rate applied was not a rate the "
            "payer was ever shown.",
            ("fx_quote_id", "fx_quoted_at", "created_at"),
        ),
        RuleSpec(
            "R501_MCC_TICKET_ANOMALY", "merchant_profile", "high",
            "Ticket size far outside the merchant's declared category",
            "A merchant whose category implies small tickets is taking a very large one. "
            "Either the category is wrong or the business has changed shape.",
            ("captured_minor", "mcc", "baseline_ticket_minor", "merchant_id"),
        ),
        RuleSpec(
            "R601_REFUND_CHAIN", "dispute_abuse", "medium",
            "Repeated refunds between the same payer and merchant",
            "Several refunds between one pair inside a short window. A refund is ordinary; a "
            "chain of them between the same two parties is a known laundering shape.",
            ("refunded_minor", "wallet_id", "merchant_id", "created_at"),
        ),
        RuleSpec(
            "R602_DOUBLE_CREDIT", "dispute_abuse", "critical",
            "Refunded and charged back - the payer was credited twice",
            "The payment was refunded by the merchant and then reversed by the issuer. The "
            "merchant has paid for this transaction twice.",
            ("refunded_minor", "charged_back_minor", "captured_minor"),
        ),
        RuleSpec(
            "R701_SHARED_PAYOUT_ACCOUNT", "network_linkage", "medium",
            "Merchant shares a payout account with other merchants",
            "Several supposedly unrelated merchants settle to one beneficiary account. Shared "
            "devices are common; shared payout accounts are not. Severity is medium rather than "
            "high because this is a fact about the *merchant*, true of every payment it ever "
            "takes: a franchise group and a fraud ring look identical here, and only the rest of "
            "the case separates them.",
            ("payout_account_id", "merchant_id"),
        ),
        RuleSpec(
            "R702_SHARED_FUNDING_ACCOUNT", "network_linkage", "medium",
            "Wallet shares a funding account with other wallets",
            "More than one wallet draws on the same funding account.",
            ("account_id", "wallet_id"),
        ),
        RuleSpec(
            "R801_MISSING_EVIDENCE", "completeness", "medium",
            "Required evidence is missing from the record",
            "A field needed to judge this case is absent. This is not a risk claim; it is the "
            "reason the case cannot be decided yet.",
            ("device_id", "mcc", "lifetime_txn_count"),
        ),
        RuleSpec(
            "R802_UNTRUSTED_INSTRUCTIONS", "completeness", "high",
            "Free text contains instructions aimed at an automated reader",
            "Merchant-supplied text is attacker-controlled. Text that addresses a model rather "
            "than a person is treated as an attack surface and quarantined before any AI reads it.",
            ("merchant_note",),
        ),
        RuleSpec(
            "R803_AUTHORITY_CLAIM", "completeness", "high",
            "Free text claims an approval that is not in the system",
            "The note asserts a pre-approval, exception or sign-off. No such record exists, and "
            "an assertion in free text is not an authorisation.",
            ("merchant_note",),
        ),
    )
}

# Written independently of the generator's attack strings on purpose: a defence
# tested only against the exact payloads it was built from measures nothing.
INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", "override_instructions"),
    (r"disregard\s+(the\s+)?(risk\s+)?(signals|instructions|rules|warnings)", "override_instructions"),
    (r"\b(system|assistant|developer)\s*[:>]", "role_impersonation"),
    (r"#{2,}\s*new\s+instructions", "instruction_injection"),
    (r"<!--.*?(assistant|system|ai)\b.*?-->", "hidden_instruction"),
    (r"you\s+are\s+now\s+in\s+\w+\s+mode", "mode_switch"),
    (r"(set|mark|force)\s+.{0,30}(recommended_action|action|verdict)\s*(=|to)\s*\w+", "output_forcing"),
    (r"(output|return|print|reveal|show)\s+.{0,40}(account\s+number|full\s+customer\s+record|"
     r"internal\s+risk\s+notes|api\s+key|password|credential)", "data_exfiltration"),
    (r"skip\s+the\s+\w+\s+section", "output_forcing"),
    (r"auto[- ]?release", "output_forcing"),
    (r"do\s+not\s+(create\s+a\s+case|review|hold)", "output_forcing"),
)

AUTHORITY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"pre[- ]?approved", "claimed_preapproval"),
    (r"already\s+(been\s+)?(reviewed|cleared|approved)", "claimed_prior_review"),
    (r"policy\s+exception\b", "claimed_exception"),
    (r"\bexception\s+(was\s+|has\s+been\s+)?granted\b", "claimed_exception"),
    (r"(compliance|risk)\s+(team\s+)?(has\s+)?(already\s+)?(cleared|signed[- ]off|approved)",
     "claimed_signoff"),
    (r"\bdo not (hold|review)\b", "instructed_no_review"),
    (r"close\s+the\s+case", "instructed_closure"),
)

_INJECTION_RE = tuple((re.compile(p, re.IGNORECASE | re.DOTALL), label) for p, label in INJECTION_PATTERNS)
_AUTHORITY_RE = tuple((re.compile(p, re.IGNORECASE), label) for p, label in AUTHORITY_PATTERNS)


def match_injection_patterns(text: str) -> list[str]:
    """Labels of every injection pattern present in `text`."""
    if not text:
        return []
    return sorted({label for pattern, label in _INJECTION_RE if pattern.search(text)})


def match_authority_patterns(text: str) -> list[str]:
    if not text:
        return []
    return sorted({label for pattern, label in _AUTHORITY_RE if pattern.search(text)})


def _usd_minor(minor: int, currency: str) -> float:
    """Amount in USD, for cross-currency comparison only - never for a ledger."""
    exponent = Money(0, currency).exponent
    return float(minor) / (10 ** exponent) / float(PER_USD[currency])


@dataclass
class RuleContext:
    """Everything the rules read. Built once per run, not per transaction."""

    settings: Settings
    transactions: pd.DataFrame
    merchants: pd.DataFrame
    wallets: pd.DataFrame
    breaks: pd.DataFrame
    now: datetime

    def __post_init__(self) -> None:
        self.merchant_by_id = self.merchants.set_index("merchant_id").to_dict("index")
        self.wallet_by_id = self.wallets.set_index("wallet_id").to_dict("index")

        self.breaks_by_txn: dict[str, list[dict]] = defaultdict(list)
        for row in self.breaks.to_dict("records"):
            self.breaks_by_txn[str(row["transaction_id"])].append(row)

        payout_counts: dict[str, set[str]] = defaultdict(set)
        for mid, merchant in self.merchant_by_id.items():
            payout_counts[str(merchant["payout_account_id"])].add(str(mid))
        self.payout_groups = {k: v for k, v in payout_counts.items() if len(v) > 1}

        funding_counts: dict[str, set[str]] = defaultdict(set)
        for wid, wallet in self.wallet_by_id.items():
            funding_counts[str(wallet["account_id"])].add(str(wid))
        self.funding_groups = {k: v for k, v in funding_counts.items() if len(v) > 1}

        frame = self.transactions.sort_values("created_at")
        self.by_wallet: dict[str, list[dict]] = defaultdict(list)
        for row in frame.to_dict("records"):
            self.by_wallet[str(row["wallet_id"])].append(row)

        self.by_idempotency: dict[str, list[dict]] = defaultdict(list)
        for row in frame.to_dict("records"):
            self.by_idempotency[str(row["idempotency_key"])].append(row)

        self.refunds_by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in frame.to_dict("records"):
            if int(row.get("refunded_minor") or 0) > 0:
                self.refunds_by_pair[(str(row["wallet_id"]), str(row["merchant_id"]))].append(row)

        # Each wallet's own typical ticket, in USD, so "large" is relative.
        self.wallet_median_usd: dict[str, float] = {}
        for wallet_id, rows in self.by_wallet.items():
            values = [
                _usd_minor(int(r["captured_minor"]), str(r["presentment_currency"]))
                for r in rows
                if int(r["captured_minor"] or 0) > 0
            ]
            if values:
                values.sort()
                self.wallet_median_usd[wallet_id] = values[len(values) // 2]


def evaluate(context: RuleContext) -> pd.DataFrame:
    """Run every rule over every transaction. Returns `risk.signals` rows."""
    signals: list[dict] = []
    fired_by_txn: dict[str, list[tuple[str, str]]] = {}
    settings = context.settings

    for row in context.transactions.to_dict("records"):
        txn_id = str(row["transaction_id"])
        merchant = context.merchant_by_id.get(str(row["merchant_id"]), {})
        wallet = context.wallet_by_id.get(str(row["wallet_id"]), {})
        breaks = context.breaks_by_txn.get(txn_id, [])
        fired: list[tuple[str, str]] = []

        # --- reconciliation-driven rules (the money already disagreed) ----
        for record in breaks:
            kind = str(record["break_type"])
            if kind == "auth_capture_gap":
                fired.append(("R102_AUTH_CAPTURE_GAP", str(record["detail"])))
            elif kind == "fee_off_schedule":
                fired.append(("R402_FEE_OFF_SCHEDULE", str(record["detail"])))
            elif kind == "fx_settlement_outside_tolerance":
                fired.append(("R401_FX_OUT_OF_TOLERANCE", str(record["detail"])))
            elif kind == "quote_expired_at_capture":
                fired.append(("R403_STALE_FX_QUOTE", str(record["detail"])))
            elif kind == "double_credit":
                fired.append(("R602_DOUBLE_CREDIT", str(record["detail"])))

        if int(row.get("rejected_event_count") or 0) > 0:
            fired.append((
                "R103_QUARANTINED_EVENTS",
                f"{int(row['rejected_event_count'])} event(s) were rejected by the state machine "
                "and did not reach the ledger",
            ))

        # --- duplication --------------------------------------------------
        siblings = [
            other for other in context.by_idempotency.get(str(row["idempotency_key"]), [])
            if str(other["transaction_id"]) != txn_id
            and str(other["wallet_id"]) == str(row["wallet_id"])
            and str(other["merchant_id"]) == str(row["merchant_id"])
            and int(other["captured_minor"] or 0) == int(row["captured_minor"] or 0)
            and int(row["captured_minor"] or 0) > 0
            and abs((pd.Timestamp(other["created_at"]) - pd.Timestamp(row["created_at"])).total_seconds())
            <= settings.duplicate_window_seconds
        ]
        if siblings:
            others = ", ".join(sorted(str(s["transaction_id"]) for s in siblings)[:3])
            gap = int(abs(
                (pd.Timestamp(siblings[0]["created_at"]) - pd.Timestamp(row["created_at"])).total_seconds()
            ))
            fired.append((
                "R101_DUPLICATE_IDEMPOTENCY",
                f"idempotency key {row['idempotency_key']} is shared with {others} for the same "
                f"wallet, merchant and amount, {gap}s apart",
            ))

        # --- velocity -----------------------------------------------------
        window = timedelta(minutes=settings.velocity_window_minutes)
        created = pd.Timestamp(row["created_at"])
        neighbours = [
            other for other in context.by_wallet.get(str(row["wallet_id"]), [])
            if abs(pd.Timestamp(other["created_at"]) - created) <= window
        ]
        if len(neighbours) >= settings.velocity_threshold:
            baseline = int(wallet.get("baseline_weekly_count") or 1)
            fired.append((
                "R201_WALLET_VELOCITY",
                f"{len(neighbours)} payments from this wallet inside "
                f"{settings.velocity_window_minutes} minutes, against a baseline of about "
                f"{baseline} per week",
            ))

        captured_usd = _usd_minor(int(row["captured_minor"] or 0), str(row["presentment_currency"]))
        median_usd = context.wallet_median_usd.get(str(row["wallet_id"]), 0.0)
        if captured_usd > 0 and median_usd > 0 and captured_usd >= 12 * median_usd:
            fired.append((
                "R202_AMOUNT_ANOMALY",
                f"about USD {captured_usd:,.0f} against this wallet's median of "
                f"USD {median_usd:,.0f} - {captured_usd / median_usd:.0f}x",
            ))

        # --- geography and device ------------------------------------------
        ip_country = str(row["ip_country"] or "")
        wallet_country = str(row["wallet_country"] or "")
        payer_country = str(row["payer_country"] or "")
        if ip_country and wallet_country and ip_country != wallet_country:
            fired.append((
                "R301_GEO_MISMATCH",
                f"payment originated in {ip_country}; the wallet is registered in {wallet_country}",
            ))

        hour_window = timedelta(hours=1)
        nearby = [
            other for other in context.by_wallet.get(str(row["wallet_id"]), [])
            if abs(pd.Timestamp(other["created_at"]) - created) <= hour_window
        ]
        devices = {str(o["device_id"]) for o in nearby if str(o["device_id"])}
        if len(devices) >= 3:
            fired.append((
                "R302_DEVICE_HOPPING",
                f"{len(devices)} distinct devices used by this wallet within an hour: "
                + ", ".join(sorted(devices)[:4]),
            ))

        speed = _implied_speed(context, row)
        if speed is not None and speed > MAX_PLAUSIBLE_SPEED_KMH:
            fired.append((
                "R303_IMPOSSIBLE_TRAVEL",
                f"implied travel speed of {speed:,.0f} km/h between consecutive payments; "
                f"the fastest commercial aircraft is about {MAX_PLAUSIBLE_SPEED_KMH:,.0f} km/h",
            ))

        distinct = {c for c in (wallet_country, payer_country, ip_country) if c}
        if len(distinct) == 3 and str(wallet.get("kyc_level", "")) == "basic":
            fired.append((
                "R304_JURISDICTION_CONFLICT",
                f"wallet registered in {wallet_country}, payer in {payer_country}, IP in "
                f"{ip_country}, on a wallet with basic KYC only",
            ))

        # --- merchant profile ----------------------------------------------
        baseline_ticket = int(merchant.get("baseline_ticket_minor") or 0)
        if baseline_ticket > 0 and int(row["captured_minor"] or 0) > 0:
            baseline_usd = _usd_minor(baseline_ticket, str(merchant.get("settlement_currency", "USD")))
            if baseline_usd > 0 and captured_usd >= 15 * baseline_usd:
                fired.append((
                    "R501_MCC_TICKET_ANOMALY",
                    f"about USD {captured_usd:,.0f} against an MCC {merchant.get('mcc')} baseline "
                    f"ticket of USD {baseline_usd:,.0f} - {captured_usd / baseline_usd:.0f}x",
                ))

        # --- disputes --------------------------------------------------------
        pair = (str(row["wallet_id"]), str(row["merchant_id"]))
        refund_siblings = [
            other for other in context.refunds_by_pair.get(pair, [])
            if abs(pd.Timestamp(other["created_at"]) - created) <= timedelta(days=7)
        ]
        if int(row.get("refunded_minor") or 0) > 0 and len(refund_siblings) >= 3:
            fired.append((
                "R601_REFUND_CHAIN",
                f"{len(refund_siblings)} refunds between this payer and merchant within 7 days",
            ))

        # --- completeness and adversarial text ---------------------------------
        missing: list[str] = []
        if not str(row.get("device_id") or ""):
            missing.append("device fingerprint")
        if not str(merchant.get("mcc") or ""):
            missing.append("merchant category")
        if int(wallet.get("lifetime_txn_count") or 0) == 0:
            missing.append("payer history")
        if missing:
            fired.append((
                "R801_MISSING_EVIDENCE",
                "missing: " + ", ".join(missing) + " - the case cannot be decided as it stands",
            ))

        note = str(row.get("merchant_note") or "")
        injection_labels = match_injection_patterns(note)
        if injection_labels:
            fired.append((
                "R802_UNTRUSTED_INSTRUCTIONS",
                "merchant free text matched injection patterns: " + ", ".join(injection_labels),
            ))
        authority_labels = match_authority_patterns(note)
        if authority_labels:
            fired.append((
                "R803_AUTHORITY_CLAIM",
                "merchant free text claims authority it does not have: " + ", ".join(authority_labels),
            ))

        fired_by_txn[txn_id] = fired

    # --- second pass: network linkage --------------------------------------
    # Linkage is deliberately not a first-pass trigger. A shared payout account
    # is a merchant-level fact true of every payment that merchant ever takes;
    # firing it standalone would put a legitimate franchise group's entire volume
    # in the queue and teach analysts to click through the signal.
    #
    # It is raised in two situations, both of which need the whole batch to be
    # known - which is why this is a second pass:
    #   (a) as an amplifier, where the transaction already has another signal;
    #   (b) as a ring escalation, where the payout group has three or more
    #       merchants and at least one of them produced a high-severity signal
    #       somewhere in the batch. That is the shape of a merchant ring, and it
    #       is not visible from any single transaction.
    merchants_with_high_signal = {
        str(row["merchant_id"])
        for row in context.transactions.to_dict("records")
        if any(
            SEVERITY_ORDER.get(RULES[rule_id].severity, 1) >= SEVERITY_ORDER["high"]
            for rule_id, _ in fired_by_txn.get(str(row["transaction_id"]), [])
        )
    }

    for row in context.transactions.to_dict("records"):
        txn_id = str(row["transaction_id"])
        merchant = context.merchant_by_id.get(str(row["merchant_id"]), {})
        wallet = context.wallet_by_id.get(str(row["wallet_id"]), {})
        fired = fired_by_txn.setdefault(txn_id, [])

        payout = str(merchant.get("payout_account_id", ""))
        group = context.payout_groups.get(payout, set())
        if group:
            implicated = sorted(group & merchants_with_high_signal)
            ring_escalation = len(group) >= 3 and bool(implicated)
            if fired or ring_escalation:
                peers = sorted(group - {str(row["merchant_id"])})
                detail = (
                    f"payout account {payout} is also used by {len(peers)} other merchant(s): "
                    + ", ".join(peers[:4])
                )
                if ring_escalation:
                    detail += (
                        f"; {len(implicated)} merchant(s) in this group produced high-severity "
                        "signals in the same batch"
                    )
                fired.append(("R701_SHARED_PAYOUT_ACCOUNT", detail))

        if fired:
            funding = str(wallet.get("account_id", ""))
            if funding in context.funding_groups:
                peers = sorted(context.funding_groups[funding] - {str(row["wallet_id"])})
                fired.append((
                    "R702_SHARED_FUNDING_ACCOUNT",
                    f"funding account {funding} is shared with {len(peers)} other wallet(s)",
                ))

    for txn_id, fired in fired_by_txn.items():
        for rule_id, detail in fired:
            spec = RULES[rule_id]
            signals.append({
                "signal_id": f"SIG_{txn_id}_{rule_id}",
                "transaction_id": txn_id,
                "rule_id": rule_id,
                "rule_version": RULES_VERSION,
                "signal_family": spec.family,
                "severity": spec.severity,
                "weight": spec.weight,
                "title": spec.title,
                "detail": detail,
                "evidence_fields": "|".join(spec.evidence_fields),
                "fired_at": context.now,
            })

    return pd.DataFrame(signals, columns=[
        "signal_id", "transaction_id", "rule_id", "rule_version", "signal_family",
        "severity", "weight", "title", "detail", "evidence_fields", "fired_at",
    ])


def _implied_speed(context: RuleContext, row: dict) -> float | None:
    """Speed needed to get from the previous payment's IP country to this one."""
    history = context.by_wallet.get(str(row["wallet_id"]), [])
    created = pd.Timestamp(row["created_at"])
    previous = None
    for other in history:
        other_at = pd.Timestamp(other["created_at"])
        if other_at < created and str(other["ip_country"]):
            if previous is None or other_at > pd.Timestamp(previous["created_at"]):
                previous = other
    if previous is None:
        return None
    from_country = str(previous["ip_country"])
    to_country = str(row["ip_country"])
    if not from_country or not to_country or from_country == to_country:
        return None
    hours = (created - pd.Timestamp(previous["created_at"])).total_seconds() / 3600.0
    if hours <= 0:
        return None
    try:
        distance = haversine_km(from_country, to_country)
    except KeyError:
        return None
    return distance / hours
