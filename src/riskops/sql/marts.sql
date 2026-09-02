-- Mart layer: every dashboard metric is defined exactly once, here.
--
-- The rule the UI obeys: the Streamlit pages filter and aggregate these tables
-- and never re-implement a definition. If "SLA breach" means one thing on the
-- overview and another on the queue page, the number is worthless, so it is
-- computed once and read twice.
--
-- Money in the marts appears twice: `*_minor` in its own currency, which is the
-- truth, and `*_usd` for charting only, converted through marts.fx_reference.
-- Anything that compares, reconciles or decides uses the minor-unit column.

CREATE OR REPLACE TABLE marts.fct_transactions AS
SELECT
    t.transaction_id,
    t.created_at,
    CAST(t.created_at AS DATE)                       AS created_date,
    date_trunc('week', t.created_at)                 AS created_week,
    t.payment_state,
    t.channel,
    t.wallet_id,
    t.merchant_id,
    t.device_id,
    t.presentment_currency,
    t.settlement_currency,
    t.authorized_minor,
    t.captured_minor,
    t.refunded_minor,
    t.charged_back_minor,
    t.fee_minor,
    t.settled_minor,
    -- Display only. Never compare two of these to decide anything.
    t.captured_minor / pow(10, fx.exponent) / fx.per_usd            AS captured_usd,
    t.fee_minor      / pow(10, fx.exponent) / fx.per_usd            AS fee_usd,
    t.wallet_country,
    t.payer_country,
    t.ip_country,
    t.merchant_country,
    t.wallet_country || ' -> ' || t.merchant_country                AS corridor,
    t.is_cross_border,
    t.quoted_fx_rate,
    t.applied_fx_rate,
    t.scenario,
    t.is_actionable_label,
    t.expected_action,
    t.rejected_event_count,
    t.merchant_note,
    m.mcc,
    m.mcc_description,
    m.risk_tier                                       AS merchant_risk_tier,
    m.payout_account_id,
    w.kyc_level,
    w.lifetime_txn_count,
    coalesce(s.signal_count, 0)                       AS signal_count,
    coalesce(s.max_severity, 'none')                  AS max_severity,
    coalesce(b.break_count, 0)                        AS reconciliation_break_count,
    coalesce(p.policy_action, 'auto_release')         AS policy_action,
    coalesce(p.risk_score, 0.0)                       AS risk_score,
    coalesce(p.risk_band, 'low')                      AS risk_band,
    coalesce(ms.score, 0.0)                           AS model_score,
    c.case_id,
    c.case_state,
    c.resolution_action
FROM core.transactions t
LEFT JOIN marts.fx_reference fx ON fx.currency = t.presentment_currency
LEFT JOIN core.merchants m      ON m.merchant_id = t.merchant_id
LEFT JOIN core.wallets w        ON w.wallet_id  = t.wallet_id
LEFT JOIN (
    SELECT transaction_id,
           count(*) AS signal_count,
           max(CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                             WHEN 'medium' THEN 2 ELSE 1 END) AS severity_rank,
           CASE max(CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                                  WHEN 'medium' THEN 2 ELSE 1 END)
                WHEN 4 THEN 'critical' WHEN 3 THEN 'high'
                WHEN 2 THEN 'medium' ELSE 'low' END AS max_severity
    FROM risk.signals GROUP BY transaction_id
) s ON s.transaction_id = t.transaction_id
LEFT JOIN (
    SELECT transaction_id, count(*) AS break_count
    FROM core.reconciliation_breaks GROUP BY transaction_id
) b ON b.transaction_id = t.transaction_id
LEFT JOIN risk.policy_decisions p ON p.transaction_id = t.transaction_id
LEFT JOIN risk.model_scores ms    ON ms.transaction_id = t.transaction_id
LEFT JOIN risk.cases c            ON c.transaction_id  = t.transaction_id;


