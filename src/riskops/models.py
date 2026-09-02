"""Warehouse schema.

Column types are chosen for the domain, not for convenience:

  * every money column is `BIGINT` in **minor units**, and the currency lives
    beside it. There is no `DECIMAL` and certainly no `DOUBLE` on a money path;
  * FX rates are `VARCHAR`, holding the exact decimal string that was quoted,
    so a rate is never re-rounded by a float round-trip on the way in or out;
  * every table that records a judgement also records *which version* of the
    rules, model and prompt produced it, because a metric without its version
    is not reproducible.
"""

from __future__ import annotations

RAW_EVENT_COLUMNS: dict[str, str] = {
    "raw_event_id": "VARCHAR",
    "transaction_id": "VARCHAR",
    "event_type": "VARCHAR",
    "occurred_at": "TIMESTAMP",
    "ingested_at": "TIMESTAMP",
    "amount_minor": "BIGINT",
    "currency": "VARCHAR",
    "fee_minor": "BIGINT",
    "idempotency_key": "VARCHAR",
    "wallet_id": "VARCHAR",
    "merchant_id": "VARCHAR",
    "device_id": "VARCHAR",
    "payer_country": "VARCHAR",
    "merchant_country": "VARCHAR",
    "wallet_country": "VARCHAR",
    "ip_country": "VARCHAR",
    "channel": "VARCHAR",
    # A settlement advice carries both legs. Keeping only the presentment leg
    # would make every FX reconciliation impossible to perform after the fact.
    "settlement_currency": "VARCHAR",
    "settlement_amount_minor": "BIGINT",
    "fx_quote_id": "VARCHAR",
    "fx_rate_applied": "VARCHAR",
    # What the acquirer reported for this merchant at event time. Blank when the
    # feed did not send it - which is itself a decision-blocking fact.
    "mcc_reported": "VARCHAR",
    "scenario": "VARCHAR",
    "payload_note": "VARCHAR",
    "source_batch_id": "VARCHAR",
}

TRANSACTION_COLUMNS: dict[str, str] = {
    "transaction_id": "VARCHAR",
    "created_at": "TIMESTAMP",
    "last_event_at": "TIMESTAMP",
    "payment_state": "VARCHAR",
    "wallet_id": "VARCHAR",
    "merchant_id": "VARCHAR",
    "device_id": "VARCHAR",
    "idempotency_key": "VARCHAR",
    "channel": "VARCHAR",
    # presentment side (what the payer sees)
    "presentment_currency": "VARCHAR",
    "authorized_minor": "BIGINT",
    "captured_minor": "BIGINT",
    "refunded_minor": "BIGINT",
    "charged_back_minor": "BIGINT",
    "fee_minor": "BIGINT",
    # settlement side (what the merchant receives)
    "settlement_currency": "VARCHAR",
    "settled_minor": "BIGINT",
    "quoted_fx_rate": "VARCHAR",
    "applied_fx_rate": "VARCHAR",
    "fx_quote_id": "VARCHAR",
    "fx_quoted_at": "TIMESTAMP",
    # geography and context
    "payer_country": "VARCHAR",
    "merchant_country": "VARCHAR",
    "wallet_country": "VARCHAR",
    "ip_country": "VARCHAR",
    "is_cross_border": "BOOLEAN",
    # provenance
    "scenario": "VARCHAR",
    # Ground truth for evaluation. Named "actionable", not "fraud": a duplicate
    # capture or an FX break is something risk ops must catch and is not fraud,
    # and conflating the two makes every detection metric mean nothing.
    "is_actionable_label": "BOOLEAN",
    "expected_action": "VARCHAR",
    "label_source": "VARCHAR",
    "event_count": "BIGINT",
    "rejected_event_count": "BIGINT",
    "merchant_note": "VARCHAR",
    "refresh_batch_id": "VARCHAR",
}

MERCHANT_COLUMNS: dict[str, str] = {
    "merchant_id": "VARCHAR",
    "merchant_name": "VARCHAR",
    "mcc": "VARCHAR",
    "mcc_description": "VARCHAR",
    "country": "VARCHAR",
    "settlement_currency": "VARCHAR",
    "onboarded_at": "TIMESTAMP",
    "risk_tier": "VARCHAR",
    "payout_account_id": "VARCHAR",
    "baseline_ticket_minor": "BIGINT",
    "baseline_daily_count": "BIGINT",
}

