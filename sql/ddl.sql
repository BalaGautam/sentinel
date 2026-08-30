-- ============================================================================
-- PROJECT SENTINEL — BigQuery DDL (§6.1, §6.2)
-- Datasets: sentinel, sentinel_raw, sentinel_clean
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. OPERATIONAL TABLES (sentinel dataset)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS `sentinel.sku_master` (
    sku_id STRING NOT NULL,
    sku_name STRING,
    category STRING,
    unit_value_usd NUMERIC,
    criticality STRING,
    units_per_case INT64,
    lead_time_buffer_days INT64,
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `sentinel.supplier_master` (
    supplier_id STRING NOT NULL,
    supplier_name STRING,
    tier STRING,
    contract_terms STRING,
    cost_center STRING,
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `sentinel.supply_options` (
    option_id STRING NOT NULL,
    supplier_id STRING NOT NULL,
    sku_id STRING NOT NULL,
    mode STRING NOT NULL,
    unit_price_usd NUMERIC NOT NULL,
    moq INT64 NOT NULL,
    max_qty INT64 NOT NULL,
    lead_time_days INT64 NOT NULL,
    fixed_fee_usd NUMERIC NOT NULL,
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `sentinel.inventory_position` (
    sku_id STRING NOT NULL,
    dc_id STRING NOT NULL,
    on_hand_units INT64 NOT NULL,
    in_transit_units INT64 NOT NULL,
    safety_stock_units INT64 NOT NULL,
    reorder_point_units INT64,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS `sentinel.customer_orders` (
    order_id STRING NOT NULL,
    customer_name STRING,
    sku_id STRING NOT NULL,
    dc_id STRING NOT NULL,
    promise_date DATE NOT NULL,
    order_qty INT64 NOT NULL,
    tier STRING NOT NULL,
    sla_penalty_rate_usd_per_day NUMERIC NOT NULL,
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `sentinel.delivery_history` (
    delivery_id STRING NOT NULL,
    supplier_id STRING NOT NULL,
    po_id STRING,
    sku_id STRING,
    promised_date DATE NOT NULL,
    actual_delivery_date DATE NOT NULL,
    quoted_price_usd NUMERIC NOT NULL,
    invoiced_price_usd NUMERIC NOT NULL,
    quantity_ordered INT64 NOT NULL,
    quantity_received INT64 NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS `sentinel.demand_signals` (
    signal_id STRING NOT NULL,
    sku_id STRING NOT NULL,
    dc_id STRING NOT NULL,
    signal_date DATE NOT NULL,
    forecast_units INT64 NOT NULL,
    actual_demand_units INT64 NOT NULL,
    deviation_magnitude INT64 NOT NULL,
    created_at TIMESTAMP NOT NULL
);

-- ----------------------------------------------------------------------------
-- 2. AGENTIC TABLES (sentinel dataset)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS `sentinel.deviations` (
    deviation_id STRING NOT NULL,
    deviation_type STRING NOT NULL,
    sku_id STRING NOT NULL,
    dc_id STRING NOT NULL,
    magnitude_units INT64 NOT NULL,
    delay_days INT64 NOT NULL,
    source_system STRING NOT NULL,
    raw_note STRING,
    detected_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS `sentinel.scenario_library` (
    scenario_id STRING NOT NULL,
    deviation_id STRING NOT NULL,
    label STRING NOT NULL,
    selected_options_json STRING,
    total_cost_usd NUMERIC NOT NULL,
    sla_penalty_usd NUMERIC NOT NULL,
    total_exposure_usd NUMERIC NOT NULL,
    days_to_coverage INT64 NOT NULL,
    feasible BOOL NOT NULL,
    solver_status STRING NOT NULL,
    result_sha256 STRING NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS `sentinel.healing_actions` (
    action_id STRING NOT NULL,
    deviation_id STRING NOT NULL,
    sku_id STRING NOT NULL,
    option_id STRING,
    mode STRING NOT NULL,
    qty INT64 NOT NULL,
    cost_usd NUMERIC NOT NULL,
    status STRING NOT NULL,
    idempotency_key STRING NOT NULL,
    executed_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS `sentinel.spend_transactions` (
    transaction_id STRING NOT NULL,
    workflow_root_id STRING NOT NULL,
    tenant STRING NOT NULL,
    supplier_id STRING NOT NULL,
    sku_id STRING NOT NULL,
    cost_center STRING NOT NULL,
    amount_usd NUMERIC NOT NULL,
    transaction_time TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS `sentinel.audit_ledger` (
    record_id STRING NOT NULL,
    deviation_id STRING NOT NULL,
    workflow_root_id STRING NOT NULL,
    phase STRING NOT NULL,
    payload_sha256 STRING NOT NULL,
    prompt_digest STRING,
    solver_result_sha256 STRING,
    okf_outcome STRING,
    operator_sub STRING,
    operator_jti STRING,
    approval_signature STRING,
    prev_record_hash STRING NOT NULL,
    record_hash STRING NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS `sentinel.supplier_reliability` (
    supplier_id STRING NOT NULL,
    on_time_rate_90d FLOAT64 NOT NULL,
    avg_lead_time_drift_days FLOAT64 NOT NULL,
    quote_variance_rate FLOAT64 NOT NULL,
    sample_size INT64 NOT NULL,
    provenance STRING NOT NULL,
    computed_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS `sentinel.agent_registry` (
    agent_id STRING NOT NULL,
    agent_name STRING NOT NULL,
    version STRING NOT NULL,
    input_schema_ref STRING,
    output_schema_ref STRING,
    rbac_scopes ARRAY<STRING>,
    owning_department STRING NOT NULL,
    status STRING NOT NULL,
    registered_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS `sentinel.okf_policy` (
    rule_id STRING NOT NULL,
    rule_name STRING NOT NULL,
    dimension STRING NOT NULL,
    ceiling_usd NUMERIC NOT NULL,
    effective_from TIMESTAMP NOT NULL,
    owner STRING NOT NULL,
    version STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS `sentinel.agent_ops` (
    event_id STRING NOT NULL,
    workflow_root_id STRING NOT NULL,
    agent_id STRING NOT NULL,
    step_name STRING NOT NULL,
    input_tokens INT64,
    output_tokens INT64,
    latency_ms INT64,
    tool_calls_count INT64,
    created_at TIMESTAMP NOT NULL
);

-- ----------------------------------------------------------------------------
-- 3. DATA ENGINEERING TABLES (sentinel_raw and sentinel_clean)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS `sentinel_raw.asn_landing` (
    asn_id STRING,
    sku_id STRING,
    dc_id STRING,
    supplier_id STRING,
    quantity STRING,
    uom STRING,
    ship_date STRING,
    eta_date STRING,
    received_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `sentinel_clean.asn_normalized` (
    asn_id STRING NOT NULL,
    sku_id STRING NOT NULL,
    dc_id STRING NOT NULL,
    supplier_id STRING NOT NULL,
    quantity_ea INT64 NOT NULL,
    ship_date DATE NOT NULL,
    eta_date DATE NOT NULL,
    normalized_at TIMESTAMP NOT NULL,
    pipeline_version STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS `sentinel_clean.asn_quarantine` (
    asn_id STRING,
    sku_id STRING,
    dc_id STRING,
    supplier_id STRING,
    raw_quantity STRING,
    raw_uom STRING,
    raw_ship_date STRING,
    raw_eta_date STRING,
    quarantine_reason STRING NOT NULL,
    quarantined_at TIMESTAMP NOT NULL
);
