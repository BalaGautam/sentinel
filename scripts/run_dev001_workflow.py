"""Execute DEV-001 workflow locally and inspect resulting state (§5, §8.8)."""

import sys
import json
from google.cloud import bigquery
from google.cloud import firestore

from config import settings
from agents.pipeline import run_sentinel_workflow, _get_credentials
from core.ledger import verify_chain


def main() -> int:
    creds = _get_credentials()
    bq_client = bigquery.Client(project=settings.PROJECT_ID, credentials=creds)

    print("=" * 70)
    print("SENTINEL LOCAL WORKFLOW EXECUTION: DEV-001")
    print("=" * 70)
    print(f"Project ID:           {settings.PROJECT_ID}")
    print(f"Dataset:              {settings.BQ_DATASET}")
    print(f"Region:               {settings.GCP_REGION}")
    print(f"Model ID:             {settings.MODEL_ID}")
    print(f"Inference Location:   {settings.VERTEX_INFERENCE_LOCATION}")
    print("-" * 70)

    # 1. Fetch DEV-001 payload from BigQuery deviations seed table
    q = f"""
    SELECT deviation_id, deviation_type, sku_id, dc_id, magnitude_units, delay_days, source_system, raw_note, detected_at
    FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.deviations`
    WHERE deviation_id = 'DEV-001'
    LIMIT 1
    """
    rows = list(bq_client.query(q).result())
    if not rows:
        print("ERROR: DEV-001 not found in deviations table!", file=sys.stderr)
        return 1

    d = rows[0]
    raw_payload = {
        "deviation_id": d["deviation_id"],
        "deviation_type": d["deviation_type"],
        "sku_id": d["sku_id"],
        "dc_id": d["dc_id"],
        "magnitude_units": d["magnitude_units"],
        "delay_days": d["delay_days"],
        "source_system": d["source_system"],
        "raw_note": d["raw_note"] or "",
        "detected_at": d["detected_at"].isoformat(),
    }

    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    print("\n[STEP 1/3] Executing Autonomous Multi-Agent Pipeline for DEV-001...")
    workflow_result = run_sentinel_workflow(
        raw_deviation_payload=raw_payload,
        traceparent=traceparent,
        tenant="SENTINEL_CORP",
        cost_center="CC_LOGISTICS",
    )

    print("\n[WORKFLOW RESULT SUMMARY]")
    print(f"  • Workflow Root ID:     {workflow_result.get('workflow_root_id')}")
    print(f"  • Status:               {workflow_result.get('status')}")
    print(f"  • Solver SHA-256:       {workflow_result.get('solver_sha256')}")
    print(f"  • Recommended Scenario: {workflow_result.get('recommended_scenario_id')}")
    print(f"  • OKF Policy Outcome:   {workflow_result.get('okf_outcome')}")
    print(f"  • Triggered Rules:      {workflow_result.get('triggered_rules')}")
    print(f"  • Healing Action:       {workflow_result.get('healing_action')}")
    print("\n[NARRATIVE (I-1: OrchestratorNarrative strictly non-numeric)]")
    print(f"  • narrative:    {workflow_result.get('narrative')}")
    print(f"  • risk_summary: {workflow_result.get('risk_summary')}")

    # 2. Display BigQuery dynamic tables
    print("\n" + "=" * 70)
    print("[STEP 2/3] Resulting BigQuery Records")
    print("=" * 70)

    print("\n--- BigQuery: audit_ledger ---")
    ledger_query = f"""
    SELECT record_id, deviation_id, phase, prev_record_hash, record_hash, okf_outcome, created_at
    FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.audit_ledger`
    ORDER BY created_at ASC
    """
    for r in bq_client.query(ledger_query).result():
        print(f"  [{r['phase']:<8}] {r['record_id']} | prev: {r['prev_record_hash'][:16]}... | hash: {r['record_hash'][:16]}... | outcome: {r['okf_outcome']}")

    print("\n--- BigQuery: healing_actions ---")
    heal_query = f"""
    SELECT action_id, deviation_id, sku_id, option_id, mode, qty, cost_usd, status, idempotency_key, executed_at
    FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.healing_actions`
    """
    for r in bq_client.query(heal_query).result():
        print(f"  {r['action_id']} | SKU: {r['sku_id']} | Option: {r['option_id']} | Mode: {r['mode']} | Qty: {r['qty']} | Cost: ${r['cost_usd']} | Status: {r['status']}")

    print("\n--- BigQuery: spend_transactions ---")
    spend_query = f"""
    SELECT transaction_id, supplier_id, sku_id, cost_center, amount_usd, transaction_time
    FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.spend_transactions`
    """
    for r in bq_client.query(spend_query).result():
        print(f"  {r['transaction_id']} | Supplier: {r['supplier_id']} | Amount: ${r['amount_usd']} | Cost Center: {r['cost_center']}")

    # 3. Hash Chain Integrity Verification
    print("\n" + "=" * 70)
    print("[STEP 3/3] Audit Ledger Hash Chain Verification (§8.3)")
    print("=" * 70)
    ok, broken_at, count = verify_chain(bq_client, settings.BQ_DATASET)
    if ok:
        print(f"[VERIFIED] Ledger chain integrity verified across {count} records. (100% Tamper-Evident)")
        print("=" * 70)
        return 0
    else:
        print(f"[FAILED] Audit chain verification failed at: {broken_at}", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
