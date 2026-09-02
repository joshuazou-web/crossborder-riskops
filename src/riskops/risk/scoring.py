"""The interpretable risk model.

Deliberately a logistic regression on a small set of named features, not a
gradient-boosted ensemble. Three reasons, in order of importance:

1. A risk analyst has to be able to argue with it. `contribution = coefficient x
   standardised value` is a number you can put in front of a human and defend.
2. It has to be stable under a re-run. A seeded linear fit is; a 400-tree
   ensemble on 6,000 rows drifts enough between seeds to make the evaluation
   report meaningless.
3. It is honestly weaker than the deterministic rules on this data, and the
   evaluation report says so. A model that exists to look impressive is worse
   than no model.

The model scores *residual* risk: what the context says beyond what the rules
already found. It never decides anything on its own - `policy.py` does that, and
only inside bands a human can read off a table.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from ..config import Settings
from ..generator.synth import PER_USD
from ..money import Money
from ..taxonomy import SEVERITY_ORDER

MODEL_VERSION = "1.0.0"

FEATURE_NAMES: tuple[str, ...] = (
    "signal_count",
    "max_severity_rank",
    "distinct_signal_families",
    "log_amount_usd",
    "amount_vs_wallet_median",
    "is_cross_border",
    "kyc_is_basic",
    "wallet_age_days",
    "merchant_age_days",
    "merchant_is_high_tier",
    "device_missing",
    "reconciliation_break_count",
    "distinct_countries",
    "has_refund",
    "has_chargeback",
)

# Human-readable names for the dashboard. A feature the analyst cannot read is a
# feature the analyst cannot challenge.
FEATURE_LABELS: dict[str, str] = {
    "signal_count": "number of rule signals",
    "max_severity_rank": "highest signal severity",
    "distinct_signal_families": "distinct kinds of signal",
    "log_amount_usd": "payment size",
    "amount_vs_wallet_median": "size against this wallet's usual ticket",
    "is_cross_border": "cross-border corridor",
    "kyc_is_basic": "wallet has basic KYC only",
    "wallet_age_days": "wallet age",
    "merchant_age_days": "merchant tenure",
    "merchant_is_high_tier": "merchant in a high-risk category",
    "device_missing": "no device fingerprint",
    "reconciliation_break_count": "reconciliation breaks",
    "distinct_countries": "number of distinct countries involved",
    "has_refund": "payment was refunded",
    "has_chargeback": "payment was charged back",
}


@dataclass
class TrainedModel:
    scaler: StandardScaler
    classifier: LogisticRegression
    feature_names: tuple[str, ...]
    version: str
    trained_at: str
    train_rows: int
    test_rows: int
    metrics: dict[str, float]

    def coefficients(self) -> dict[str, float]:
        return dict(zip(self.feature_names, self.classifier.coef_[0].tolist(), strict=True))


def _usd(minor: int, currency: str) -> float:
    exponent = Money(0, currency).exponent
    return float(minor) / (10 ** exponent) / float(PER_USD.get(currency, 1))


def build_features(
    transactions: pd.DataFrame,
    signals: pd.DataFrame,
    merchants: pd.DataFrame,
    wallets: pd.DataFrame,
    breaks: pd.DataFrame,
    as_of: datetime,
) -> pd.DataFrame:
    """One row per transaction, one column per named feature."""
    if transactions.empty:
        return pd.DataFrame(columns=["transaction_id", *FEATURE_NAMES])

    merchant_by_id = merchants.set_index("merchant_id").to_dict("index")
    wallet_by_id = wallets.set_index("wallet_id").to_dict("index")

    if signals.empty:
        signal_stats = pd.DataFrame(
            columns=["transaction_id", "signal_count", "max_severity_rank",
                     "distinct_signal_families"]
        )
    else:
        work = signals.copy()
        work["severity_rank"] = work["severity"].map(SEVERITY_ORDER).fillna(0)
        signal_stats = work.groupby("transaction_id").agg(
            signal_count=("signal_id", "count"),
            max_severity_rank=("severity_rank", "max"),
            distinct_signal_families=("signal_family", "nunique"),
        ).reset_index()

    break_stats = (
        breaks.groupby("transaction_id").size().rename("reconciliation_break_count").reset_index()
        if not breaks.empty
        else pd.DataFrame(columns=["transaction_id", "reconciliation_break_count"])
    )

    frame = transactions.merge(signal_stats, on="transaction_id", how="left")
    frame = frame.merge(break_stats, on="transaction_id", how="left")
    for column in ("signal_count", "max_severity_rank", "distinct_signal_families",
                   "reconciliation_break_count"):
        frame[column] = frame[column].fillna(0)

    amounts_usd = [
        _usd(int(r.captured_minor or 0), str(r.presentment_currency))
        for r in frame.itertuples()
    ]
    frame["_amount_usd"] = amounts_usd

    wallet_median = (
        frame[frame["_amount_usd"] > 0]
        .groupby("wallet_id")["_amount_usd"]
        .median()
        .to_dict()
    )

    rows = []
    as_of_ts = pd.Timestamp(as_of)
    for record in frame.to_dict("records"):
        wallet = wallet_by_id.get(str(record["wallet_id"]), {})
        merchant = merchant_by_id.get(str(record["merchant_id"]), {})
        amount_usd = float(record["_amount_usd"])
        median = float(wallet_median.get(str(record["wallet_id"]), 0.0) or 0.0)
        countries = {
            str(record.get(field) or "")
            for field in ("wallet_country", "payer_country", "ip_country", "merchant_country")
        } - {""}
        rows.append({
            "transaction_id": str(record["transaction_id"]),
            "signal_count": float(record["signal_count"]),
            "max_severity_rank": float(record["max_severity_rank"]),
            "distinct_signal_families": float(record["distinct_signal_families"]),
            "log_amount_usd": float(np.log1p(max(amount_usd, 0.0))),
            "amount_vs_wallet_median": float(min(amount_usd / median, 50.0)) if median > 0 else 1.0,
            "is_cross_border": 1.0 if record.get("is_cross_border") else 0.0,
            "kyc_is_basic": 1.0 if str(wallet.get("kyc_level", "")) == "basic" else 0.0,
            "wallet_age_days": float(
                max((as_of_ts - pd.Timestamp(wallet.get("opened_at", as_of_ts))).days, 0)
            ),
            "merchant_age_days": float(
                max((as_of_ts - pd.Timestamp(merchant.get("onboarded_at", as_of_ts))).days, 0)
            ),
            "merchant_is_high_tier": 1.0 if str(merchant.get("risk_tier", "")) == "high" else 0.0,
            "device_missing": 1.0 if not str(record.get("device_id") or "") else 0.0,
            "reconciliation_break_count": float(record["reconciliation_break_count"]),
            "distinct_countries": float(len(countries)),
            "has_refund": 1.0 if int(record.get("refunded_minor") or 0) > 0 else 0.0,
            "has_chargeback": 1.0 if int(record.get("charged_back_minor") or 0) > 0 else 0.0,
        })
    return pd.DataFrame(rows)


def deterministic_split(transaction_ids: pd.Series, test_fraction: float = 0.30) -> pd.Series:
    """Assign each transaction to train or test by hashing its id.

    Hashing rather than shuffling means the split survives a change in row
    order, a re-run, and a different machine - so a reported test metric is
    always measured on the same rows.
    """
    def bucket(txn_id: str) -> str:
        digest = hashlib.sha1(str(txn_id).encode("utf-8")).hexdigest()
        return "test" if (int(digest[:8], 16) % 1000) / 1000.0 < test_fraction else "train"

    return transaction_ids.map(bucket)


def train(
    features: pd.DataFrame,
    labels: pd.Series,
    settings: Settings,
    test_fraction: float = 0.30,
) -> TrainedModel:
    """Fit the model and measure it on a held-out split."""
    split = deterministic_split(features["transaction_id"], test_fraction)
    matrix = features[list(FEATURE_NAMES)].to_numpy(dtype=float)
    target = labels.to_numpy(dtype=int)

    train_mask = (split == "train").to_numpy()
    test_mask = ~train_mask

    scaler = StandardScaler().fit(matrix[train_mask])
    classifier = LogisticRegression(
        max_iter=2000,
        class_weight="balanced",
        random_state=settings.random_seed,
    )
    classifier.fit(scaler.transform(matrix[train_mask]), target[train_mask])

    probabilities = classifier.predict_proba(scaler.transform(matrix[test_mask]))[:, 1]
    predicted = (probabilities >= 0.5).astype(int)
    actual = target[test_mask]

    true_positive = int(((predicted == 1) & (actual == 1)).sum())
    false_positive = int(((predicted == 1) & (actual == 0)).sum())
    false_negative = int(((predicted == 0) & (actual == 1)).sum())
    true_negative = int(((predicted == 0) & (actual == 0)).sum())

    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 0.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 0.0

    metrics = {
        "test_precision": round(precision, 4),
        "test_recall": round(recall, 4),
        "test_f1": round(2 * precision * recall / (precision + recall), 4) if (precision + recall) else 0.0,
        "test_accuracy": round((true_positive + true_negative) / max(len(actual), 1), 4),
        "test_positive_rate": round(float(actual.mean()), 4) if len(actual) else 0.0,
    }

    return TrainedModel(
        scaler=scaler,
        classifier=classifier,
        feature_names=FEATURE_NAMES,
        version=MODEL_VERSION,
        trained_at=datetime.now().replace(microsecond=0).isoformat(),
        train_rows=int(train_mask.sum()),
        test_rows=int(test_mask.sum()),
        metrics=metrics,
    )


def score(model: TrainedModel, features: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Score every row and record which features drove each score."""
    if features.empty:
        return pd.DataFrame(columns=["transaction_id", "model_version", "score", "band",
                                     "top_features", "scored_at"])

    matrix = features[list(FEATURE_NAMES)].to_numpy(dtype=float)
    standardised = model.scaler.transform(matrix)
    probabilities = model.classifier.predict_proba(standardised)[:, 1]
    coefficients = model.classifier.coef_[0]

    rows = []
    for index, txn_id in enumerate(features["transaction_id"]):
        contributions = standardised[index] * coefficients
        ranked = sorted(
            zip(FEATURE_NAMES, contributions.tolist(), strict=True),
            key=lambda pair: abs(pair[1]),
            reverse=True,
        )[:4]
        rows.append({
            "transaction_id": str(txn_id),
            "model_version": model.version,
            "score": float(probabilities[index]),
            "band": band_for(float(probabilities[index])),
            "top_features": json.dumps([
                {"feature": name, "label": FEATURE_LABELS[name], "contribution": round(value, 4)}
                for name, value in ranked
            ]),
            "scored_at": now,
        })
    return pd.DataFrame(rows)


def band_for(score_value: float) -> str:
    if score_value >= 0.85:
        return "critical"
    if score_value >= 0.60:
        return "high"
    if score_value >= 0.30:
        return "medium"
    return "low"


def save(model: TrainedModel, path) -> None:
    import joblib

    joblib.dump(model, path)
    logging.getLogger(__name__).info("model saved to %s", path)


def load(path) -> TrainedModel:
    import joblib

    return joblib.load(path)