WALLET_COLUMNS: dict[str, str] = {
    "wallet_id": "VARCHAR",
    "wallet_country": "VARCHAR",
    "home_currency": "VARCHAR",
    "opened_at": "TIMESTAMP",
    "kyc_level": "VARCHAR",
    "account_id": "VARCHAR",
    "baseline_weekly_count": "BIGINT",
    "lifetime_txn_count": "BIGINT",
}

DEVICE_COLUMNS: dict[str, str] = {
    "device_id": "VARCHAR",
    "platform": "VARCHAR",
    "first_seen_at": "TIMESTAMP",
    "is_emulator": "BOOLEAN",
    "known_wallet_count": "BIGINT",
}

FX_QUOTE_COLUMNS: dict[str, str] = {
    "fx_quote_id": "VARCHAR",
    "source_currency": "VARCHAR",
    "target_currency": "VARCHAR",
    "rate": "VARCHAR",
    "quoted_at": "TIMESTAMP",
    "expires_at": "TIMESTAMP",
}

RECON_BREAK_COLUMNS: dict[str, str] = {
    "break_id": "VARCHAR",
    "transaction_id": "VARCHAR",
    "break_type": "VARCHAR",
    "expected_minor": "BIGINT",
    "observed_minor": "BIGINT",
    "difference_minor": "BIGINT",
    "currency": "VARCHAR",
    "difference_bps": "BIGINT",
    "detected_at": "TIMESTAMP",
    "detail": "VARCHAR",
}

SIGNAL_COLUMNS: dict[str, str] = {
    "signal_id": "VARCHAR",
    "transaction_id": "VARCHAR",
    "rule_id": "VARCHAR",
    "rule_version": "VARCHAR",
    "signal_family": "VARCHAR",
    "severity": "VARCHAR",
    "weight": "DOUBLE",
    "title": "VARCHAR",
    "detail": "VARCHAR",
    # Field paths this signal is grounded in, pipe-separated. The AI copilot
    # may only cite from this set, which is what makes "ungrounded claim" a
    # computable metric rather than an opinion.
    "evidence_fields": "VARCHAR",
    "fired_at": "TIMESTAMP",
}

MODEL_SCORE_COLUMNS: dict[str, str] = {
    "transaction_id": "VARCHAR",
    "model_version": "VARCHAR",
    "score": "DOUBLE",
    "band": "VARCHAR",
    "top_features": "VARCHAR",
    "scored_at": "TIMESTAMP",
}

CASE_COLUMNS: dict[str, str] = {
    "case_id": "VARCHAR",
    "transaction_id": "VARCHAR",
    "opened_at": "TIMESTAMP",
    "case_state": "VARCHAR",
    "risk_band": "VARCHAR",
    "risk_score": "DOUBLE",
    "policy_action": "VARCHAR",
    "policy_rationale": "VARCHAR",
    "primary_reason_family": "VARCHAR",
    "signal_count": "BIGINT",
    "max_severity": "VARCHAR",
    "assigned_role": "VARCHAR",
    "sla_due_at": "TIMESTAMP",
    # Two different clocks, and conflating them is why SLA dashboards lie.
    # `first_actioned_at` is when a person first did something - that is what the
    # SLA is actually about. `resolved_at` is when the case reached a terminal
    # state, which can be days later through no fault of the analyst, because it
    # waits on a merchant or a payer.
    "first_actioned_at": "TIMESTAMP",
    "resolved_at": "TIMESTAMP",
    "resolution_action": "VARCHAR",
    "resolution_reason_code": "VARCHAR",
    "handling_minutes": "DOUBLE",
    "ai_recommended_action": "VARCHAR",
    "ai_confidence": "DOUBLE",
    "ai_agreed_with_human": "BOOLEAN",
    "appeal_count": "BIGINT",
    "is_false_positive": "BOOLEAN",
    "policy_version": "VARCHAR",
    "rules_version": "VARCHAR",
    "refresh_batch_id": "VARCHAR",
}

# --- audit schema -----------------------------------------------------------

