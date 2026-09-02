"""Seeded synthetic cross-border payment generator.

Produces a world - merchants, wallets, devices, FX quotes - and then plays the
scenarios in `scenarios.py` out as raw payment events, in the shape a payment
network would emit them.

Three properties this generator is built for:

1. **Reproducible.** One `random.Random(seed)` threads through everything. The
   same seed produces byte-identical output, which is what makes an evaluation
   number citable.
2. **Honest about its own labels.** A transaction carries the scenario that
   produced it and whether it *should* have been actioned. Benign scenarios that
   deliberately trip rules exist, so precision is measurable.
3. **Not a mirror of the rule engine.** The generator writes events. It never
   writes signals, scores or cases. If a rule fires, it fired on the data.

What it does not model: partial authorisations, multi-currency baskets,
scheme representment cycles, or real network latency distributions.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd

from ..config import Settings
from ..money import Money, convert
from .scenarios import (
    AUTHORITY_NOTES,
    BENIGN_NOTES,
    INJECTION_NOTES,
    SCENARIOS,
    ScenarioSpec,
)

GENERATOR_VERSION = "1.0.0"

# Units of each currency per 1 USD. Fixed, not fetched: a portfolio project that
# silently depends on a live rate is not reproducible.
PER_USD: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "EUR": Decimal("0.92"),
    "GBP": Decimal("0.79"),
    "CNY": Decimal("7.24"),
    "HKD": Decimal("7.81"),
    "SGD": Decimal("1.345"),
    "AUD": Decimal("1.51"),
    "CAD": Decimal("1.36"),
    "MYR": Decimal("4.68"),
    "THB": Decimal("35.20"),
    "PHP": Decimal("57.30"),
    "IDR": Decimal("15800"),
    "JPY": Decimal("151.23"),
    "KRW": Decimal("1350"),
    "VND": Decimal("24500"),
    "BHD": Decimal("0.376"),
}

# country -> (currency, latitude, longitude). Coordinates are the capital, and
# exist only so "impossible travel" can be a distance over a time rather than a
# hand-wave about country codes.
COUNTRIES: dict[str, tuple[str, float, float]] = {
    "SG": ("SGD", 1.35, 103.82),
    "MY": ("MYR", 3.14, 101.69),
    "TH": ("THB", 13.76, 100.50),
    "PH": ("PHP", 14.60, 120.98),
    "ID": ("IDR", -6.21, 106.85),
    "VN": ("VND", 21.03, 105.85),
    "JP": ("JPY", 35.68, 139.69),
    "KR": ("KRW", 37.57, 126.98),
    "HK": ("HKD", 22.32, 114.17),
    "CN": ("CNY", 39.90, 116.41),
    "AU": ("AUD", -33.87, 151.21),
    "GB": ("GBP", 51.51, -0.13),
    "DE": ("EUR", 52.52, 13.40),
    "FR": ("EUR", 48.86, 2.35),
    "US": ("USD", 40.71, -74.01),
    "CA": ("CAD", 43.65, -79.38),
    "BH": ("BHD", 26.23, 50.59),
}

COUNTRY_CODES = tuple(COUNTRIES)

# Origin markets weighted toward the corridors this product is about.
PAYER_MARKETS = ("SG", "MY", "TH", "PH", "ID", "HK", "CN", "JP", "KR", "AU", "GB", "US", "VN")
MERCHANT_MARKETS = ("SG", "JP", "KR", "HK", "TH", "GB", "US", "AU", "DE", "FR", "MY", "CA")

# mcc -> (description, baseline ticket in USD major units, tier)
MCCS: dict[str, tuple[str, str, str]] = {
    "5411": ("Grocery stores and supermarkets", "34", "low"),
    "5812": ("Eating places and restaurants", "48", "low"),
    "5691": ("Clothing and accessories", "86", "low"),
    "7011": ("Lodging - hotels and resorts", "240", "medium"),
    "4722": ("Travel agencies and tour operators", "620", "medium"),
    "5732": ("Electronics stores", "310", "medium"),
    "5944": ("Jewellery and watches", "740", "high"),
    "5399": ("General merchandise", "72", "low"),
    "7995": ("Betting and gaming", "150", "high"),
    "6051": ("Quasi-cash and money transfer", "420", "high"),
}

PLATFORMS = ("ios", "android", "web", "android", "ios")
CHANNELS = ("qr_instore", "ecommerce", "in_app", "remittance")
KYC_LEVELS = ("basic", "standard", "enhanced")

# Fee schedule in percent, by merchant risk tier, plus a cross-border surcharge.
FEE_BY_TIER: dict[str, str] = {"low": "1.90", "medium": "2.60", "high": "3.40"}
CROSS_BORDER_SURCHARGE_PCT = "1.00"

FX_QUOTE_TTL_MINUTES = 15


@dataclass
class GeneratedWorld:
    merchants: pd.DataFrame
    wallets: pd.DataFrame
    devices: pd.DataFrame
    fx_quotes: pd.DataFrame
    events: pd.DataFrame
    labels: pd.DataFrame
    seed: int
    generator_version: str = GENERATOR_VERSION

    def summary(self) -> dict[str, object]:
        return {
            "merchants": len(self.merchants),
            "wallets": len(self.wallets),
            "devices": len(self.devices),
            "fx_quotes": len(self.fx_quotes),
            "raw_events": len(self.events),
            "transactions": len(self.labels),
            "seed": self.seed,
            "generator_version": self.generator_version,
        }


def fx_rate(source: str, target: str) -> Decimal:
    """Target units per one source unit, to eight decimal places."""
    if source == target:
        return Decimal("1")
    return (PER_USD[target] / PER_USD[source]).quantize(Decimal("0.00000001"))


def haversine_km(a: str, b: str) -> float:
    """Great-circle distance between two country capitals."""
    if a == b:
        return 0.0
    _, lat1, lon1 = COUNTRIES[a]
    _, lat2, lon2 = COUNTRIES[b]
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def _sid(*parts: object) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _quantise(rnd: random.Random, usd_major: str, currency: str, spread: float) -> Money:
    """A plausible amount near `usd_major` USD, expressed in `currency`."""
    base = Decimal(usd_major) * Decimal(str(round(rnd.lognormvariate(0, spread), 4)))
    in_currency = base * PER_USD[currency]
    exponent = Money(0, currency).exponent
    minor = int((in_currency * (Decimal(10) ** exponent)).to_integral_value())
    return Money(max(minor, 10 ** max(exponent - 1, 0)), currency)


class WorldBuilder:
    """Builds the static entities. Separated so tests can build a tiny world."""

    def __init__(self, settings: Settings, rnd: random.Random, start: datetime):
        self.settings = settings
        self.rnd = rnd
        self.start = start

    def merchants(self, count: int) -> pd.DataFrame:
        rnd = self.rnd
        mcc_keys = list(MCCS)
        rows = []
        # Three payout-account rings: several "unrelated" merchants sharing one
        # beneficiary account. The linkage rule has to find these.
        ring_accounts = [f"ACCT_RING_{i}" for i in range(1, 4)]
        ring_members = set()
        for index in range(count):
            mid = f"MER_{index:04d}"
            mcc = rnd.choice(mcc_keys)
            description, ticket_usd, tier = MCCS[mcc]
            country = rnd.choice(MERCHANT_MARKETS)
            currency = COUNTRIES[country][0]
            if index % 13 == 5 and len(ring_members) < 8:
                payout = ring_accounts[len(ring_members) // 4 % len(ring_accounts)]
                ring_members.add(mid)
            else:
                payout = f"ACCT_{_sid('payout', mid)}"
            rows.append({
                "merchant_id": mid,
                "merchant_name": f"{description.split()[0].title()} Merchant {index:04d}",
                "mcc": mcc,
                "mcc_description": description,
                "country": country,
                "settlement_currency": currency,
                "onboarded_at": self.start - timedelta(days=rnd.randint(20, 1500)),
                "risk_tier": tier,
                "payout_account_id": payout,
                "baseline_ticket_minor": _quantise(rnd, ticket_usd, currency, 0.05).minor_units,
                "baseline_daily_count": rnd.randint(4, 260),
            })
        return pd.DataFrame(rows)

    def wallets(self, count: int) -> pd.DataFrame:
        rnd = self.rnd
        rows = []
        # A handful of wallets share one funding account - the payer-side twin of
        # the merchant ring.
        for index in range(count):
            wid = f"WAL_{index:05d}"
            country = rnd.choice(PAYER_MARKETS)
            account = "ACCT_SHARED_PAYER_1" if index % 149 == 3 else f"ACCT_{_sid('payer', wid)}"
            rows.append({
                "wallet_id": wid,
                "wallet_country": country,
                "home_currency": COUNTRIES[country][0],
                "opened_at": self.start - timedelta(days=rnd.randint(1, 2200)),
                "kyc_level": rnd.choices(KYC_LEVELS, weights=[0.25, 0.55, 0.20])[0],
                "account_id": account,
                "baseline_weekly_count": rnd.randint(1, 9),
                "lifetime_txn_count": rnd.randint(0, 900),
            })
        return pd.DataFrame(rows)

    def devices(self, count: int) -> pd.DataFrame:
        rnd = self.rnd
        rows = []
        for index in range(count):
            did = f"DEV_{index:05d}"
            rows.append({
                "device_id": did,
                "platform": rnd.choice(PLATFORMS),
                "first_seen_at": self.start - timedelta(days=rnd.randint(0, 900)),
                "is_emulator": rnd.random() < 0.02,
                "known_wallet_count": 1,
            })
        return pd.DataFrame(rows)


def generate(settings: Settings, seed: int | None = None) -> GeneratedWorld:
    """Build one complete synthetic world. Deterministic in `seed`."""
    resolved_seed = settings.random_seed if seed is None else seed
    rnd = random.Random(resolved_seed)

    as_of = datetime.fromisoformat(settings.as_of_date)
    start = as_of - timedelta(days=settings.history_days)

    builder = WorldBuilder(settings, rnd, start)
    merchants = builder.merchants(settings.n_merchants)
    wallets = builder.wallets(settings.n_wallets)
    devices = builder.devices(max(settings.n_wallets // 2, 40))

    merchant_rows = merchants.to_dict("records")
    wallet_rows = wallets.to_dict("records")
    device_rows = devices.to_dict("records")

    payout_groups: dict[str, list[dict]] = {}
    for row in merchant_rows:
        payout_groups.setdefault(str(row["payout_account_id"]), []).append(row)
    rings = [group for group in payout_groups.values() if len(group) > 1]

    # A scenario's `share` is its share of *transactions*, not of draws. Burst
    # scenarios emit several transactions per draw, so the draw weight is divided
    # by the expected burst size - otherwise a 4% velocity scenario with bursts of
    # ten silently becomes a quarter of the population.
    weights = [s.share / ((s.burst[0] + s.burst[1]) / 2) for s in SCENARIOS]

    events: list[dict] = []
    labels: list[dict] = []
    quotes: dict[str, dict] = {}
    counter = 0

    while len(labels) < settings.n_transactions:
        scenario: ScenarioSpec = rnd.choices(SCENARIOS, weights=weights)[0]
        low, high = scenario.burst
        burst = rnd.randint(low, high)
        occurred = start + timedelta(
            seconds=rnd.randint(0, max(settings.history_days * 86400 - 7200, 3600))
        )
        wallet = rnd.choice(wallet_rows)
        if scenario.key == "wallet_country_mismatch":
            # The scenario is specifically about a thin identity paying across
            # three jurisdictions. Drawing a fully verified wallet would make the
            # story and the rule describe different things.
            thin = [w for w in wallet_rows if str(w["kyc_level"]) == "basic"]
            if thin:
                wallet = rnd.choice(thin)
        device = rnd.choice(device_rows)
        if scenario.key == "payout_account_ring" and rings:
            ring = rnd.choice(rings)
            merchant_choices = [rnd.choice(ring) for _ in range(burst)]
        else:
            merchant = rnd.choice(merchant_rows)
            merchant_choices = [merchant] * burst

        shared_key = f"IDK_{_sid('dup', counter, resolved_seed)}"

        for position in range(burst):
            counter += 1
            txn_id = f"TXN_{counter:07d}"
            merchant = merchant_choices[position]
            txn_events, label, quote = _one_transaction(
                rnd=rnd,
                settings=settings,
                scenario=scenario,
                position=position,
                burst=burst,
                txn_id=txn_id,
                wallet=wallet,
                merchant=merchant,
                device=device,
                device_pool=device_rows,
                occurred=occurred,
                shared_idempotency_key=shared_key,
            )
            events.extend(txn_events)
            labels.append(label)
            if quote is not None:
                quotes[str(quote["fx_quote_id"])] = quote
            if len(labels) >= settings.n_transactions:
                break

    events_frame = pd.DataFrame(events)
    events_frame = events_frame.sort_values(["occurred_at", "transaction_id"]).reset_index(drop=True)

    return GeneratedWorld(
        merchants=merchants,
        wallets=wallets,
        devices=devices,
        fx_quotes=pd.DataFrame(list(quotes.values())),
        events=events_frame,
        labels=pd.DataFrame(labels),
        seed=resolved_seed,
    )


def _one_transaction(
    *,
    rnd: random.Random,
    settings: Settings,
    scenario: ScenarioSpec,
    position: int,
    burst: int,
    txn_id: str,
    wallet: dict,
    merchant: dict,
    device: dict,
    device_pool: list[dict],
    occurred: datetime,
    shared_idempotency_key: str,
) -> tuple[list[dict], dict, dict | None]:
    key = scenario.key
    presentment = str(wallet["home_currency"])
    settlement = str(merchant["settlement_currency"])
    wallet_country = str(wallet["wallet_country"])
    merchant_country = str(merchant["country"])

    # --- geography -------------------------------------------------------
    payer_country = wallet_country
    ip_country = wallet_country
    if key == "normal_domestic":
        merchant_country = wallet_country
        merchant = {**merchant, "country": wallet_country}
        settlement = presentment
    elif key in ("legitimate_traveller", "impossible_travel"):
        far = [c for c in PAYER_MARKETS if haversine_km(c, wallet_country) > 6000]
        ip_country = rnd.choice(far) if far else wallet_country
        payer_country = ip_country if key == "legitimate_traveller" else wallet_country
        if key == "impossible_travel" and position % 2 == 1:
            # Bounce back home between legs. Each consecutive pair is then a
            # separate impossible hop, which is what a proxy-hopping attacker
            # actually produces - and it means every leg after the first carries
            # its own evidence.
            ip_country = wallet_country
    elif key == "wallet_country_mismatch":
        others = [c for c in PAYER_MARKETS if c != wallet_country]
        payer_country = rnd.choice(others)
        ip_country = rnd.choice([c for c in others if c != payer_country])
    elif rnd.random() < 0.08:
        ip_country = rnd.choice(PAYER_MARKETS)

    if key == "device_hopping" and position > 0:
        device = rnd.choice(device_pool)

    # --- timing ----------------------------------------------------------
    if key == "velocity_burst":
        occurred = occurred + timedelta(seconds=position * rnd.randint(20, 90))
    elif key == "device_hopping":
        occurred = occurred + timedelta(minutes=position * rnd.randint(5, 18))
    elif key == "impossible_travel":
        occurred = occurred + timedelta(minutes=position * rnd.randint(25, 70))
    elif key == "duplicate_capture":
        occurred = occurred + timedelta(seconds=position * rnd.randint(30, 400))
    elif key == "refund_chain":
        occurred = occurred + timedelta(hours=position * rnd.randint(2, 20))
    else:
        occurred = occurred + timedelta(minutes=position * rnd.randint(30, 600))

    # --- amount ----------------------------------------------------------
    baseline_usd = MCCS[str(merchant["mcc"])][1]
    if key == "high_amount_anomaly":
        amount = _quantise(rnd, str(int(baseline_usd) * rnd.randint(18, 45)), presentment, 0.10)
    elif key == "mcc_behaviour_mismatch":
        amount = _quantise(rnd, str(int(baseline_usd) * rnd.randint(25, 60)), presentment, 0.10)
    elif key == "duplicate_capture":
        # A retry duplicates the *same* payment. Drawing a fresh amount for the
        # second leg would produce two different payments that merely share a
        # key, which is not the defect being modelled.
        amount = _quantise(random.Random(shared_idempotency_key), baseline_usd, presentment, 0.55)
    else:
        amount = _quantise(rnd, baseline_usd, presentment, 0.55)

    authorized = amount
    captured = amount
    if key == "amount_mismatch":
        # A partial capture well outside any tip or adjustment tolerance.
        captured = Money(int(amount.minor_units * rnd.uniform(0.55, 0.80)), presentment)

    # --- fee -------------------------------------------------------------
    is_cross_border = presentment != settlement or wallet_country != merchant_country
    fee_pct = Decimal(FEE_BY_TIER[str(merchant["risk_tier"])])
    if is_cross_border:
        fee_pct += Decimal(CROSS_BORDER_SURCHARGE_PCT)
    fee, _ = captured.split_percent(fee_pct)
    if key == "fee_schedule_break":
        fee = Money(int(fee.minor_units * rnd.uniform(1.9, 3.4)), presentment)

    # --- FX --------------------------------------------------------------
    quoted = fx_rate(presentment, settlement)
    quoted_at = occurred - timedelta(minutes=rnd.randint(1, 6))
    expires_at = quoted_at + timedelta(minutes=FX_QUOTE_TTL_MINUTES)
    quote_id = f"FXQ_{_sid(presentment, settlement, quoted_at.isoformat())}"
    quote_row = {
        "fx_quote_id": quote_id,
        "source_currency": presentment,
        "target_currency": settlement,
        "rate": str(quoted),
        "quoted_at": quoted_at,
        "expires_at": expires_at,
    }

    applied = quoted
    if key == "fx_settlement_break":
        drift = Decimal(str(round(rnd.uniform(0.015, 0.045), 6))) * (1 if rnd.random() < 0.5 else -1)
        applied = (quoted * (Decimal(1) + drift)).quantize(Decimal("0.00000001"))

    # The quote is a rate lock: a capture inside the lock window gets the rate the
    # payer was shown. Ordinary captures land inside it; the stale-quote scenario
    # is precisely the case where the merchant captured after the lock expired and
    # the payer was charged at a price nobody quoted them.
    capture_at = occurred + timedelta(seconds=rnd.randint(30, FX_QUOTE_TTL_MINUTES * 60 - 420))
    if key == "stale_fx_quote":
        capture_at = expires_at + timedelta(minutes=rnd.randint(20, 900))

    net_presentment = captured - fee
    settled = convert(net_presentment, settlement, applied).target

    # --- free text (attacker-controlled surface) -------------------------
    if key == "injected_merchant_note":
        note = rnd.choice(INJECTION_NOTES)
    elif key == "authority_escalation_note":
        note = rnd.choice(AUTHORITY_NOTES)
    else:
        note = rnd.choice(BENIGN_NOTES)

    device_id = str(device["device_id"])
    mcc_visible = True
    if key == "insufficient_information":
        device_id = ""
        mcc_visible = False
        note = ""

    idempotency_key = (
        shared_idempotency_key
        if key == "duplicate_capture"
        else f"IDK_{_sid(txn_id, occurred.isoformat())}"
    )

    base = {
        "transaction_id": txn_id,
        "currency": presentment,
        "idempotency_key": idempotency_key,
        "wallet_id": str(wallet["wallet_id"]),
        "merchant_id": str(merchant["merchant_id"]),
        "device_id": device_id,
        "payer_country": payer_country,
        "merchant_country": merchant_country,
        "wallet_country": wallet_country,
        "ip_country": ip_country,
        "channel": rnd.choice(CHANNELS),
        "settlement_currency": settlement,
        "fx_quote_id": quote_id,
        "mcc_reported": str(merchant["mcc"]) if mcc_visible else "",
        "scenario": key,
        "source_batch_id": "",
    }

    def event(event_type: str, at: datetime, amount_minor: int | None,
              fee_minor: int | None = None, payload_note: str = "",
              settlement_amount_minor: int | None = None,
              fx_rate_applied: str = "") -> dict:
        return {
            **base,
            "raw_event_id": f"EVT_{_sid(txn_id, event_type, at.isoformat(), amount_minor)}",
            "event_type": event_type,
            "occurred_at": at,
            "ingested_at": at + timedelta(seconds=rnd.randint(1, 60)),
            "amount_minor": amount_minor,
            "fee_minor": fee_minor,
            "settlement_amount_minor": settlement_amount_minor,
            "fx_rate_applied": fx_rate_applied,
            "payload_note": payload_note,
        }

    rows = [
        event("initiate", occurred, 0),
        event("authorize", occurred + timedelta(seconds=rnd.randint(1, 25)), authorized.minor_units),
    ]

    final_state = "settled"
    refunded_minor = 0
    charged_back_minor = 0

    # A slice of ordinary traffic never completes - abandoned, declined, expired.
    outcome_roll = rnd.random()
    benign_incomplete = scenario.family == "none" and outcome_roll < 0.14

    if benign_incomplete:
        kind = rnd.choices(["decline", "expire", "authorized_only"], weights=[0.5, 0.25, 0.25])[0]
        if kind == "authorized_only":
            final_state = "authorized"
        else:
            rows.append(event(kind, occurred + timedelta(minutes=rnd.randint(1, 90)), None))
            final_state = "failed"
    else:
        rows.append(event("capture", capture_at, captured.minor_units, fee.minor_units, note))
        settle_at = capture_at + timedelta(days=rnd.randint(1, 3))
        rows.append(event(
            "settle", settle_at, net_presentment.minor_units,
            settlement_amount_minor=settled.minor_units, fx_rate_applied=str(applied),
        ))
        final_state = "settled"

        if key == "refund_chain":
            refunded_minor = captured.minor_units
            rows.append(event("refund", settle_at + timedelta(hours=rnd.randint(2, 40)), refunded_minor))
            final_state = "refunded"
        elif key == "chargeback_after_refund":
            refunded_minor = int(captured.minor_units * rnd.uniform(0.3, 0.6))
            rows.append(event("refund", settle_at + timedelta(hours=rnd.randint(2, 30)), refunded_minor))
            charged_back_minor = captured.minor_units
            rows.append(event(
                "chargeback", settle_at + timedelta(days=rnd.randint(4, 25)), charged_back_minor
            ))
            final_state = "chargeback"
        elif scenario.family == "none" and rnd.random() < 0.035:
            refunded_minor = captured.minor_units
            rows.append(event("refund", settle_at + timedelta(days=rnd.randint(1, 20)), refunded_minor))
            final_state = "refunded"
        elif scenario.family == "none" and rnd.random() < 0.012:
            charged_back_minor = captured.minor_units
            rows.append(event(
                "chargeback", settle_at + timedelta(days=rnd.randint(5, 40)), charged_back_minor
            ))
            final_state = "chargeback"

    # Feed noise: about one event in a hundred arrives out of order or replayed.
    # The pipeline must quarantine it rather than let it corrupt the ledger.
    if rnd.random() < 0.010:
        rows.append(event("settle", occurred - timedelta(minutes=5), captured.minor_units))
        base["source_batch_id"] = ""

    label = {
        "transaction_id": txn_id,
        "scenario": key,
        "is_actionable_label": scenario.is_actionable,
        "expected_action": scenario.expected_action,
        "label_source": f"generator:{GENERATOR_VERSION}",
        "expected_state": final_state,
        "presentment_currency": presentment,
        "settlement_currency": settlement,
        "quoted_fx_rate": str(quoted),
        "applied_fx_rate": str(applied),
        "fx_quote_id": quote_id,
        "fx_quoted_at": quoted_at,
        "fx_expires_at": expires_at,
        "capture_at": capture_at,
        "merchant_note": note,
        "mcc_visible": mcc_visible,
        "is_cross_border": is_cross_border,
        "refunded_minor": refunded_minor,
        "charged_back_minor": charged_back_minor,
    }
    return rows, label, quote_row
