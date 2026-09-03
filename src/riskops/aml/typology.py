"""The six typologies this release detects, as one shared vocabulary.

A typology is a *shape of behaviour*, not a verdict. The single most important
sentence in this module, and the one the whole product is arranged around:

    An unusual transaction is not a laundered transaction.

Nothing here concludes that a customer is laundering money. A typology match
says a pattern is present that is worth a person's time, and it says which
transfers and entities to look at. What it means is for an investigator to
decide, and the disposition vocabulary in `investigation.py` has no value that
asserts a crime.

Generator and detector share this file but not their reasoning. The generator
uses `TYPOLOGIES` to know what to build; the detector uses it to know what to
name what it found. The detector never reads the generator's `scenario_id` -
it has to rediscover the pattern from behaviour alone, which is the only reason
the recall numbers in the evaluation report mean anything. `test_typologies.py`
enforces that separation by running the detectors over a world with the
provenance columns dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Bumped when a threshold, window or matching condition changes - never for a
# wording change. Alerts carry the version that produced them so a case opened
# last month can still be read against the rules that opened it.
TYPOLOGY_VERSION = "1.0.0"

SEVERITY_ORDER: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass(frozen=True)
class Typology:
    """One detectable pattern.

    `explanation_template` is filled with the detector's feature values and is
    what an investigator reads first. It is phrased as an observation - "N
    transfers totalling X" - and never as a conclusion. `counter_evidence_hints`
    is the list of innocent explanations a reviewer should actively look for;
    the workbench renders them next to the evidence rather than below it,
    because a queue under time pressure reads top-down and stops early.
    """

    typology_id: str
    key: str
    title: str
    severity: str
    question: str
    explanation_template: str
    # The same sentence in Chinese, taking the same fields. Kept beside the
    # English rather than in the interface's translation table because it is a
    # format string with named holes, not a phrase: a translator who reorders
    # the clauses must be able to see which values move with them.
    explanation_template_zh: str
    evidence_fields: tuple[str, ...]
    counter_evidence_hints: tuple[str, ...]
    thresholds: dict[str, float] = field(default_factory=dict)

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER[self.severity]


STRUCTURING = Typology(
    typology_id="AML_T01_STRUCTURING",
    key="structuring",
    title="Structured transfers below a reporting threshold",
    severity="high",
    question="Were several transfers sized to stay under a threshold rather than sized by need?",
    explanation_template=(
        "{transfer_count} transfers totalling {total_usd} USD left this account in "
        "{window_hours} hours, {under_threshold_count} of them between {band_low} and "
        "{threshold} USD, to {beneficiary_count} beneficiary account(s). Individually each "
        "sits below the {threshold} USD reporting threshold."
    ),
    explanation_template_zh=(
        "{transfer_count} 笔转账合计 {total_usd} 美元在 {window_hours} 小时内离开该账户,其中 "
        "{under_threshold_count} 笔金额在 {band_low} 至 {threshold} 美元之间,流向 "
        "{beneficiary_count} 个收款账户。单笔都低于 {threshold} 美元的申报阈值。"
    ),
    evidence_fields=(
        "transfer_count", "total_usd", "window_hours", "under_threshold_count",
        "beneficiary_count", "threshold", "band_low", "largest_usd",
    ),
    counter_evidence_hints=(
        "A payroll, rent or instalment schedule can produce similar amounts on a regular cadence.",
        "Per-transfer limits set by the customer's own bank or the channel can cap amounts "
        "without any intent to avoid a threshold.",
        "Check whether the same pattern is present in the months before the alert window; a "
        "long-standing habit is weaker evidence than a new one.",
    ),
    thresholds={"threshold_usd": 10_000.0, "band_low_usd": 7_000.0,
                "window_hours": 72.0, "min_transfers": 3.0},
)

RAPID_MOVEMENT = Typology(
    typology_id="AML_T02_RAPID_MOVEMENT",
    key="rapid_movement",
    title="Funds forwarded cross-border shortly after arriving",
    severity="high",
    question="Did this account hold the money, or only pass it on?",
    explanation_template=(
        "{outbound_usd} USD left this account within {hold_minutes} minutes of "
        "{inbound_usd} USD arriving, forwarding {passthrough_pct}% of what came in to "
        "{destination_country}. The account retained {retained_usd} USD."
    ),
    explanation_template_zh=(
        "{inbound_usd} 美元到账后 {hold_minutes} 分钟内,{outbound_usd} 美元离开该账户,"
        "相当于流入金额的 {passthrough_pct}%,流向 {destination_country}。账户留存 {retained_usd} 美元。"
    ),
    evidence_fields=(
        "inbound_usd", "outbound_usd", "hold_minutes", "passthrough_pct",
        "retained_usd", "destination_country", "inbound_transfer_id", "outbound_transfer_id",
    ),
    counter_evidence_hints=(
        "Treasury sweeps, supplier settlement and payroll runs are all fast by design.",
        "A named, consistent counterparty on both legs is a weaker signal than a new one.",
        "Check whether the customer's declared business makes same-day forwarding normal.",
    ),
    thresholds={"max_hold_minutes": 1440.0, "min_passthrough_pct": 80.0,
                "min_amount_usd": 5_000.0},
)

FUNNEL_ACCOUNT = Typology(
    typology_id="AML_T03_FUNNEL_ACCOUNT",
    key="funnel_account",
    title="Many unrelated senders converging on one account",
    severity="high",
    question="Why are these particular senders all paying the same account?",
    explanation_template=(
        "{sender_count} accounts across {sender_country_count} countries sent "
        "{total_usd} USD to this account in {window_days} days. "
        "{unrelated_sender_count} of the senders share no customer, no beneficial owner and "
        "no device with each other."
    ),
    explanation_template_zh=(
        "{window_days} 天内,来自 {sender_country_count} 个国家/地区的 {sender_count} 个账户"
        "向该账户汇入 {total_usd} 美元。其中 {unrelated_sender_count} 个付款方彼此之间"
        "没有共同的客户、实际受益人或设备。"
    ),
    evidence_fields=(
        "sender_count", "sender_country_count", "total_usd", "window_days",
        "unrelated_sender_count", "transfer_count",
    ),
    counter_evidence_hints=(
        "Merchants, marketplaces, schools and landlords are all legitimately many-to-one.",
        "Check the declared business: a collection account for a real business explains the shape.",
        "Remittance corridors concentrate by nature - many senders in one country paying one "
        "family account is ordinary.",
    ),
    thresholds={"min_senders": 8.0, "window_days": 14.0, "min_total_usd": 20_000.0},
)

CIRCULAR_FLOW = Typology(
    typology_id="AML_T04_CIRCULAR_FLOW",
    key="circular_flow",
    title="Funds returning to their origin through intermediaries",
    severity="critical",
    question="Did this money travel, or only appear to?",
    explanation_template=(
        "{hop_count} transfers moved {total_usd} USD from this account through "
        "{country_path} and back to {return_target}, returning {return_pct}% of the original "
        "amount in {elapsed_hours} hours."
    ),
    explanation_template_zh=(
        "{hop_count} 笔转账将 {total_usd} 美元从该账户经 {country_path} "
        "转回 {return_target},在 {elapsed_hours} 小时内回流了原始金额的 {return_pct}%。"
    ),
    evidence_fields=(
        "hop_count", "total_usd", "country_path", "return_target",
        "return_pct", "elapsed_hours", "cycle_account_ids",
    ),
    counter_evidence_hints=(
        "Intra-group treasury movements between accounts of one owner look circular and are routine.",
        "A returned or reversed payment produces a two-hop cycle with an innocent cause; check "
        "whether any leg is a refund.",
        "FX round-tripping to obtain a better rate is legal in most corridors.",
    ),
    thresholds={"min_hops": 3.0, "max_hops": 5.0, "min_return_pct": 70.0,
                "max_elapsed_hours": 168.0},
)

PROFILE_MISMATCH = Typology(
    typology_id="AML_T05_PROFILE_MISMATCH",
    key="profile_mismatch",
    title="Activity inconsistent with the declared profile",
    severity="medium",
    question="Does this account behave like what the customer said it was for?",
    explanation_template=(
        "This account moved {actual_usd} USD in {window_days} days against a declared "
        "expectation of {expected_usd} USD ({ratio}x). Declared purpose is "
        "{declared_business}; {mismatch_count} of {transfer_count} transfers declare "
        "{observed_purposes}."
    ),
    explanation_template_zh=(
        "该账户在 {window_days} 天内转出 {actual_usd} 美元,而申报预期为 {expected_usd} 美元"
        "({ratio} 倍)。申报用途为 {declared_business};{transfer_count} 笔转账中有 "
        "{mismatch_count} 笔申报为 {observed_purposes}。"
    ),
    evidence_fields=(
        "actual_usd", "expected_usd", "ratio", "declared_business",
        "observed_purposes", "mismatch_count", "transfer_count", "window_days",
    ),
    counter_evidence_hints=(
        "A business that genuinely grew will breach its onboarding expectation; check whether "
        "the rise is sustained or a spike.",
        "One-off events - a property sale, an inheritance, a funding round - explain a single "
        "large deviation.",
        "A stale profile is a data problem, not a customer problem. Check when it was last "
        "reviewed before treating the gap as a signal.",
    ),
    thresholds={"min_ratio": 4.0, "window_days": 30.0, "min_actual_usd": 15_000.0},
)

MISSING_INFORMATION = Typology(
    typology_id="AML_T06_MISSING_INFORMATION",
    key="missing_information",
    title="Required payment information absent or unverified",
    severity="medium",
    question="Can we say who is on both ends of this money, and why it moved?",
    explanation_template=(
        "{affected_count} of {transfer_count} transfers ({affected_usd} USD) are missing "
        "required information: {missing_fields}. The beneficiary information status is "
        "{beneficiary_status} and the customer's beneficial ownership is {ownership_status}."
    ),
    explanation_template_zh=(
        "{transfer_count} 笔转账中有 {affected_count} 笔({affected_usd} 美元)缺少必需信息:"
        "{missing_fields}。收款人信息状态为 {beneficiary_status},该客户的实际受益人状态为 {ownership_status}。"
    ),
    evidence_fields=(
        "affected_count", "transfer_count", "affected_usd", "missing_fields",
        "beneficiary_status", "ownership_status",
    ),
    counter_evidence_hints=(
        "Missing data is very often an operational defect at the sending institution, not "
        "concealment - the fix is a request for information, not an escalation.",
        "Some corridors and channels legitimately carry less structured data.",
        "Check whether the same fields are missing across all of that channel's traffic; a "
        "systemic gap says nothing about this customer.",
    ),
    thresholds={"min_affected": 3.0, "min_affected_usd": 5_000.0},
)

TYPOLOGIES: tuple[Typology, ...] = (
    STRUCTURING,
    RAPID_MOVEMENT,
    FUNNEL_ACCOUNT,
    CIRCULAR_FLOW,
    PROFILE_MISMATCH,
    MISSING_INFORMATION,
)

TYPOLOGY_BY_ID: dict[str, Typology] = {t.typology_id: t for t in TYPOLOGIES}
TYPOLOGY_BY_KEY: dict[str, Typology] = {t.key: t for t in TYPOLOGIES}


def get(key_or_id: str) -> Typology:
    if key_or_id in TYPOLOGY_BY_ID:
        return TYPOLOGY_BY_ID[key_or_id]
    return TYPOLOGY_BY_KEY[key_or_id]


def render_explanation(
    typology: Typology, features: dict[str, object], language: str = "en"
) -> str:
    """Fill a typology's template, and say so plainly when a field is absent.

    A detector that computed one feature fewer than the template expects would
    otherwise raise inside the pipeline, or - worse, with a lenient formatter -
    silently print an explanation with a hole in it. An investigator reading
    "(not computed)" knows not to rely on that clause; an investigator reading a
    smooth sentence with a missing number does not.
    """
    template = (
        typology.explanation_template_zh if language == "zh"
        else typology.explanation_template
    )
    missing = "(未计算)" if language == "zh" else "(not computed)"
    safe = {field: features.get(field, missing) for field in typology.evidence_fields}
    try:
        return template.format(**safe)
    except (KeyError, IndexError):  # a template field outside evidence_fields
        return template


def parse_features(feature_values: str) -> dict[str, str]:
    """Read a stored alert's `feature_values` back into a dict.

    Alerts are written once, in English, and read by whoever opens them. Rather
    than storing two copies of every sentence, the measured values are stored
    once in a parseable form and the sentence is rebuilt in the reader's
    language at display time - the same arrangement the audit log's diagnostic
    reasons use.
    """
    out: dict[str, str] = {}
    for chunk in str(feature_values).split("; "):
        key, sep, value = chunk.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out
