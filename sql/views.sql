-- ============================================================================
-- PROJECT SENTINEL — BigQuery Analytical Views (§6.3)
-- Dataset: sentinel
-- All column names formatted in business language per §6.3 specification.
-- Accurate grain for all seven views.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. v_autonomy_rate: Daily count auto-healed vs escalated, and % autonomous
-- Grain: One row per decision date.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `sentinel.v_autonomy_rate` AS
WITH policy_events AS (
    SELECT
        DATE(created_at) AS decision_date,
        COUNTIF(okf_outcome = 'AUTO_HEAL' OR okf_outcome = 'AUTO_HEALED') AS auto_healed_count,
        COUNTIF(okf_outcome IN ('REQUIRE_HITL', 'BLOCKED', 'HITL_APPROVED')) AS escalated_count,
        COUNT(1) AS total_decisions
    FROM `sentinel.audit_ledger`
    WHERE phase = 'POLICY'
    GROUP BY decision_date
)
SELECT
    decision_date,
    auto_healed_count,
    escalated_count,
    total_decisions,
    ROUND(IEEE_DIVIDE(auto_healed_count * 100.0, NULLIF(total_decisions, 0)), 2) AS autonomy_rate_pct
FROM policy_events;

-- ----------------------------------------------------------------------------
-- 2. v_exposure_avoided: Cumulative USD SLA exposure avoided by healing actions
-- Grain: One row per executed healing action.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `sentinel.v_exposure_avoided` AS
WITH baseline_scenarios AS (
    SELECT
        deviation_id,
        MAX(CASE WHEN label = 'STATUS_QUO' THEN total_exposure_usd END) AS status_quo_exposure_usd,
        MAX(CASE WHEN label = 'STATUS_QUO' THEN sla_penalty_usd END) AS status_quo_sla_penalty_usd
    FROM `sentinel.scenario_library`
    GROUP BY deviation_id
),
action_scenarios AS (
    SELECT
        h.action_id,
        h.deviation_id,
        h.sku_id,
        h.option_id,
        h.mode AS mitigation_mode,
        h.qty AS units_mitigated,
        h.cost_usd AS action_cost_usd,
        h.status AS action_status,
        h.executed_at,
        COALESCE(s.total_exposure_usd, h.cost_usd) AS action_exposure_usd,
        COALESCE(b.status_quo_exposure_usd, h.cost_usd * 2) AS baseline_exposure_usd
    FROM `sentinel.healing_actions` h
    LEFT JOIN `sentinel.scenario_library` s
        ON h.deviation_id = s.deviation_id AND h.mode = s.label
    LEFT JOIN baseline_scenarios b
        ON h.deviation_id = b.deviation_id
)
SELECT
    action_id,
    deviation_id,
    sku_id,
    mitigation_mode,
    units_mitigated,
    action_cost_usd,
    action_status,
    executed_at,
    GREATEST(NUMERIC '0.00', baseline_exposure_usd - action_exposure_usd) AS exposure_avoided_usd,
    SUM(GREATEST(NUMERIC '0.00', baseline_exposure_usd - action_exposure_usd)) OVER (
        ORDER BY executed_at, action_id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS cumulative_exposure_avoided_usd
FROM action_scenarios;

-- ----------------------------------------------------------------------------
-- 3. v_scenario_impact_map: Every scenario as (total_cost_usd, days_to_coverage, exposure)
-- Grain: One row per evaluated scenario in scenario_library.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `sentinel.v_scenario_impact_map` AS
SELECT
    s.scenario_id,
    s.deviation_id,
    d.deviation_type,
    d.sku_id,
    sm.sku_name,
    d.dc_id,
    s.label AS scenario_label,
    s.total_cost_usd,
    s.days_to_coverage,
    s.sla_penalty_usd,
    s.total_exposure_usd,
    s.feasible,
    s.solver_status,
    s.result_sha256,
    s.created_at
FROM `sentinel.scenario_library` s
LEFT JOIN `sentinel.deviations` d ON s.deviation_id = d.deviation_id
LEFT JOIN `sentinel.sku_master` sm ON d.sku_id = sm.sku_id;

-- ----------------------------------------------------------------------------
-- 4. v_spend_velocity_24h: Rolling 24h spend per active dimension against its ceiling
-- Grain: One row per active spend dimension key in the rolling 24h window.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `sentinel.v_spend_velocity_24h` AS
WITH recent_spend AS (
    SELECT
        tenant,
        supplier_id,
        sku_id,
        cost_center,
        workflow_root_id,
        amount_usd
    FROM `sentinel.spend_transactions`
    WHERE transaction_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
),
active_dims AS (
    SELECT
        'TENANT' AS dimension,
        tenant AS dimension_key,
        SUM(amount_usd) AS spent_24h_usd
    FROM recent_spend
    GROUP BY tenant
    UNION ALL
    SELECT
        'SUPPLIER' AS dimension,
        supplier_id AS dimension_key,
        SUM(amount_usd) AS spent_24h_usd
    FROM recent_spend
    GROUP BY supplier_id
    UNION ALL
    SELECT
        'SKU' AS dimension,
        sku_id AS dimension_key,
        SUM(amount_usd) AS spent_24h_usd
    FROM recent_spend
    GROUP BY sku_id
    UNION ALL
    SELECT
        'COST_CENTER' AS dimension,
        cost_center AS dimension_key,
        SUM(amount_usd) AS spent_24h_usd
    FROM recent_spend
    GROUP BY cost_center
    UNION ALL
    SELECT
        'WORKFLOW_ROOT' AS dimension,
        workflow_root_id AS dimension_key,
        SUM(amount_usd) AS spent_24h_usd
    FROM recent_spend
    GROUP BY workflow_root_id
),
policy_ceilings AS (
    SELECT
        dimension,
        ceiling_usd
    FROM `sentinel.okf_policy`
    WHERE rule_name = 'VELOCITY_CAP'
)
SELECT
    a.dimension,
    a.dimension_key,
    a.spent_24h_usd,
    COALESCE(p.ceiling_usd, NUMERIC '5000.00') AS ceiling_usd,
    GREATEST(NUMERIC '0.00', COALESCE(p.ceiling_usd, NUMERIC '5000.00') - a.spent_24h_usd) AS remaining_budget_usd,
    ROUND(IEEE_DIVIDE(a.spent_24h_usd * 100.0, NULLIF(p.ceiling_usd, 0)), 2) AS utilization_pct,
    CURRENT_TIMESTAMP() AS as_of_time
FROM active_dims a
LEFT JOIN policy_ceilings p ON a.dimension = p.dimension;

-- ----------------------------------------------------------------------------
-- 5. v_governance_events: Policy rule trips and governance decisions over time
-- Grain: One row per policy evaluation / approval event.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `sentinel.v_governance_events` AS
SELECT
    record_id AS event_id,
    deviation_id,
    workflow_root_id,
    phase AS governance_phase,
    CASE
        WHEN phase = 'POLICY' AND (okf_outcome = 'AUTO_HEAL' OR okf_outcome = 'AUTO_HEALED') THEN 'POLICY_AUTO_HEAL_APPROVAL'
        WHEN phase = 'POLICY' AND okf_outcome = 'REQUIRE_HITL' THEN 'POLICY_HITL_ESCALATION'
        WHEN phase = 'POLICY' AND okf_outcome = 'BLOCKED' THEN 'POLICY_KILL_SWITCH_TRIP'
        WHEN phase = 'APPROVAL' THEN 'HUMAN_OPERATOR_SIGNATURE'
        WHEN phase = 'SANITIZE' THEN 'INGRESS_GUARDRAIL_INTERCEPTION'
        ELSE CONCAT('GOVERNANCE_', phase)
    END AS rule_name,
    COALESCE(okf_outcome, phase) AS policy_outcome,
    created_at AS event_timestamp,
    DATE(created_at) AS event_date
FROM `sentinel.audit_ledger`
WHERE phase IN ('POLICY', 'APPROVAL');

-- ----------------------------------------------------------------------------
-- 6. v_supplier_reliability_trend: On-time rate by supplier over 90-day window
-- Grain: One row per supplier in supplier_master (12 rows).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `sentinel.v_supplier_reliability_trend` AS
WITH delivery_metrics AS (
    SELECT
        supplier_id,
        COUNT(delivery_id) AS deliveries_evaluated,
        COUNTIF(actual_delivery_date <= promised_date) AS on_time_deliveries,
        COUNTIF(actual_delivery_date > promised_date) AS late_deliveries,
        ROUND(IEEE_DIVIDE(COUNTIF(actual_delivery_date <= promised_date) * 100.0, COUNT(delivery_id)), 2) AS on_time_delivery_rate_pct,
        ROUND(AVG(DATE_DIFF(actual_delivery_date, promised_date, DAY)), 2) AS avg_lead_time_drift_days,
        ROUND(IEEE_DIVIDE(SUM(invoiced_price_usd - quoted_price_usd) * 100.0, NULLIF(SUM(quoted_price_usd), 0)), 2) AS quote_variance_rate_pct,
        ROUND(IEEE_DIVIDE(SUM(quantity_received) * 100.0, NULLIF(SUM(quantity_ordered), 0)), 2) AS quantity_fulfillment_rate_pct
    FROM `sentinel.delivery_history`
    WHERE promised_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
    GROUP BY supplier_id
)
SELECT
    s.supplier_id,
    s.supplier_name,
    s.tier AS supplier_tier,
    s.cost_center,
    COALESCE(d.deliveries_evaluated, 0) AS deliveries_evaluated,
    COALESCE(d.on_time_deliveries, 0) AS on_time_deliveries,
    COALESCE(d.late_deliveries, 0) AS late_deliveries,
    COALESCE(d.on_time_delivery_rate_pct, 0.0) AS on_time_delivery_rate_pct,
    COALESCE(d.avg_lead_time_drift_days, 0.0) AS avg_lead_time_drift_days,
    COALESCE(d.quote_variance_rate_pct, 0.0) AS quote_variance_rate_pct,
    COALESCE(d.quantity_fulfillment_rate_pct, 0.0) AS quantity_fulfillment_rate_pct,
    90 AS evaluation_window_days,
    CASE
        WHEN COALESCE(d.on_time_delivery_rate_pct, 0.0) >= 90.0 THEN 'PREMIER'
        WHEN COALESCE(d.on_time_delivery_rate_pct, 0.0) >= 75.0 THEN 'ACCEPTABLE'
        ELSE 'HIGH_RISK'
    END AS reliability_tier
FROM `sentinel.supplier_master` s
LEFT JOIN delivery_metrics d ON s.supplier_id = d.supplier_id;

-- ----------------------------------------------------------------------------
-- 7. v_feed_quality: Inbound ASN raw-feed defect detection and anomaly classification
-- Grain: One row per feed date and detected defect class.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `sentinel.v_feed_quality` AS
WITH classified_landing AS (
    SELECT
        DATE(received_at) AS feed_date,
        asn_id,
        sku_id,
        dc_id,
        supplier_id,
        quantity,
        uom,
        ship_date,
        eta_date,
        CASE
            WHEN ship_date LIKE '%/%' OR eta_date LIKE '%/%' THEN 'DATE_FORMAT_NON_ISO'
            WHEN uom = 'CASE' THEN 'UOM_MISMATCH_CASE_TO_EA'
            WHEN asn_id IN (
                SELECT asn_id FROM `sentinel_raw.asn_landing` GROUP BY asn_id HAVING COUNT(1) > 1
            ) THEN 'DUPLICATE_ASN_CONFLICT'
            ELSE 'CLEAN_RECORD'
        END AS detected_defect_class
    FROM `sentinel_raw.asn_landing`
),
aggregated_feed AS (
    SELECT
        feed_date,
        detected_defect_class,
        COUNT(1) AS raw_records_scanned,
        COUNTIF(detected_defect_class IN ('DATE_FORMAT_NON_ISO', 'UOM_MISMATCH_CASE_TO_EA')) AS format_anomaly_count,
        COUNTIF(detected_defect_class = 'DUPLICATE_ASN_CONFLICT') AS duplicate_conflict_count
    FROM classified_landing
    GROUP BY feed_date, detected_defect_class
)
SELECT
    feed_date,
    detected_defect_class,
    raw_records_scanned,
    format_anomaly_count,
    duplicate_conflict_count,
    ROUND(IEEE_DIVIDE((format_anomaly_count + duplicate_conflict_count) * 100.0, NULLIF(raw_records_scanned, 0)), 2) AS anomaly_rate_pct,
    CASE
        WHEN duplicate_conflict_count > 0 THEN 'DUPLICATE_CONFLICT'
        WHEN format_anomaly_count > 0 THEN 'ANOMALY_DETECTED'
        ELSE 'CLEAN'
    END AS feed_quality_rating
FROM aggregated_feed;