CREATE OR REPLACE TABLE marts.fct_cases AS
SELECT
    c.case_id,
    c.transaction_id,
    c.opened_at,
    CAST(c.opened_at AS DATE)          AS opened_date,
    c.case_state,
    c.risk_band,
    c.risk_score,
    c.policy_action,
    c.policy_rationale,
    c.primary_reason_family,
    c.signal_count,
    c.max_severity,
    c.assigned_role,
    c.sla_due_at,
    c.first_actioned_at,
    c.resolved_at,
    c.resolution_action,
    c.resolution_reason_code,
    c.handling_minutes,
    c.ai_recommended_action,
    c.ai_confidence,
    c.ai_agreed_with_human,
    c.appeal_count,
    c.is_false_positive,
    -- One definition of "breached", used everywhere it appears. The clock stops
    -- when a person first acts, not when the case finally closes: a case waiting
    -- on a merchant's shipping record is not an analyst being slow.
    CASE
        WHEN c.first_actioned_at IS NOT NULL AND c.first_actioned_at > c.sla_due_at THEN TRUE
        WHEN c.first_actioned_at IS NULL
             AND c.sla_due_at < (SELECT max(created_at) FROM core.transactions) THEN TRUE
        ELSE FALSE
    END                                 AS sla_breached,
    -- Open means "still needs work", not "not yet closed". An escalated case or
    -- one waiting on evidence is open; a released one is not.
    CASE WHEN c.case_state IN ('resolved_released', 'resolved_held', 'closed_false_positive')
         THEN FALSE ELSE TRUE END       AS is_open,
    t.presentment_currency,
    t.captured_minor,
    t.captured_minor / pow(10, fx.exponent) / fx.per_usd AS captured_usd,
    t.wallet_country,
    t.merchant_country,
    t.wallet_country || ' -> ' || t.merchant_country     AS corridor,
    t.scenario,
    t.is_actionable_label,
    t.expected_action,
    m.mcc_description,
    m.risk_tier                          AS merchant_risk_tier
FROM risk.cases c
JOIN core.transactions t   ON t.transaction_id = c.transaction_id
LEFT JOIN marts.fx_reference fx ON fx.currency = t.presentment_currency
LEFT JOIN core.merchants m ON m.merchant_id = t.merchant_id;


CREATE OR REPLACE TABLE marts.kpi_overview AS
SELECT
    (SELECT count(*) FROM core.transactions)                                 AS transactions,
    (SELECT count(*) FROM core.transactions WHERE is_cross_border)           AS cross_border_transactions,
    (SELECT count(*) FROM core.payment_events)                               AS payment_events,
    (SELECT count(*) FROM core.payment_events WHERE NOT accepted)            AS quarantined_events,
    (SELECT count(*) FROM risk.signals)                                      AS signals,
    (SELECT count(*) FROM core.reconciliation_breaks)                        AS reconciliation_breaks,
    (SELECT count(*) FROM risk.cases)                                        AS cases,
    (SELECT count(*) FROM risk.cases WHERE resolved_at IS NULL)              AS open_cases,
    (SELECT count(*) FROM risk.cases WHERE is_false_positive)                AS overturned_holds,
    (SELECT count(*) FROM audit.appeals)                                     AS appeals,
    (SELECT count(*) FROM audit.decisions)                                   AS decisions,
    (SELECT count(*) FROM audit.ai_invocations)                              AS ai_briefs,
    (SELECT count(*) FROM audit.audit_log)                                   AS audit_entries,
    (SELECT round(100.0 * count(*) FILTER (WHERE policy_action = 'auto_release') / nullif(count(*), 0), 2)
       FROM risk.policy_decisions)                                           AS auto_release_pct,
    (SELECT round(100.0 * count(*) FILTER (WHERE policy_action <> 'auto_release') / nullif(count(*), 0), 2)
       FROM risk.policy_decisions)                                           AS manual_review_pct,
    (SELECT round(median(handling_minutes), 1) FROM risk.cases WHERE handling_minutes IS NOT NULL)
                                                                             AS median_handling_minutes,
    (SELECT round(quantile_cont(handling_minutes, 0.90), 1) FROM risk.cases WHERE handling_minutes IS NOT NULL)
                                                                             AS p90_handling_minutes;