AUDIT_DDL: list[str] = [
    # Forward hash chain. `prev_hash` and `entry_hash` make a partial edit
    # detectable: rewriting entry N invalidates N and every entry after it.
    """
    CREATE TABLE IF NOT EXISTS audit.audit_log (
        seq             BIGINT,
        entry_id        VARCHAR PRIMARY KEY,
        occurred_at     TIMESTAMP,
        actor_role      VARCHAR,
        actor_id        VARCHAR,
        action          VARCHAR,
        object_type     VARCHAR,
        object_id       VARCHAR,
        summary         VARCHAR,
        payload_json    VARCHAR,
        prev_hash       VARCHAR,
        entry_hash      VARCHAR
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audit.ai_invocations (
        invocation_id       VARCHAR PRIMARY KEY,
        case_id             VARCHAR,
        transaction_id      VARCHAR,
        requested_by        VARCHAR,
        provider            VARCHAR,
        model_version       VARCHAR,
        prompt_version      VARCHAR,
        rules_version       VARCHAR,
        input_digest        VARCHAR,
        injection_verdict   VARCHAR,
        injection_patterns  VARCHAR,
        guardrail_verdict   VARCHAR,
        guardrail_reasons   VARCHAR,
        recommended_action  VARCHAR,
        confidence          DOUBLE,
        abstained           BOOLEAN,
        citation_count      BIGINT,
        unresolved_citations BIGINT,
        latency_ms          DOUBLE,
        brief_json          VARCHAR,
        created_at          TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audit.decisions (
        decision_id         VARCHAR PRIMARY KEY,
        case_id             VARCHAR,
        transaction_id      VARCHAR,
        actor_role          VARCHAR,
        actor_id            VARCHAR,
        action              VARCHAR,
        reason_code         VARCHAR,
        note                VARCHAR,
        ai_recommended_action VARCHAR,
        ai_confidence       DOUBLE,
        agreed_with_ai      BOOLEAN,
        previous_case_state VARCHAR,
        new_case_state      VARCHAR,
        decided_at          TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audit.appeals (
        appeal_id       VARCHAR PRIMARY KEY,
        case_id         VARCHAR,
        transaction_id  VARCHAR,
        filed_by_role   VARCHAR,
        filed_by        VARCHAR,
        claimant        VARCHAR,
        evidence_type   VARCHAR,
        evidence_note   VARCHAR,
        filed_at        TIMESTAMP,
        outcome         VARCHAR,
        outcome_reason_code VARCHAR,
        closed_at       TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audit.refresh_log (
        batch_id            VARCHAR PRIMARY KEY,
        started_at          TIMESTAMP,
        ended_at            TIMESTAMP,
        duration_seconds    DOUBLE,
        status              VARCHAR,
        rows_ingested       BIGINT,
        rows_core           BIGINT,
        rows_rejected       BIGINT,
        cases_opened        BIGINT,
        validation_status   VARCHAR,
        taxonomy_version    VARCHAR,
        rules_version       VARCHAR,
        model_version       VARCHAR,
        seed                BIGINT,
        error_message       VARCHAR
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audit.validation_results (
        batch_id    VARCHAR,
        check_name  VARCHAR,
        severity    VARCHAR,
        passed      BOOLEAN,
        observed    DOUBLE,
        threshold   DOUBLE,
        detail      VARCHAR,
        checked_at  TIMESTAMP
    );
    """,
]


def ddl(qualified_name: str, columns: dict[str, str], primary_key: str | None = None) -> str:
    body = ",\n    ".join(f"{name} {sql_type}" for name, sql_type in columns.items())
    if primary_key:
        body += f",\n    PRIMARY KEY ({primary_key})"
    return f"CREATE TABLE IF NOT EXISTS {qualified_name} (\n    {body}\n);"


CORE_TABLES: dict[str, tuple[dict[str, str], str | None]] = {
    "core.transactions": (TRANSACTION_COLUMNS, "transaction_id"),
    "core.merchants": (MERCHANT_COLUMNS, "merchant_id"),
    "core.wallets": (WALLET_COLUMNS, "wallet_id"),
    "core.devices": (DEVICE_COLUMNS, "device_id"),
    "core.fx_quotes": (FX_QUOTE_COLUMNS, "fx_quote_id"),
    "core.reconciliation_breaks": (RECON_BREAK_COLUMNS, "break_id"),
}

POLICY_DECISION_COLUMNS: dict[str, str] = {
    "transaction_id": "VARCHAR",
    "policy_action": "VARCHAR",
    "risk_score": "DOUBLE",
    "risk_band": "VARCHAR",
    "rule_score": "DOUBLE",
    "model_score": "DOUBLE",
    "max_severity": "VARCHAR",
    "signal_count": "BIGINT",
    "primary_reason_family": "VARCHAR",
    "policy_rationale": "VARCHAR",
    "policy_version": "VARCHAR",
}

RISK_TABLES: dict[str, tuple[dict[str, str], str | None]] = {
    "risk.signals": (SIGNAL_COLUMNS, "signal_id"),
    "risk.policy_decisions": (POLICY_DECISION_COLUMNS, "transaction_id"),
    "risk.model_scores": (MODEL_SCORE_COLUMNS, "transaction_id"),
    "risk.cases": (CASE_COLUMNS, "case_id"),
}
