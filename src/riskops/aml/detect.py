"""The six typology detectors. Deterministic, no model, no LLM.

Every alert this module raises is reproducible from the transfer table alone.
That is not a stylistic preference: an alert that a person will act on has to be
explainable to the person it is about, and a rule with a printed threshold can
be argued with in a way a learned score cannot.

The detectors read only the columns a real feed would carry. They never read
`scenario_id` or `scenario_role` - the generator's provenance - which is what
makes the recall figure in the evaluation report a measurement rather than a
restatement. `AmlContext.blind()` drops those columns entirely so the guarantee
is structural rather than a matter of discipline.

Each detector returns alerts keyed on a *subject*: an account, because that is
what an investigator opens. A transfer can support several alerts and an alert
always names the transfers it rests on, so evidence is traceable in both
directions.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dt_time

import pandas as pd

from . import typology as typo
from .typology import (
    CIRCULAR_FLOW,
    FUNNEL_ACCOUNT,
    MISSING_INFORMATION,
    PROFILE_MISMATCH,
    RAPID_MOVEMENT,
    STRUCTURING,
    TYPOLOGY_VERSION,
)

PROVENANCE_COLUMNS = ("scenario_id", "scenario_role")

ALERT_COLUMNS = [
    "alert_id", "typology_id", "typology_version", "severity", "subject_type",
    "subject_id", "subject_customer_id", "triggered_at", "window_start", "window_end",
    "transfer_ids", "transfer_count", "entity_ids", "total_usd_minor",
    "feature_values", "threshold_values", "explanation", "evidence_fields",
    "counter_evidence", "dedup_key",
]


def _usd(minor: int) -> str:
    return f"{minor / 100:,.0f}"


@dataclass
class AmlContext:
    """Everything the detectors read, indexed once rather than per rule."""

    transfers: pd.DataFrame
    accounts: pd.DataFrame
    customers: pd.DataFrame
    beneficial_owners: pd.DataFrame
    now: datetime

    by_payer: dict[str, list[dict]] = field(default_factory=dict, init=False)
    by_beneficiary: dict[str, list[dict]] = field(default_factory=dict, init=False)

    @classmethod
    def blind(cls, transfers: pd.DataFrame, **kwargs) -> AmlContext:
        """Build a context with the generator's provenance removed.

        The pipeline uses this. Passing the full frame would work and would
        quietly make every recall number meaningless the first time someone
        wrote `row.get("scenario_id")` inside a detector.
        """
        stripped = transfers.drop(
            columns=[c for c in PROVENANCE_COLUMNS if c in transfers.columns]
        )
        return cls(transfers=stripped, **kwargs)

    def __post_init__(self) -> None:
        leaked = [c for c in PROVENANCE_COLUMNS if c in self.transfers.columns]
        if leaked:
            raise ValueError(
                f"detector context was given generator provenance {leaked}; build it with "
                "AmlContext.blind() so recall measures detection rather than the label"
            )
        frame = self.transfers.sort_values("timestamp")
        self.rows = frame.to_dict("records")
        self.by_payer = defaultdict(list)
        self.by_beneficiary = defaultdict(list)
        for row in self.rows:
            self.by_payer[str(row["payer_account"])].append(row)
            self.by_beneficiary[str(row["beneficiary_account"])].append(row)

        self.account_by_id = self.accounts.set_index("account_id").to_dict("index")
        self.customer_by_id = self.customers.set_index("customer_id").to_dict("index")

        self.owners_by_customer: dict[str, list[dict]] = defaultdict(list)
        for row in self.beneficial_owners.to_dict("records"):
            self.owners_by_customer[str(row["customer_id"])].append(row)

    def customer_of(self, account_id: str) -> dict:
        """The customer behind an account, with its own id put back on the row.

        `set_index("customer_id")` moves that column into the index, so the
        dict it produces has every customer field except the one thing every
        caller needs. Reading it back off the account is both correct and
        cheaper than resetting the index.
        """
        account = self.account_by_id.get(account_id, {})
        customer_id = str(account.get("customer_id", ""))
        customer = self.customer_by_id.get(customer_id)
        if customer is None:
            return {}
        return {**customer, "customer_id": customer_id}

    def ownership_status(self, customer_id: str) -> str:
        owners = self.owners_by_customer.get(customer_id, [])
        if not owners:
            customer = self.customer_by_id.get(customer_id, {})
            return "not_required" if customer.get("customer_type") == "individual" else "missing"
        if any(o["verification_status"] != "verified" for o in owners):
            return "unverified"
        return "verified"



def _daily_windows(
    rows: list[dict], window: timedelta
) -> list[tuple[datetime, list[dict]]]:
    """Every window a daily batch job would evaluate, one per day new data arrived.

    This is what makes the deduplication figures mean something. A detector that
    reported only its single best window per subject would produce no duplicates
    at all, and the dedup step would be measuring nothing - but a real monitoring
    system re-evaluates its window every day, so a pattern that stays inside the
    lookback re-fires every day until it ages out. That flood is the actual
    problem alert deduplication exists to solve, so it is reproduced here rather
    than avoided.

    Only days on which a transfer actually arrived are evaluated; a window whose
    contents have not changed produces nothing new to say.
    """
    if not rows:
        return []
    ordered = sorted(rows, key=lambda r: r["timestamp"])
    out: list[tuple[datetime, list[dict]]] = []
    seen: set[tuple[str, ...]] = set()
    for date in sorted({r["timestamp"].date() for r in ordered}):
        end = datetime.combine(date, dt_time.max)
        start = end - window
        span = [r for r in ordered if start <= r["timestamp"] <= end]
        if not span:
            continue
        key = tuple(str(r["transaction_id"]) for r in span)
        if key in seen:
            continue
        seen.add(key)
        out.append((end, span))
    return out


def _alert(
    context: AmlContext,
    typology: typo.Typology,
    subject_id: str,
    transfers: list[dict],
    features: dict[str, object],
    *,
    window: tuple[datetime, datetime],
    entity_ids: list[str],
    dedup_key: str,
) -> dict:
    customer = context.customer_of(subject_id)
    transfer_ids = [str(t["transaction_id"]) for t in transfers]
    total = sum(int(t["normalized_amount_usd_minor"]) for t in transfers)
    # The alert id is derived from what the alert is about, not from a counter,
    # so two runs over the same data produce the same ids and a case opened
    # yesterday still resolves its evidence today. sha256 rather than hash():
    # Python randomises string hashing per process, so hash() would have made
    # the ids differ between two runs over identical data.
    digest = hashlib.sha256(dedup_key.encode()).hexdigest()[:10].upper()
    alert_id = f"ALT_{typology.key.upper()[:6]}_{digest}"
    return {
        "alert_id": alert_id,
        "typology_id": typology.typology_id,
        "typology_version": TYPOLOGY_VERSION,
        "severity": typology.severity,
        "subject_type": "account",
        "subject_id": subject_id,
        "subject_customer_id": str(customer.get("customer_id", "")),
        "triggered_at": context.now,
        "window_start": window[0],
        "window_end": window[1],
        "transfer_ids": "|".join(transfer_ids),
        "transfer_count": len(transfer_ids),
        "entity_ids": "|".join(sorted(set(entity_ids))),
        "total_usd_minor": total,
        "feature_values": "; ".join(f"{k}={v}" for k, v in features.items()),
        "threshold_values": "; ".join(f"{k}={v}" for k, v in typology.thresholds.items()),
        "explanation": typo.render_explanation(typology, features),
        "evidence_fields": "|".join(typology.evidence_fields),
        "counter_evidence": " | ".join(typology.counter_evidence_hints),
        "dedup_key": dedup_key,
    }


# --------------------------------------------------------------------------- #
# T01 Structuring
# --------------------------------------------------------------------------- #

def detect_structuring(context: AmlContext) -> list[dict]:
    threshold = STRUCTURING.thresholds["threshold_usd"]
    band_low = STRUCTURING.thresholds["band_low_usd"]
    window = timedelta(hours=STRUCTURING.thresholds["window_hours"])
    minimum = int(STRUCTURING.thresholds["min_transfers"])
    alerts: list[dict] = []

    for account_id, rows in context.by_payer.items():
        # Only transfers inside the band matter; a mixed history with three
        # banded transfers among two hundred ordinary ones is not the pattern,
        # so the window is walked over the banded subset.
        banded = [
            r for r in rows
            if band_low * 100 <= int(r["normalized_amount_usd_minor"]) < threshold * 100
        ]
        if len(banded) < minimum:
            continue
        for anchor, span in _daily_windows(banded, window):
            if len(span) < minimum:
                continue
            beneficiaries = {str(r["beneficiary_account"]) for r in span}
            total = sum(int(r["normalized_amount_usd_minor"]) for r in span)
            elapsed = (span[-1]["timestamp"] - span[0]["timestamp"]).total_seconds() / 3600
            features = {
                "transfer_count": len(span),
                "total_usd": _usd(total),
                "window_hours": f"{elapsed:.0f}",
                "under_threshold_count": len(span),
                "beneficiary_count": len(beneficiaries),
                "threshold": f"{threshold:,.0f}",
                "band_low": f"{band_low:,.0f}",
                "largest_usd": _usd(max(int(r["normalized_amount_usd_minor"]) for r in span)),
            }
            alerts.append(_alert(
                context, STRUCTURING, account_id, span, features,
                window=(span[0]["timestamp"], span[-1]["timestamp"]),
                entity_ids=[account_id, *beneficiaries],
                dedup_key=f"structuring:{account_id}:{anchor:%Y-%m-%d}",
            ))
    return alerts


# --------------------------------------------------------------------------- #
# T02 Rapid movement
# --------------------------------------------------------------------------- #

def detect_rapid_movement(context: AmlContext) -> list[dict]:
    max_hold = timedelta(minutes=RAPID_MOVEMENT.thresholds["max_hold_minutes"])
    min_pct = RAPID_MOVEMENT.thresholds["min_passthrough_pct"]
    min_amount = RAPID_MOVEMENT.thresholds["min_amount_usd"] * 100
    alerts: list[dict] = []

    for account_id, inbound_rows in context.by_beneficiary.items():
        outbound_rows = context.by_payer.get(account_id, [])
        if not outbound_rows:
            continue
        for inbound in inbound_rows:
            amount_in = int(inbound["normalized_amount_usd_minor"])
            if amount_in < min_amount:
                continue
            following = [
                r for r in outbound_rows
                if timedelta(0) < r["timestamp"] - inbound["timestamp"] <= max_hold
            ]
            if not following:
                continue
            # The best single onward leg, not the sum: a sum over a busy account
            # reaches 80% by accident, and the pattern being described is one
            # amount arriving and substantially the same amount leaving.
            outbound = max(following, key=lambda r: int(r["normalized_amount_usd_minor"]))
            amount_out = int(outbound["normalized_amount_usd_minor"])
            pct = amount_out / amount_in * 100 if amount_in else 0.0
            if pct < min_pct or pct > 130:
                continue
            hold = (outbound["timestamp"] - inbound["timestamp"]).total_seconds() / 60
            # Above 100% the account paid out more than this transfer brought
            # in, so it was topped up from somewhere else. That is still worth
            # looking at, but calling it "forwarded" would misdescribe it, and
            # an investigator reading "retained 0 USD" would draw the wrong
            # picture of where the balance came from.
            onward_note = (
                " More left than this transfer brought in, so the balance was funded from "
                "elsewhere; this is not a straight pass-through."
                if pct > 100 else ""
            )
            features = {
                "inbound_usd": _usd(amount_in),
                "outbound_usd": _usd(amount_out),
                "hold_minutes": f"{hold:.0f}",
                "passthrough_pct": f"{pct:.0f}",
                "retained_usd": _usd(max(0, amount_in - amount_out)),
                "destination_country": str(outbound["destination_country"]),
                "inbound_transfer_id": str(inbound["transaction_id"]),
                "outbound_transfer_id": str(outbound["transaction_id"]),
            }
            alert = _alert(
                context, RAPID_MOVEMENT, account_id, [inbound, outbound], features,
                window=(inbound["timestamp"], outbound["timestamp"]),
                entity_ids=[account_id, str(inbound["payer_account"]),
                            str(outbound["beneficiary_account"])],
                dedup_key=f"rapid:{account_id}:{inbound['transaction_id']}",
            )
            alert["explanation"] += onward_note
            alerts.append(alert)
    return alerts


# --------------------------------------------------------------------------- #
# T03 Funnel account
# --------------------------------------------------------------------------- #

def _unrelated(context: AmlContext, senders: list[dict]) -> int:
    """Senders sharing no customer, no beneficial owner and no device.

    "Unrelated" is the load-bearing word in this typology: many-to-one is
    ordinary, and it is the *absence of any connection between the senders* that
    makes the shape worth a look. Counting it explicitly also gives the
    investigator something falsifiable - they can go and find the connection.
    """
    seen_customers: set[str] = set()
    seen_owners: set[str] = set()
    seen_devices: set[str] = set()
    unrelated = 0
    for row in senders:
        customer_id = str(row["payer_customer_id"])
        owners = {o["owner_reference"] for o in context.owners_by_customer.get(customer_id, [])}
        device = str(row["device_id"])
        if customer_id in seen_customers or owners & seen_owners or device in seen_devices:
            continue
        unrelated += 1
        seen_customers.add(customer_id)
        seen_owners |= owners
        seen_devices.add(device)
    return unrelated


def detect_funnel(context: AmlContext) -> list[dict]:
    min_senders = int(FUNNEL_ACCOUNT.thresholds["min_senders"])
    window = timedelta(days=FUNNEL_ACCOUNT.thresholds["window_days"])
    min_total = FUNNEL_ACCOUNT.thresholds["min_total_usd"] * 100
    alerts: list[dict] = []

    for account_id, rows in context.by_beneficiary.items():
        if len({str(r["payer_account"]) for r in rows}) < min_senders:
            continue
        for anchor, span in _daily_windows(rows, window):
            senders = {str(r["payer_account"]) for r in span}
            total = sum(int(r["normalized_amount_usd_minor"]) for r in span)
            if len(senders) < min_senders or total < min_total:
                continue
            countries = {str(r["origin_country"]) for r in span}
            features = {
                "sender_count": len(senders),
                "sender_country_count": len(countries),
                "total_usd": _usd(total),
                "window_days": f"{(span[-1]['timestamp'] - span[0]['timestamp']).days}",
                "unrelated_sender_count": _unrelated(context, span),
                "transfer_count": len(span),
            }
            alerts.append(_alert(
                context, FUNNEL_ACCOUNT, account_id, span, features,
                window=(span[0]["timestamp"], span[-1]["timestamp"]),
                entity_ids=[account_id, *senders],
                dedup_key=f"funnel:{account_id}:{anchor:%Y-%m-%d}",
            ))
    return alerts


# --------------------------------------------------------------------------- #
# T04 Circular flow
# --------------------------------------------------------------------------- #

def detect_circular(context: AmlContext) -> list[dict]:
    """Walk forward from each transfer looking for a path back to its origin.

    Bounded depth-first search, not a graph library: the maximum hop count is
    five, so the search space is small, and keeping it explicit means the
    evidence can name every leg it walked. A cycle found here is a statement
    about these specific transfers, not about a component of a graph.
    """
    min_hops = int(CIRCULAR_FLOW.thresholds["min_hops"])
    max_hops = int(CIRCULAR_FLOW.thresholds["max_hops"])
    min_return = CIRCULAR_FLOW.thresholds["min_return_pct"]
    max_elapsed = timedelta(hours=CIRCULAR_FLOW.thresholds["max_elapsed_hours"])
    alerts: list[dict] = []
    seen_cycles: set[frozenset] = set()

    for origin, first_legs in context.by_payer.items():
        for first in first_legs:
            start_amount = int(first["normalized_amount_usd_minor"])
            if start_amount < 5_000 * 100:
                continue
            stack: list[tuple[dict, list[dict]]] = [(first, [first])]
            while stack:
                current, path = stack.pop()
                if len(path) > max_hops:
                    continue
                node = str(current["beneficiary_account"])
                elapsed = current["timestamp"] - first["timestamp"]
                if node == origin and len(path) >= min_hops:
                    returned = int(current["normalized_amount_usd_minor"])
                    pct = returned / start_amount * 100 if start_amount else 0.0
                    if pct < min_return or elapsed > max_elapsed:
                        continue
                    key = frozenset(str(p["transaction_id"]) for p in path)
                    if key in seen_cycles:
                        continue
                    seen_cycles.add(key)
                    countries = [str(p["origin_country"]) for p in path]
                    countries.append(str(path[-1]["destination_country"]))
                    features = {
                        "hop_count": len(path),
                        "total_usd": _usd(sum(int(p["normalized_amount_usd_minor"])
                                              for p in path)),
                        "country_path": " → ".join(countries),
                        "return_target": origin,
                        "return_pct": f"{pct:.0f}",
                        "elapsed_hours": f"{elapsed.total_seconds() / 3600:.0f}",
                        "cycle_account_ids": ", ".join(
                            [origin, *[str(p["beneficiary_account"]) for p in path[:-1]]]
                        ),
                    }
                    alerts.append(_alert(
                        context, CIRCULAR_FLOW, origin, path, features,
                        window=(first["timestamp"], current["timestamp"]),
                        entity_ids=[origin, *[str(p["beneficiary_account"]) for p in path]],
                        dedup_key=f"circular:{origin}:{sorted(key)[0]}",
                    ))
                    continue
                if elapsed > max_elapsed or len(path) == max_hops:
                    continue
                for nxt in context.by_payer.get(node, []):
                    gap = nxt["timestamp"] - current["timestamp"]
                    if timedelta(0) < gap <= max_elapsed:
                        stack.append((nxt, [*path, nxt]))
    return alerts


# --------------------------------------------------------------------------- #
# T05 Profile / purpose mismatch
# --------------------------------------------------------------------------- #

def detect_profile_mismatch(context: AmlContext) -> list[dict]:
    from .world import BUSINESS_TYPES, INDIVIDUAL_PURPOSES

    min_ratio = PROFILE_MISMATCH.thresholds["min_ratio"]
    window = timedelta(days=PROFILE_MISMATCH.thresholds["window_days"])
    min_actual = PROFILE_MISMATCH.thresholds["min_actual_usd"] * 100
    alerts: list[dict] = []

    for account_id, rows in context.by_payer.items():
        customer = context.customer_of(account_id)
        expected_month = float(customer.get("expected_monthly_usd") or 0)
        if expected_month <= 0:
            continue
        if customer.get("customer_type") == "business":
            on_profile = set(BUSINESS_TYPES.get(str(customer.get("industry")), ()))
        else:
            on_profile = set(INDIVIDUAL_PURPOSES)

        for anchor, span in _daily_windows(rows, window):
            total = sum(int(r["normalized_amount_usd_minor"]) for r in span)
            if total < min_actual:
                continue
            ratio = total / (expected_month * 100)
            if ratio < min_ratio:
                continue
            off = [
                str(r["declared_purpose"]) for r in span
                if str(r["declared_purpose"]) and str(r["declared_purpose"]) not in on_profile
            ]
            features = {
                "actual_usd": _usd(total),
                "expected_usd": f"{expected_month:,.0f}",
                "ratio": f"{ratio:.1f}",
                "declared_business": str(
                    customer.get("industry") or customer.get("customer_type")
                ),
                "observed_purposes": ", ".join(sorted(set(off))[:4]) or "none off-profile",
                "mismatch_count": len(off),
                "transfer_count": len(span),
                "window_days": int(PROFILE_MISMATCH.thresholds["window_days"]),
            }
            alerts.append(_alert(
                context, PROFILE_MISMATCH, account_id, span, features,
                window=(span[0]["timestamp"], span[-1]["timestamp"]),
                entity_ids=[account_id, str(customer.get("customer_id", ""))],
                dedup_key=f"profile:{account_id}:{anchor:%Y-%m-%d}",
            ))
    return alerts


# --------------------------------------------------------------------------- #
# T06 Missing information
# --------------------------------------------------------------------------- #

def detect_missing_information(context: AmlContext) -> list[dict]:
    min_affected = int(MISSING_INFORMATION.thresholds["min_affected"])
    min_usd = MISSING_INFORMATION.thresholds["min_affected_usd"] * 100
    alerts: list[dict] = []

    for account_id, rows in context.by_payer.items():
        affected = []
        missing_fields: set[str] = set()
        for row in rows:
            gaps = set()
            if str(row["beneficiary_information_status"]) in ("missing", "partial"):
                gaps.add("beneficiary_information")
            if not str(row["declared_purpose"]).strip():
                gaps.add("declared_purpose")
            if gaps:
                affected.append(row)
                missing_fields |= gaps
        if len(affected) < min_affected:
            continue
        total = sum(int(r["normalized_amount_usd_minor"]) for r in affected)
        if total < min_usd:
            continue

        customer = context.customer_of(account_id)
        ownership = context.ownership_status(str(customer.get("customer_id", "")))
        if ownership in ("unverified", "missing"):
            missing_fields.add("beneficial_ownership")
        statuses = {str(r["beneficiary_information_status"]) for r in affected}
        features = {
            "affected_count": len(affected),
            "transfer_count": len(rows),
            "affected_usd": _usd(total),
            "missing_fields": ", ".join(sorted(missing_fields)),
            "beneficiary_status": ", ".join(sorted(statuses)),
            "ownership_status": ownership,
        }
        alerts.append(_alert(
            context, MISSING_INFORMATION, account_id, affected, features,
            window=(min(r["timestamp"] for r in affected),
                    max(r["timestamp"] for r in affected)),
            entity_ids=[account_id, str(customer.get("customer_id", ""))],
            dedup_key=f"missing:{account_id}",
        ))
    return alerts


DETECTORS = (
    detect_structuring,
    detect_rapid_movement,
    detect_funnel,
    detect_circular,
    detect_profile_mismatch,
    detect_missing_information,
)


def run_all(context: AmlContext) -> pd.DataFrame:
    """Every detector over the whole world. Order is fixed, so output is stable."""
    alerts: list[dict] = []
    for detector in DETECTORS:
        alerts.extend(detector(context))
    frame = pd.DataFrame(alerts, columns=ALERT_COLUMNS)
    if frame.empty:
        return frame
    return frame.sort_values(["typology_id", "subject_id"]).reset_index(drop=True)