CREATE OR REPLACE TABLE marts.kpi_daily_volume AS
SELECT
    created_date,
    count(*)                                                  AS transactions,
    count(*) FILTER (WHERE is_cross_border)                   AS cross_border,
    round(sum(captured_usd), 2)                               AS captured_usd,
    count(*) FILTER (WHERE policy_action <> 'auto_release')   AS routed_to_review,
    count(*) FILTER (WHERE case_id IS NOT NULL)               AS cases_opened
FROM marts.fct_transactions
GROUP BY created_date
ORDER BY created_date;


CREATE OR REPLACE TABLE marts.kpi_state_mix AS
SELECT payment_state,
       count(*) AS transactions,
       round(100.0 * count(*) / (SELECT count(*) FROM marts.fct_transactions), 2) AS share_pct
FROM marts.fct_transactions
GROUP BY payment_state
ORDER BY transactions DESC;


CREATE OR REPLACE TABLE marts.kpi_corridor AS
SELECT corridor,
       wallet_country,
       merchant_country,
       (wallet_country <> merchant_country) AS is_cross_border,
       count(*)                                                AS transactions,
       round(sum(captured_usd), 2)                             AS captured_usd,
       count(*) FILTER (WHERE case_id IS NOT NULL)             AS cases,
       round(100.0 * count(*) FILTER (WHERE case_id IS NOT NULL) / count(*), 2) AS case_rate_pct
FROM marts.fct_transactions
GROUP BY corridor, wallet_country, merchant_country, is_cross_border
HAVING count(*) >= 5
ORDER BY transactions DESC;


CREATE OR REPLACE TABLE marts.kpi_signal_frequency AS
SELECT s.rule_id,
       s.signal_family,
       s.severity,
       count(*)                                                       AS fired,
       count(DISTINCT s.transaction_id)                               AS transactions,
       round(100.0 * count(*) FILTER (WHERE t.is_actionable_label) / nullif(count(*), 0), 2)
                                                                      AS precision_pct,
       any_value(s.title)                                             AS title
FROM risk.signals s
JOIN core.transactions t ON t.transaction_id = s.transaction_id
GROUP BY s.rule_id, s.signal_family, s.severity
ORDER BY fired DESC;


CREATE OR REPLACE TABLE marts.kpi_case_queue AS
SELECT case_id, transaction_id, opened_at, case_state, risk_band, risk_score,
       policy_action, primary_reason_family, signal_count, max_severity,
       assigned_role, sla_due_at, sla_breached, corridor, captured_usd,
       ai_recommended_action, ai_confidence, appeal_count
FROM marts.fct_cases
WHERE is_open
ORDER BY
    CASE risk_band WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
    sla_due_at;


CREATE OR REPLACE TABLE marts.kpi_case_outcomes AS
SELECT coalesce(nullif(resolution_action, ''), 'unresolved') AS resolution_action,
       coalesce(nullif(resolution_reason_code, ''), 'n/a')   AS reason_code,
       count(*)                                             AS cases,
       round(median(handling_minutes), 1)                   AS median_handling_minutes,
       count(*) FILTER (WHERE is_false_positive)            AS overturned
FROM marts.fct_cases
GROUP BY 1, 2
ORDER BY cases DESC;


CREATE OR REPLACE TABLE marts.kpi_sla AS
SELECT risk_band,
       count(*)                                                   AS cases,
       count(*) FILTER (WHERE sla_breached)                       AS breached,
       round(100.0 * count(*) FILTER (WHERE sla_breached) / nullif(count(*), 0), 2)
                                                                  AS breach_pct,
       round(median(handling_minutes), 1)                         AS median_handling_minutes,
       round(quantile_cont(handling_minutes, 0.90), 1)            AS p90_handling_minutes
