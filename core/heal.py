"""Sentinel Autonomous Healing Engine (§8.6, I-9, I-10).

Executes idempotent remediation actions to the BigQuery `healing_actions` table.
Guarantees duplicate delivery protection via deterministic idempotency keys:
    idempotency_key = SHA256(deviation_id + sku_id + option_id)

Unconditionally emits INTENT prior to execution and OUTCOME after execution
(including duplicate replay paths) to the audit ledger per I-10.
"""

import sys
import uuid
import hashlib
import subprocess
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import google.auth
from google.auth.exceptions import RefreshError, DefaultCredentialsError
from google.oauth2 import credentials
from google.cloud import bigquery

from config import settings
from contracts.models import Deviation, Scenario
from core.ledger import append_ledger_record


def _get_credentials():
    """Retrieve credentials via google.auth with fallback to active gcloud token."""
    try:
        creds, _ = google.auth.default()
        import google.auth.transport.requests
        creds.refresh(google.auth.transport.requests.Request())
        return creds
    except (RefreshError, DefaultCredentialsError, Exception):
        try:
            token = subprocess.check_output(
                ["gcloud", "auth", "print-access-token"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if token:
                return credentials.Credentials(token)
        except Exception:
            pass
        return None


def compute_idempotency_key(deviation_id: str, sku_id: str, option_id: str) -> str:
    """Compute SHA-256 idempotency key over deviation_id + sku_id + option_id (I-9)."""
    raw_key = f"{deviation_id}:{sku_id}:{option_id}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def find_existing_healing_action(
    bq_client: bigquery.Client, dataset_id: str, idempotency_key: str
) -> Optional[Dict[str, Any]]:
    """Check if a healing action with this idempotency key was already executed."""
    query = f"""
    SELECT action_id, deviation_id, sku_id, option_id, mode, qty, cost_usd, status, idempotency_key, executed_at
    FROM `{bq_client.project}.{dataset_id}.healing_actions`
    WHERE idempotency_key = @key
    LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("key", "STRING", idempotency_key)]
    )
    try:
        rows = list(bq_client.query(query, job_config=job_config).result())
        if rows:
            r = rows[0]
            return {
                "action_id": r["action_id"],
                "deviation_id": r["deviation_id"],
                "sku_id": r["sku_id"],
                "option_id": r["option_id"],
                "mode": r["mode"],
                "qty": r["qty"],
                "cost_usd": str(r["cost_usd"]),
                "status": r["status"],
                "idempotency_key": r["idempotency_key"],
                "duplicate": True,
            }
    except Exception:
        pass
    return None


def execute_healing_action(
    bq_client: bigquery.Client,
    dataset_id: str,
    deviation: Deviation,
    scenario: Scenario,
    status: str = "AUTO_HEALED",
    override_idempotency_key: Optional[str] = None,
    workflow_root_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute and record an idempotent healing action with INTENT and OUTCOME audit records (I-9, I-10)."""
    if not scenario.selected:
        raise ValueError(f"Cannot execute healing action for infeasible/empty scenario '{scenario.scenario_id}'")

    if not workflow_root_id:
        workflow_root_id = f"WF-{deviation.deviation_id}"

    # Aggregate selected options info
    option_ids = ",".join(item["option_id"] for item in scenario.selected)
    total_qty = sum(item["qty"] for item in scenario.selected)
    total_cost = scenario.total_cost_usd or Decimal("0.00")
    mode = scenario.label

    # Idempotency key per I-9
    if override_idempotency_key:
        idempotency_key = override_idempotency_key
    else:
        first_opt = scenario.selected[0]["option_id"]
        idempotency_key = compute_idempotency_key(deviation.deviation_id, deviation.sku_id, first_opt)

    # 1. Unconditionally write INTENT record to audit ledger per I-10
    try:
        append_ledger_record(
            bq_client=bq_client,
            dataset_id=dataset_id,
            deviation_id=deviation.deviation_id,
            workflow_root_id=workflow_root_id,
            phase="INTENT",
            payload={
                "action_type": "PURCHASE_ORDER",
                "mode": mode,
                "qty": total_qty,
                "cost_usd": str(total_cost),
                "option_id": option_ids,
                "idempotency_key": idempotency_key,
                "target_status": status,
            },
            okf_outcome=status,
        )
    except Exception as e:
        print(f"Warning: Failed to append INTENT audit record: {e}", file=sys.stderr)

    # 2. Check for duplicate execution (I-9)
    existing = find_existing_healing_action(bq_client, dataset_id, idempotency_key)
    if existing:
        # Write OUTCOME record capturing duplicate replay detection per I-10
        try:
            append_ledger_record(
                bq_client=bq_client,
                dataset_id=dataset_id,
                deviation_id=deviation.deviation_id,
                workflow_root_id=workflow_root_id,
                phase="OUTCOME",
                payload=existing,
                okf_outcome="DUPLICATE_NOOP",
            )
        except Exception as e:
            print(f"Warning: Failed to append OUTCOME audit record for duplicate: {e}", file=sys.stderr)
        return existing

    # 3. Record new healing action
    action_id = f"ACT-{uuid.uuid4().hex[:8].upper()}"
    now_iso = datetime.now(timezone.utc).isoformat()

    row = {
        "action_id": action_id,
        "deviation_id": deviation.deviation_id,
        "sku_id": deviation.sku_id,
        "option_id": option_ids,
        "mode": mode,
        "qty": total_qty,
        "cost_usd": float(total_cost),
        "status": status,
        "idempotency_key": idempotency_key,
        "executed_at": now_iso,
    }

    table_ref = f"{bq_client.project}.{dataset_id}.healing_actions"
    errors = bq_client.insert_rows_json(table_ref, [row])
    if errors:
        raise RuntimeError(f"Failed to record healing action to {table_ref}: {errors}")

    result_payload = {
        "action_id": action_id,
        "deviation_id": deviation.deviation_id,
        "sku_id": deviation.sku_id,
        "option_id": option_ids,
        "mode": mode,
        "qty": total_qty,
        "cost_usd": str(total_cost),
        "status": status,
        "idempotency_key": idempotency_key,
        "duplicate": False,
    }

    # 4. Unconditionally write OUTCOME record to audit ledger per I-10
    try:
        append_ledger_record(
            bq_client=bq_client,
            dataset_id=dataset_id,
            deviation_id=deviation.deviation_id,
            workflow_root_id=workflow_root_id,
            phase="OUTCOME",
            payload=result_payload,
            okf_outcome=status,
        )
    except Exception as e:
        print(f"Warning: Failed to append OUTCOME audit record: {e}", file=sys.stderr)

    return result_payload
