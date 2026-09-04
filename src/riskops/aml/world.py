"""Generate a synthetic cross-border remittance world.

Everything here is invented. No real customer, account, institution, corridor
volume or transfer is represented, and no field is derived from one. The names
are drawn from a fixed word list, the countries are real country codes used as
labels only, and the behaviour is whatever these functions say it is.

Two rules govern the generation, and both exist so the evaluation numbers are
worth reading:

**Injected patterns must be reachable from behaviour alone.** Each typology is
built out of ordinary transfers whose *shape* matches the typology. The
`scenario_id` written next to them is provenance for scoring, and the detectors
never see it. A generator that also planted a flag would be scoring the flag.

**Normal behaviour must be able to produce the same shapes.** The background
population deliberately contains fast-forwarding treasury accounts, collection
accounts with many senders, businesses that outgrew their onboarding estimate,
and channels that carry incomplete data. Without those, every detector would
score perfectly and the precision figure would be a measurement of nothing. The
false positives in the evaluation report come from here, on purpose.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from ..config import Settings
from ..money import exponent_of
from .typology import (
    CIRCULAR_FLOW,
    FUNNEL_ACCOUNT,
    MISSING_INFORMATION,
    PROFILE_MISMATCH,
    RAPID_MOVEMENT,
    STRUCTURING,
)

WORLD_VERSION = "1.0.0"

# Country codes used as labels for synthetic corridors. Presence here says
# nothing about any country's actual risk; the "higher-attention" split below is
# a property of this generated dataset and exists so corridor concentration has
# something to concentrate on.
# Restricted to the currencies `money.py` already knows, so amounts run through
# the same exponent table as the payments side rather than a second one. BHD is
# kept deliberately: it has three decimal places, so any code that assumed two
# breaks here rather than in production.
CORRIDOR_COUNTRIES: tuple[str, ...] = (
    "SG", "HK", "GB", "US", "AU", "JP", "DE", "FR", "NL", "MY",
    "ID", "PH", "VN", "TH", "KR", "CA", "CN", "BH",
)
HIGHER_ATTENTION_COUNTRIES: tuple[str, ...] = ("PH", "VN", "ID", "MY", "BH")

CURRENCY_BY_COUNTRY: dict[str, str] = {
    "SG": "SGD", "HK": "HKD", "GB": "GBP", "US": "USD", "AU": "AUD",
    "JP": "JPY", "DE": "EUR", "FR": "EUR", "NL": "EUR", "MY": "MYR",
    "ID": "IDR", "PH": "PHP", "VN": "VND", "TH": "THB",
    "KR": "KRW", "CA": "CAD", "CN": "CNY", "BH": "BHD",
}

# USD per unit of currency. Fixed, invented, and used only to put transfers on
# one comparable scale - `normalized_amount_usd_minor`. Not a market rate.
USD_PER_UNIT: dict[str, float] = {
    "USD": 1.0, "SGD": 0.74, "HKD": 0.128, "GBP": 1.27, "AUD": 0.66,
    "JPY": 0.0067, "EUR": 1.08, "MYR": 0.21, "IDR": 0.000062,
    "PHP": 0.0176, "VND": 0.0000395, "THB": 0.028,
    "KRW": 0.00073, "CAD": 0.73, "CNY": 0.138, "BHD": 2.65,
}

CHANNELS: tuple[str, ...] = ("wallet_transfer", "bank_wire", "card_funded", "agent_cash_in")

DECLARED_PURPOSES: tuple[str, ...] = (
    "family_support", "goods_payment", "services_payment", "salary",
    "tuition", "property", "investment", "loan_repayment", "other",
)

MERCHANT_CATEGORIES: tuple[str, ...] = (
    "remittance", "wholesale_trade", "professional_services", "education",
    "travel", "electronics", "logistics", "construction", "consulting",
    "food_service", "none",
)

BUSINESS_TYPES: dict[str, tuple[str, ...]] = {
    "wholesale_trade": ("goods_payment", "logistics"),
    "professional_services": ("services_payment", "salary"),
    "education": ("tuition",),
    "logistics": ("goods_payment", "services_payment"),
    "construction": ("services_payment", "salary"),
    "consulting": ("services_payment",),
    "food_service": ("goods_payment", "salary"),
}

# Individuals declare a purpose from this narrower set; a business purpose on an
# individual account is one of the things PROFILE_MISMATCH looks for.
INDIVIDUAL_PURPOSES: tuple[str, ...] = (
    "family_support", "tuition", "salary", "property", "other",
)

RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")
BENEFICIARY_INFO_STATES: tuple[str, ...] = ("complete", "partial", "missing")
OWNERSHIP_STATES: tuple[str, ...] = ("verified", "unverified", "not_required")

_GIVEN = ("Wei", "Mei", "Arun", "Siti", "Hana", "Ravi", "Lena", "Omar", "Yuki", "Nadia",
          "Tomas", "Priya", "Kwame", "Elif", "Diego", "Ingrid", "Hassan", "Rosa")
_FAMILY = ("Tan", "Lim", "Kaur", "Rahman", "Sato", "Novak", "Okafor", "Silva", "Kim",
           "Haddad", "Muller", "Costa", "Nguyen", "Ali", "Petrov", "Reyes")
_BIZ_HEAD = ("Meridian", "Northwind", "Harbourline", "Cedarpoint", "Bluefin", "Anvil",
             "Lantern", "Kestrel", "Orchard", "Ironwood", "Saltgate", "Wavelength")
_BIZ_TAIL = ("Trading", "Logistics", "Partners", "Holdings", "Services", "Supply",
             "Consulting", "Ventures", "Exports")



# Every injected scenario is built at one of three difficulties. This is the
# single most important thing in this file for the evaluation to mean anything.
#
# The first version of this generator built each typology squarely inside its
# detector's thresholds, and scored 100% recall on all six. That number measured
# nothing: the generator and the detector were written against the same
# constants, so a miss was impossible by construction. A detector that fires on
# everything would have scored identically.
#
# So scenarios are now spread across the threshold instead of sitting on it:
#
#   clear            comfortably inside the detector's thresholds. Should be found.
#   borderline       just inside. Finding these is what a threshold is worth.
#   below_threshold  deliberately outside - fewer legs, slower, weaker. These
#                    SHOULD be missed, and a detector that finds them all is
#                    over-firing rather than performing well.
#
# The evaluation reports recall separately per difficulty for exactly that
# reason: a high recall on below_threshold is a bad result, not a good one.
DIFFICULTIES: tuple[str, ...] = ("clear", "borderline", "below_threshold")
DIFFICULTY_WEIGHTS: tuple[int, ...] = (55, 30, 15)


def _difficulty(rnd: random.Random) -> str:
    return rnd.choices(DIFFICULTIES, weights=DIFFICULTY_WEIGHTS)[0]


def _minor(amount_major: float, currency: str) -> int:
    return int(round(amount_major * (10 ** exponent_of(currency))))


def _usd_minor(amount_minor: int, currency: str) -> int:
    """Put an amount on the common USD scale, in integer USD cents.

    Every comparison in this product - thresholds, totals, priority - runs on
    this integer, never on the presentment amount. Comparing a JPY amount to a
    USD one without normalising is how a 1,000,000 JPY transfer gets treated as
    a hundred times larger than a 10,000 USD one.
    """
    major = amount_minor / (10 ** exponent_of(currency))
    return int(round(major * USD_PER_UNIT[currency] * 100))


@dataclass
class AmlWorld:
    customers: pd.DataFrame
    accounts: pd.DataFrame
    beneficial_owners: pd.DataFrame
    devices: pd.DataFrame
    transfers: pd.DataFrame
    scenarios: pd.DataFrame
    seed: int
    world_version: str = WORLD_VERSION

    def summary(self) -> dict[str, object]:
        return {
            "customers": len(self.customers),
            "accounts": len(self.accounts),
            "beneficial_owners": len(self.beneficial_owners),
            "devices": len(self.devices),
            "transfers": len(self.transfers),
            "injected_scenarios": len(self.scenarios),
            "seed": self.seed,
            "world_version": self.world_version,
        }


class _Builder:
    """Holds the growing world so the typology injectors can append to it."""

    def __init__(self, settings: Settings, rnd: random.Random, as_of: datetime,
                 history_days: int) -> None:
        self.settings = settings
        self.rnd = rnd
        self.as_of = as_of
        self.start = as_of - timedelta(days=history_days)
        self.history_days = history_days
        self.transfers: list[dict] = []
        self.scenarios: list[dict] = []
        self._counter = 0

    # -- entities ---------------------------------------------------------

    def customers(self, count: int) -> pd.DataFrame:
        rnd = self.rnd
        rows = []
        for index in range(count):
            is_business = rnd.random() < 0.28
            country = rnd.choice(CORRIDOR_COUNTRIES)
            opened = self.start - timedelta(days=rnd.randint(0, 1500))
            if is_business:
                industry = rnd.choice(list(BUSINESS_TYPES))
                name = f"{rnd.choice(_BIZ_HEAD)} {rnd.choice(_BIZ_TAIL)}"
                expected = rnd.choice((40_000, 80_000, 150_000, 300_000))
            else:
                industry = "none"
                name = f"{rnd.choice(_GIVEN)} {rnd.choice(_FAMILY)}"
                expected = rnd.choice((2_000, 5_000, 9_000, 15_000))
            # Risk level is an onboarding judgement in this world, not an
            # outcome. It is deliberately weakly correlated with behaviour: a
            # detector that leant on it would be reading the generator's opinion
            # rather than the transfers.
            risk = rnd.choices(RISK_LEVELS, weights=(72, 22, 6))[0]
            rows.append({
                "customer_id": f"CUST_{index:06d}",
                "customer_name": name,
                "customer_type": "business" if is_business else "individual",
                "home_country": country,
                "industry": industry,
                "customer_risk_level": risk,
                "onboarded_at": opened,
                "expected_monthly_usd": expected,
                "profile_reviewed_at": opened + timedelta(days=rnd.randint(0, 900)),
            })
        return pd.DataFrame(rows)

    def accounts(self, customers: pd.DataFrame) -> pd.DataFrame:
        rnd = self.rnd
        rows = []
        index = 0
        for customer in customers.to_dict("records"):
            for _ in range(rnd.choices((1, 2, 3), weights=(74, 20, 6))[0]):
                country = (
                    customer["home_country"] if rnd.random() < 0.85
                    else rnd.choice(CORRIDOR_COUNTRIES)
                )
                opened = customer["onboarded_at"] + timedelta(days=rnd.randint(0, 400))
                rows.append({
                    "account_id": f"ACCT_{index:06d}",
                    "customer_id": customer["customer_id"],
                    "account_country": country,
                    "currency": CURRENCY_BY_COUNTRY[country],
                    "opened_at": opened,
                    "account_age_days": max(0, (self.as_of - opened).days),
                    "account_type": "business" if customer["customer_type"] == "business"
                                    else "personal",
                })
                index += 1
        return pd.DataFrame(rows)

    def beneficial_owners(self, customers: pd.DataFrame) -> pd.DataFrame:
        """Only businesses have them; individuals get `not_required`.

        The verification status is what MISSING_INFORMATION reads. An
        `unverified` owner is a data gap, and the product treats it as a reason
        to request information - never on its own as a reason to escalate.
        """
        rnd = self.rnd
        rows = []
        index = 0
        for customer in customers.to_dict("records"):
            if customer["customer_type"] != "business":
                continue
            for _ in range(rnd.choices((1, 2, 3), weights=(58, 30, 12))[0]):
                status = rnd.choices(OWNERSHIP_STATES[:2], weights=(80, 20))[0]
                name = f"{rnd.choice(_GIVEN)} {rnd.choice(_FAMILY)}"
                rows.append({
                    "owner_id": f"BO_{index:06d}",
                    "customer_id": customer["customer_id"],
                    # Hashed rather than stored: this is synthetic, but the shape
                    # of the record should be the shape a real one would take.
                    "owner_reference": hashlib.sha256(name.encode()).hexdigest()[:16],
                    "ownership_pct": rnd.choice((25, 40, 51, 75, 100)),
                    "verification_status": status,
                    "recorded_at": customer["onboarded_at"],
                })
                index += 1
        return pd.DataFrame(rows, columns=[
            "owner_id", "customer_id", "owner_reference", "ownership_pct",
            "verification_status", "recorded_at",
        ])

    def devices(self, count: int) -> pd.DataFrame:
        rnd = self.rnd
        rows = []
        for index in range(count):
            rows.append({
                "device_id": f"DEV_{index:06d}",
                "os_family": rnd.choice(("android", "ios", "web", "desktop")),
                "first_seen_at": self.start - timedelta(days=rnd.randint(0, 700)),
                "is_shared_terminal": rnd.random() < 0.06,
            })
        return pd.DataFrame(rows)

    # -- transfers --------------------------------------------------------

    def _next_id(self) -> str:
        self._counter += 1
        return f"TRF_{self._counter:08d}"

    def transfer(
        self,
        *,
        timestamp: datetime,
        payer: dict,
        beneficiary: dict,
        amount_major: float,
        declared_purpose: str,
        channel: str,
        merchant_category: str,
        device_id: str,
        payer_customer: dict,
        beneficiary_info: str = "complete",
        scenario_id: str = "",
        scenario_role: str = "",
    ) -> dict:
        currency = payer["currency"]
        amount_minor = _minor(amount_major, currency)
        row = {
            "transaction_id": self._next_id(),
            "timestamp": timestamp,
            "payer_account": payer["account_id"],
            "beneficiary_account": beneficiary["account_id"],
            "payer_customer_id": payer["customer_id"],
            "beneficiary_customer_id": beneficiary["customer_id"],
            "amount_minor": amount_minor,
            "currency": currency,
            "normalized_amount_usd_minor": _usd_minor(amount_minor, currency),
            "origin_country": payer["account_country"],
            "destination_country": beneficiary["account_country"],
            "declared_purpose": declared_purpose,
            "channel": channel,
            "merchant_category": merchant_category,
            "device_id": device_id,
            "account_age_days": int(payer["account_age_days"]),
            "customer_risk_level": payer_customer["customer_risk_level"],
            "beneficiary_information_status": beneficiary_info,
            "is_cross_border": payer["account_country"] != beneficiary["account_country"],
            # Provenance. Never read by a detector - see the module docstring.
            "scenario_id": scenario_id,
            "scenario_role": scenario_role,
        }
        self.transfers.append(row)
        return row

    def record_scenario(self, scenario_id: str, typology_id: str, subject_account: str,
                        subject_customer: str, transfer_ids: list[str],
                        construction: str, difficulty: str = "clear") -> None:
        """Why this scenario exists and how it was built.

        `construction` is written for a reader auditing the evaluation, not for
        the detector. It is the answer to "how do you know your recall figure is
        not measuring your own hint?"
        """
        self.scenarios.append({
            "scenario_id": scenario_id,
            "typology_id": typology_id,
            "subject_account": subject_account,
            "subject_customer": subject_customer,
            "transfer_ids": "|".join(transfer_ids),
            "transfer_count": len(transfer_ids),
            "difficulty": difficulty,
            "construction": construction,
        })


def _pick_purpose(rnd: random.Random, customer: dict) -> str:
    if customer["customer_type"] == "business":
        allowed = BUSINESS_TYPES.get(customer["industry"], DECLARED_PURPOSES)
        return rnd.choice(allowed)
    return rnd.choice(INDIVIDUAL_PURPOSES)


def _background(builder: _Builder, accounts: pd.DataFrame, customers: pd.DataFrame,
                devices: pd.DataFrame, target_count: int) -> None:
    """Ordinary traffic, including the shapes the detectors will fire on.

    The confounders are deliberate and are listed in SYNTHETIC_DATA_DESIGN.md:
    without a population that sometimes forwards money quickly, collects from
    many senders, or outgrows its declared volume, precision would be measured
    against a world where only the injected cases can possibly match.
    """
    rnd = builder.rnd
    account_rows = accounts.to_dict("records")
    customer_by_id = customers.set_index("customer_id").to_dict("index")
    device_ids = devices["device_id"].tolist()
    span_seconds = int((builder.as_of - builder.start).total_seconds())

    # A minority of accounts behave like collection points or fast forwarders in
    # the ordinary course of business.
    collectors = {r["account_id"] for r in account_rows if rnd.random() < 0.02}
    forwarders = {r["account_id"] for r in account_rows if rnd.random() < 0.02}

    while len(builder.transfers) < target_count:
        payer = rnd.choice(account_rows)
        payer_customer = customer_by_id[payer["customer_id"]]
        if rnd.random() < 0.12 and collectors:
            beneficiary = next(
                (r for r in account_rows if r["account_id"] in collectors), None
            ) or rnd.choice(account_rows)
        else:
            beneficiary = rnd.choice(account_rows)
        if beneficiary["account_id"] == payer["account_id"]:
            continue

        scale = 4_000 if payer["account_type"] == "business" else 700
        amount_usd = max(20.0, rnd.lognormvariate(0, 1.0) * scale)
        rate = USD_PER_UNIT[payer["currency"]]
        amount_major = amount_usd / rate

        # Incomplete beneficiary data is mostly a channel property here, which
        # is why the counter-evidence for MISSING_INFORMATION tells a reviewer
        # to check the channel before reading it as concealment.
        channel = rnd.choice(CHANNELS)
        if channel == "agent_cash_in":
            info = rnd.choices(BENEFICIARY_INFO_STATES, weights=(58, 30, 12))[0]
        else:
            info = rnd.choices(BENEFICIARY_INFO_STATES, weights=(92, 6, 2))[0]

        builder.transfer(
            timestamp=builder.start + timedelta(seconds=rnd.randint(0, span_seconds)),
            payer=payer,
            beneficiary=beneficiary,
            amount_major=amount_major,
            declared_purpose=_pick_purpose(rnd, payer_customer),
            channel=channel,
            merchant_category=(
                rnd.choice(MERCHANT_CATEGORIES) if payer["account_type"] == "business"
                else "none"
            ),
            device_id=rnd.choice(device_ids),
            payer_customer=payer_customer,
            beneficiary_info=info,
        )

        # An ordinary fast forward: money in, money out the same day, innocent.
        if payer["account_id"] in forwarders and rnd.random() < 0.3:
            onward = rnd.choice(account_rows)
            if onward["account_id"] != payer["account_id"]:
                builder.transfer(
                    timestamp=builder.transfers[-1]["timestamp"] + timedelta(
                        minutes=rnd.randint(20, 400)
                    ),
                    payer=payer, beneficiary=onward,
                    amount_major=amount_major * rnd.uniform(0.82, 0.97),
                    declared_purpose=_pick_purpose(rnd, payer_customer),
                    channel=channel, merchant_category="none",
                    device_id=rnd.choice(device_ids), payer_customer=payer_customer,
                )


def _inject_structuring(builder: _Builder, accounts: list[dict], customers: dict,
                        devices: list[str], count: int) -> None:
    """Legs priced under a reporting threshold. Detector wants >= 3 within 72h."""
    rnd = builder.rnd
    threshold = STRUCTURING.thresholds["threshold_usd"]
    band_low = STRUCTURING.thresholds["band_low_usd"]
    for index in range(count):
        difficulty = _difficulty(rnd)
        if difficulty == "clear":
            legs, span_hours = rnd.randint(5, 7), rnd.uniform(6, 40)
        elif difficulty == "borderline":
            legs, span_hours = rnd.randint(3, 4), rnd.uniform(50, 68)
        else:
            # Two legs, or three spread past the 72h window: below the bar on
            # purpose. A detector that reports these is reporting noise.
            legs, span_hours = rnd.choice(((2, 30.0), (3, 110.0)))

        payer = rnd.choice(accounts)
        payer_customer = customers[payer["customer_id"]]
        pool = [a for a in accounts if a["customer_id"] != payer["customer_id"]]
        beneficiaries = rnd.sample(pool, rnd.randint(1, 3))
        start = builder.start + timedelta(
            seconds=rnd.randint(0, max(1, int((builder.as_of - builder.start).total_seconds()
                                              - span_hours * 3600 - 3600)))
        )
        scenario_id = f"SC_STRUCT_{index:04d}"
        ids = []
        for leg in range(legs):
            usd = rnd.uniform(band_low, threshold - 150)
            row = builder.transfer(
                timestamp=start + timedelta(hours=span_hours * leg / max(1, legs - 1)),
                payer=payer,
                beneficiary=rnd.choice(beneficiaries),
                amount_major=usd / USD_PER_UNIT[payer["currency"]],
                declared_purpose=_pick_purpose(rnd, payer_customer),
                channel=rnd.choice(("bank_wire", "wallet_transfer")),
                merchant_category="none",
                device_id=rnd.choice(devices),
                payer_customer=payer_customer,
                scenario_id=scenario_id,
                scenario_role=f"leg_{leg}",
            )
            ids.append(row["transaction_id"])
        builder.record_scenario(
            scenario_id, STRUCTURING.typology_id, payer["account_id"],
            payer["customer_id"], ids,
            f"{legs} transfers priced between {band_low:.0f} and {threshold:.0f} USD across "
            f"{span_hours:.0f}h. No field marks them; only the shape does.",
            difficulty,
        )


def _inject_rapid_movement(builder: _Builder, accounts: list[dict], customers: dict,
                           devices: list[str], count: int) -> None:
    """In then straight out. Detector wants >= 80% forwarded within 24h."""
    rnd = builder.rnd
    for index in range(count):
        difficulty = _difficulty(rnd)
        if difficulty == "clear":
            hold_minutes, passthrough = rnd.randint(15, 300), rnd.uniform(0.90, 0.98)
        elif difficulty == "borderline":
            hold_minutes, passthrough = rnd.randint(1000, 1400), rnd.uniform(0.81, 0.86)
        else:
            # Held too long, or too little of it forwarded, to be this pattern.
            hold_minutes, passthrough = rnd.choice(
                ((rnd.randint(1600, 3000), rnd.uniform(0.88, 0.96)),
                 (rnd.randint(60, 600), rnd.uniform(0.55, 0.74)))
            )

        middle = rnd.choice(accounts)
        middle_customer = customers[middle["customer_id"]]
        pool = [a for a in accounts if a["customer_id"] != middle["customer_id"]]
        source, destination = rnd.sample(pool, 2)
        arrival = builder.start + timedelta(
            seconds=rnd.randint(0, max(1, int((builder.as_of - builder.start).total_seconds()
                                              - hold_minutes * 60 - 3600)))
        )
        usd = rnd.uniform(9_000, 90_000)
        scenario_id = f"SC_RAPID_{index:04d}"
        inbound = builder.transfer(
            timestamp=arrival, payer=source, beneficiary=middle,
            amount_major=usd / USD_PER_UNIT[source["currency"]],
            declared_purpose=rnd.choice(("goods_payment", "services_payment", "investment")),
            channel="bank_wire", merchant_category="none",
            device_id=rnd.choice(devices),
            payer_customer=customers[source["customer_id"]],
            scenario_id=scenario_id, scenario_role="inbound",
        )
        outbound = builder.transfer(
            timestamp=arrival + timedelta(minutes=hold_minutes),
            payer=middle, beneficiary=destination,
            amount_major=(usd * passthrough) / USD_PER_UNIT[middle["currency"]],
            declared_purpose=rnd.choice(("goods_payment", "services_payment", "other")),
            channel=rnd.choice(("bank_wire", "wallet_transfer")), merchant_category="none",
            device_id=rnd.choice(devices), payer_customer=middle_customer,
            scenario_id=scenario_id, scenario_role="outbound",
        )
        builder.record_scenario(
            scenario_id, RAPID_MOVEMENT.typology_id, middle["account_id"],
            middle["customer_id"], [inbound["transaction_id"], outbound["transaction_id"]],
            f"{passthrough * 100:.0f}% of an inbound transfer forwarded after "
            f"{hold_minutes / 60:.1f}h. Ordinary treasury sweeps look the same.",
            difficulty,
        )


def _inject_funnel(builder: _Builder, accounts: list[dict], customers: dict,
                   devices: list[str], count: int) -> None:
    """Many unrelated senders, one account. Detector wants >= 8 within 14 days."""
    rnd = builder.rnd
    for index in range(count):
        difficulty = _difficulty(rnd)
        if difficulty == "clear":
            senders_n, window_days = rnd.randint(12, 18), rnd.uniform(3, 11)
        elif difficulty == "borderline":
            senders_n, window_days = rnd.randint(8, 10), rnd.uniform(11, 13.5)
        else:
            senders_n, window_days = rnd.randint(4, 7), rnd.uniform(5, 13)

        target = rnd.choice(accounts)
        pool = [a for a in accounts if a["customer_id"] != target["customer_id"]]
        senders = rnd.sample(pool, min(senders_n, len(pool)))
        window_start = builder.start + timedelta(
            seconds=rnd.randint(0, max(1, int((builder.as_of - builder.start).total_seconds()
                                              - window_days * 86400 - 3600)))
        )
        scenario_id = f"SC_FUNNEL_{index:04d}"
        ids = []
        for sender in senders:
            row = builder.transfer(
                timestamp=window_start + timedelta(days=rnd.uniform(0, window_days)),
                payer=sender, beneficiary=target,
                amount_major=rnd.uniform(1_800, 9_500) / USD_PER_UNIT[sender["currency"]],
                declared_purpose=rnd.choice(("family_support", "goods_payment", "other")),
                channel=rnd.choice(CHANNELS), merchant_category="none",
                device_id=rnd.choice(devices),
                payer_customer=customers[sender["customer_id"]],
                scenario_id=scenario_id, scenario_role="inbound",
            )
            ids.append(row["transaction_id"])
        builder.record_scenario(
            scenario_id, FUNNEL_ACCOUNT.typology_id, target["account_id"],
            target["customer_id"], ids,
            f"{len(senders)} senders sharing no customer, owner or device paying one account "
            f"across {window_days:.0f} days. Collection accounts produce the same shape.",
            difficulty,
        )


def _inject_circular(builder: _Builder, accounts: list[dict], customers: dict,
                     devices: list[str], count: int) -> None:
    """Money returning to its origin. Detector wants >= 3 hops, >= 70% back, <= 7 days."""
    rnd = builder.rnd
    for index in range(count):
        difficulty = _difficulty(rnd)
        if difficulty == "clear":
            hops, decay, gap = rnd.randint(2, 3), rnd.uniform(0.94, 0.985), (2, 24)
        elif difficulty == "borderline":
            hops, decay, gap = 2, rnd.uniform(0.90, 0.925), (20, 45)
        else:
            # One intermediary (two legs, below the three-hop floor), or so much
            # value shed on the way round that it is no longer a round trip.
            hops, decay, gap = rnd.choice(((1, rnd.uniform(0.93, 0.98), (2, 30)),
                                           (3, rnd.uniform(0.78, 0.86), (2, 30))))

        origin = rnd.choice(accounts)
        pool = [a for a in accounts if a["customer_id"] != origin["customer_id"]]
        intermediaries = rnd.sample(pool, min(hops, len(pool)))
        chain = [origin, *intermediaries, origin]
        start = builder.start + timedelta(
            seconds=rnd.randint(0, max(1, int((builder.as_of - builder.start).total_seconds()
                                              - 8 * 86400)))
        )
        usd = rnd.uniform(15_000, 120_000)
        scenario_id = f"SC_CIRCLE_{index:04d}"
        ids = []
        clock = start
        for leg in range(len(chain) - 1):
            payer, beneficiary = chain[leg], chain[leg + 1]
            usd *= decay
            clock += timedelta(hours=rnd.uniform(*gap))
            row = builder.transfer(
                timestamp=clock, payer=payer, beneficiary=beneficiary,
                amount_major=usd / USD_PER_UNIT[payer["currency"]],
                declared_purpose=rnd.choice(("investment", "loan_repayment", "services_payment")),
                channel="bank_wire", merchant_category="none",
                device_id=rnd.choice(devices),
                payer_customer=customers[payer["customer_id"]],
                scenario_id=scenario_id, scenario_role=f"hop_{leg}",
            )
            ids.append(row["transaction_id"])
        builder.record_scenario(
            scenario_id, CIRCULAR_FLOW.typology_id, origin["account_id"],
            origin["customer_id"], ids,
            f"{len(ids)} legs returning about {decay ** len(ids) * 100:.0f}% of the value to "
            "the originating account. Intra-group treasury movement is the innocent twin.",
            difficulty,
        )


def _inject_profile_mismatch(builder: _Builder, accounts: list[dict], customers: dict,
                             devices: list[str], count: int) -> None:
    """Volume far above the declared expectation. Detector wants >= 4x over 30 days."""
    rnd = builder.rnd
    for index in range(count):
        difficulty = _difficulty(rnd)
        if difficulty == "clear":
            multiple = rnd.uniform(8.0, 14.0)
        elif difficulty == "borderline":
            multiple = rnd.uniform(4.2, 5.5)
        else:
            multiple = rnd.uniform(1.8, 3.4)

        payer = rnd.choice(accounts)
        payer_customer = customers[payer["customer_id"]]
        pool = [a for a in accounts if a["customer_id"] != payer["customer_id"]]
        window_start = builder.start + timedelta(
            seconds=rnd.randint(0, max(1, int((builder.as_of - builder.start).total_seconds()
                                              - 31 * 86400)))
        )
        expected = float(payer_customer["expected_monthly_usd"])
        target_total = expected * multiple
        off_profile = [
            p for p in DECLARED_PURPOSES
            if p not in BUSINESS_TYPES.get(payer_customer["industry"], INDIVIDUAL_PURPOSES)
        ] or list(DECLARED_PURPOSES)
        scenario_id = f"SC_PROFILE_{index:04d}"
        ids = []
        moved = 0.0
        while moved < target_total and len(ids) < 40:
            usd = rnd.uniform(target_total / 18, target_total / 7)
            row = builder.transfer(
                timestamp=window_start + timedelta(days=rnd.uniform(0, 29)),
                payer=payer, beneficiary=rnd.choice(pool),
                amount_major=usd / USD_PER_UNIT[payer["currency"]],
                declared_purpose=rnd.choice(off_profile),
                channel=rnd.choice(CHANNELS), merchant_category=rnd.choice(MERCHANT_CATEGORIES),
                device_id=rnd.choice(devices), payer_customer=payer_customer,
                scenario_id=scenario_id, scenario_role="volume",
            )
            ids.append(row["transaction_id"])
            moved += usd
        builder.record_scenario(
            scenario_id, PROFILE_MISMATCH.typology_id, payer["account_id"],
            payer["customer_id"], ids,
            f"{len(ids)} transfers moving about {moved:,.0f} USD in 30 days against a declared "
            f"{expected:,.0f} USD expectation ({multiple:.1f}x). Genuine growth in the "
            "background population produces the same ratio.",
            difficulty,
        )


def _inject_missing_information(builder: _Builder, accounts: list[dict], customers: dict,
                                devices: list[str], count: int) -> None:
    """Absent beneficiary data or purpose. Detector wants >= 3 affected, >= 5,000 USD."""
    rnd = builder.rnd
    for index in range(count):
        difficulty = _difficulty(rnd)
        if difficulty == "clear":
            affected, low, high = rnd.randint(6, 9), 2_000, 18_000
        elif difficulty == "borderline":
            affected, low, high = rnd.randint(3, 4), 1_400, 4_000
        else:
            # Too few affected transfers, or too little value behind them.
            affected, low, high = rnd.choice(((2, 2_000, 18_000), (4, 200, 900)))

        payer = rnd.choice(accounts)
        payer_customer = customers[payer["customer_id"]]
        pool = [a for a in accounts if a["customer_id"] != payer["customer_id"]]
        scenario_id = f"SC_MISSING_{index:04d}"
        ids = []
        for _ in range(affected):
            row = builder.transfer(
                timestamp=builder.start + timedelta(
                    seconds=rnd.randint(0, int((builder.as_of - builder.start).total_seconds()))
                ),
                payer=payer, beneficiary=rnd.choice(pool),
                amount_major=rnd.uniform(low, high) / USD_PER_UNIT[payer["currency"]],
                declared_purpose=rnd.choice(("other", "")),
                channel=rnd.choice(("agent_cash_in", "wallet_transfer")),
                merchant_category="none", device_id=rnd.choice(devices),
                payer_customer=payer_customer,
                beneficiary_info=rnd.choice(("missing", "partial")),
                scenario_id=scenario_id, scenario_role="incomplete",
            )
            ids.append(row["transaction_id"])
        builder.record_scenario(
            scenario_id, MISSING_INFORMATION.typology_id, payer["account_id"],
            payer["customer_id"], ids,
            f"{affected} transfers with absent or partial beneficiary information. The "
            "background population carries the same gaps on the agent_cash_in channel, "
            "which is why the channel is shown as counter-evidence.",
            difficulty,
        )


def generate(
    settings: Settings,
    *,
    seed: int | None = None,
    n_transfers: int | None = None,
    n_customers: int | None = None,
    scenarios_per_typology: int | None = None,
) -> AmlWorld:
    """Build one complete synthetic AML world. Deterministic in `seed`.

    Sizing is a parameter rather than a constant so the same code produces both
    the small pack committed for the demo and the six-figure run the evaluation
    uses. Nothing about the shape changes between them - only how many.
    """
    resolved_seed = settings.random_seed if seed is None else seed
    rnd = random.Random(resolved_seed)
    as_of = datetime.fromisoformat(settings.as_of_date)

    transfers_target = n_transfers or int(settings.extra.get("aml_n_transfers", 40_000))
    # Roughly one customer per dozen transfers. Too few customers and every
    # account looks like a funnel simply because there is nowhere else for the
    # money to go, which would inflate both the alert count and the apparent
    # recall.
    customer_count = n_customers or max(200, transfers_target // 12)
    per_typology = (
        scenarios_per_typology
        if scenarios_per_typology is not None
        else max(6, transfers_target // 1_200)
    )

    builder = _Builder(settings, rnd, as_of, settings.history_days)
    customers = builder.customers(customer_count)
    accounts = builder.accounts(customers)
    owners = builder.beneficial_owners(customers)
    devices = builder.devices(max(60, customer_count // 3))

    account_rows = accounts.to_dict("records")
    customer_by_id = customers.set_index("customer_id").to_dict("index")
    device_ids = devices["device_id"].tolist()

    # Injected first, so a small world still contains every typology; the
    # background then fills the remainder up to the requested size.
    _inject_structuring(builder, account_rows, customer_by_id, device_ids, per_typology)
    _inject_rapid_movement(builder, account_rows, customer_by_id, device_ids, per_typology)
    _inject_funnel(builder, account_rows, customer_by_id, device_ids, per_typology)
    _inject_circular(builder, account_rows, customer_by_id, device_ids, per_typology)
    _inject_profile_mismatch(builder, account_rows, customer_by_id, device_ids, per_typology)
    _inject_missing_information(builder, account_rows, customer_by_id, device_ids, per_typology)

    _background(builder, accounts, customers, devices, transfers_target)

    transfers = pd.DataFrame(builder.transfers).sort_values("timestamp").reset_index(drop=True)
    scenarios = pd.DataFrame(builder.scenarios, columns=[
        "scenario_id", "typology_id", "subject_account", "subject_customer",
        "transfer_ids", "transfer_count", "difficulty", "construction",
    ])
    return AmlWorld(
        customers=customers,
        accounts=accounts,
        beneficial_owners=owners,
        devices=devices,
        transfers=transfers,
        scenarios=scenarios,
        seed=resolved_seed,
    )