FROM marts.fct_cases
GROUP BY risk_band
ORDER BY CASE risk_band WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2 ELSE 3 END;


-- AI agreement. Read this next to the caveat it carries: the human side of the
-- comparison is simulated analyst behaviour, so this measures consistency
-- between two synthetic processes, not human trust in a model.
CREATE OR REPLACE TABLE marts.kpi_ai_agreement AS
SELECT coalesce(nullif(ai_recommended_action, ''), 'no_brief') AS ai_recommended_action,
       coalesce(nullif(resolution_action, ''), 'unresolved')   AS human_action,
       count(*)                                               AS cases,
       round(avg(ai_confidence), 3)                           AS mean_ai_confidence
FROM marts.fct_cases
GROUP BY 1, 2
ORDER BY cases DESC;


CREATE OR REPLACE TABLE marts.kpi_ai_guardrails AS
SELECT provider,
       guardrail_verdict,
       injection_verdict,
       count(*)                                        AS invocations,
       sum(CASE WHEN abstained THEN 1 ELSE 0 END)      AS abstentions,
       sum(unresolved_citations)                       AS unresolved_citations,
       round(avg(confidence), 3)                       AS mean_confidence,
       round(avg(citation_count), 2)                   AS mean_citations,
       round(avg(latency_ms), 1)                       AS mean_latency_ms
FROM audit.ai_invocations
GROUP BY provider, guardrail_verdict, injection_verdict
ORDER BY invocations DESC;


CREATE OR REPLACE TABLE marts.kpi_appeals AS
SELECT coalesce(nullif(a.outcome, ''), 'open') AS outcome,
       a.evidence_type,
       count(*)                                AS appeals,
       round(median(date_diff('hour', a.filed_at, a.closed_at)), 1) AS median_hours_to_close
FROM audit.appeals a
GROUP BY 1, 2
ORDER BY appeals DESC;


CREATE OR REPLACE TABLE marts.kpi_reconciliation AS
SELECT break_type,
       count(*)                        AS breaks,
       count(DISTINCT transaction_id)  AS transactions,
       round(median(abs(difference_bps)), 1) AS median_abs_bps,
       max(abs(difference_bps))        AS max_abs_bps
FROM core.reconciliation_breaks
GROUP BY break_type
ORDER BY breaks DESC;


CREATE OR REPLACE TABLE marts.kpi_data_quality AS
SELECT
    (SELECT count(*) FROM core.payment_events)                        AS events,
    (SELECT count(*) FROM core.payment_events WHERE NOT accepted)     AS quarantined_events,
    (SELECT round(100.0 * count(*) FILTER (WHERE NOT accepted) / nullif(count(*), 0), 3)
       FROM core.payment_events)                                      AS quarantine_pct,
    (SELECT count(*) FROM core.transactions WHERE device_id = '')     AS missing_device,
    (SELECT round(100.0 * count(*) FILTER (WHERE device_id = '') / nullif(count(*), 0), 2)
       FROM core.transactions)                                        AS missing_device_pct,
    (SELECT count(*) FROM audit.validation_results WHERE NOT passed AND severity = 'error')
                                                                      AS failed_error_checks,
    (SELECT count(*) FROM audit.validation_results WHERE NOT passed AND severity = 'warning')
                                                                      AS failed_warning_checks,
    (SELECT count(*) FROM audit.validation_results)                   AS total_checks;


CREATE OR REPLACE TABLE marts.kpi_policy_mix AS
SELECT policy_action,
       risk_band,
       count(*) AS transactions,
       round(100.0 * count(*) / (SELECT count(*) FROM risk.policy_decisions), 2) AS share_pct
FROM risk.policy_decisions
GROUP BY policy_action, risk_band
ORDER BY transactions DESC;
