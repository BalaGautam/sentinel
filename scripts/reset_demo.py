"""Sentinel Demo State Reset Script (§8, §8.8).

Runnable as:
    python -m scripts.reset_demo

Truncates:
    - spend_transactions
    - healing_actions
    - scenario_library
    - audit_ledger

Clears:
    - All Firestore reservation leases

Sets:
    - Kill-switch flag to False (`fleet/control.paused` = False)

Preserves:
    - All operational and seed tables untouched.

Prints:
    - Row counts before and after for all target tables.
"""

import sys
import subprocess
from typing import Dict, List, Optional
import google.auth
from google.auth.exceptions import RefreshError, DefaultCredentialsError
from google.oauth2 import credentials
from google.cloud import bigquery
from google.cloud import firestore

from config import settings

# Dynamic tables that accumulate state during demo execution
DYNAMIC_TABLES: List[str] = [
    "spend_transactions",
    "healing_actions",
    "scenario_library",
    "audit_ledger",
]


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


def get_table_row_count(bq_client: bigquery.Client, dataset_id: str, table_name: str) -> Optional[int]:
    """Query row count of a BigQuery table, returning None if table does not exist."""
    table_ref = f"`{bq_client.project}.{dataset_id}.{table_name}`"
    query = f"SELECT COUNT(1) AS total_rows FROM {table_ref}"
    try:
        query_job = bq_client.query(query)
        results = query_job.result()
        for row in results:
            return row["total_rows"]
    except Exception as e:
        err_msg = str(e).lower()
        if "not found" in err_msg:
            return None
        raise e
    return None


TABLE_DDLS: Dict[str, str] = {
    "audit_ledger": """
    CREATE TABLE `{project}.{dataset}.audit_ledger` (
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
    )
    """,
    "healing_actions": """
    CREATE TABLE `{project}.{dataset}.healing_actions` (
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
    )
    """,
    "spend_transactions": """
    CREATE TABLE `{project}.{dataset}.spend_transactions` (
        transaction_id STRING NOT NULL,
        workflow_root_id STRING NOT NULL,
        tenant STRING NOT NULL,
        supplier_id STRING NOT NULL,
        sku_id STRING NOT NULL,
        cost_center STRING NOT NULL,
        amount_usd NUMERIC NOT NULL,
        transaction_time TIMESTAMP NOT NULL
    )
    """,
    "scenario_library": """
    CREATE TABLE `{project}.{dataset}.scenario_library` (
        scenario_id STRING NOT NULL,
        deviation_id STRING NOT NULL,
        label STRING NOT NULL,
        selected_options_json STRING,
        total_cost_usd NUMERIC,
        sla_penalty_usd NUMERIC,
        total_exposure_usd NUMERIC,
        days_to_coverage INT64,
        feasible BOOL NOT NULL,
        solver_status STRING NOT NULL,
        result_sha256 STRING NOT NULL,
        created_at TIMESTAMP NOT NULL
    )
    """,
}


def truncate_bq_table(bq_client: bigquery.Client, dataset_id: str, table_name: str) -> None:
    """Drop and recreate table from DDL to cleanly discard any streaming buffer rows."""
    table_id = f"{bq_client.project}.{dataset_id}.{table_name}"
    try:
        bq_client.delete_table(table_id, not_found_ok=True)
    except Exception:
        pass

    ddl = TABLE_DDLS.get(table_name)
    if ddl:
        formatted_ddl = ddl.format(project=bq_client.project, dataset=dataset_id)
        bq_client.query(formatted_ddl).result()


def reset_bigquery_tables(bq_client: bigquery.Client, dataset_id: str) -> Dict[str, Dict[str, Optional[int]]]:
    """Recreate all dynamic demo tables while recording before/after counts."""
    counts: Dict[str, Dict[str, Optional[int]]] = {}

    for table in DYNAMIC_TABLES:
        before = get_table_row_count(bq_client, dataset_id, table)
        truncate_bq_table(bq_client, dataset_id, table)
        after = get_table_row_count(bq_client, dataset_id, table)
        counts[table] = {"before": before, "after": after}

    return counts


def clear_firestore_leases_and_killswitch(db: firestore.Client) -> Dict[str, any]:
    """Clear Firestore reservation leases and reset kill-switch flag to False."""
    deleted_leases = 0
    deleted_reservations = 0

    try:
        # Probe database connectivity first with short timeout
        control_ref = db.collection("fleet").document("control")
        control_ref.get(timeout=1.5)

        # 1. Clear top-level reservations collection if present
        try:
            res_docs = db.collection("reservations").stream(timeout=2.0)
            for doc in res_docs:
                doc.reference.delete()
                deleted_reservations += 1
        except Exception:
            pass

        # 2. Clear top-level leases & lease_locks collection if present
        try:
            lease_docs = db.collection("leases").stream()
            for doc in lease_docs:
                doc.reference.delete()
                deleted_leases += 1
        except Exception:
            pass

        try:
            lock_docs = db.collection("lease_locks").stream()
            for doc in lock_docs:
                doc.reference.delete()
        except Exception:
            pass

        # 3. Clear subcollections and reset fleet control kill-switch document
        control_ref = db.collection("fleet").document("control")
        try:
            sub_res = control_ref.collection("reservations").stream()
            for doc in sub_res:
                doc.reference.delete()
                deleted_reservations += 1
        except Exception:
            pass

        try:
            sub_leases = control_ref.collection("leases").stream()
            for doc in sub_leases:
                doc.reference.delete()
                deleted_leases += 1
        except Exception:
            pass

        # 4. Set kill switch flag to False and clear active leases map
        control_ref.set(
            {
                "paused": False,
                "kill_switch": False,
                "active_leases": {},
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )

        # 5. Reset Firestore ledger head to GENESIS (§8.3)
        try:
            head_ref = db.collection("governance").document("ledger_head")
            head_ref.set({
                "head_hash": "GENESIS",
                "prev_hash": "GENESIS",
                "record_id": "GENESIS",
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
        except Exception:
            pass

        return {
            "status": "CLEARED",
            "deleted_reservations": deleted_reservations,
            "deleted_leases": deleted_leases,
            "paused": False,
            "kill_switch": False,
        }

    except Exception as e:
        err_str = str(e).lower()
        if "not exist" in err_str or "not found" in err_str or "404" in err_str:
            return {
                "status": "UNPROVISIONED",
                "message": "Firestore (default) database not yet provisioned; skipped.",
                "deleted_reservations": 0,
                "deleted_leases": 0,
                "paused": False,
                "kill_switch": False,
            }
        raise e


def reset_demo_state(bq_client: bigquery.Client, fs_client: Optional[firestore.Client] = None) -> Dict[str, Any]:
    """Programmatic entry point for resetting demo state from Streamlit UI or scripts."""
    bq_counts = reset_bigquery_tables(bq_client, settings.BQ_DATASET)
    fs_res = {}
    if fs_client is not None:
        try:
            fs_res = clear_firestore_leases_and_killswitch(fs_client)
        except Exception as e:
            fs_res = {"error": str(e)}
    return {
        "bq_counts": bq_counts,
        "fs_result": fs_res,
    }


def reset_demo() -> int:
    """Main orchestrator for resetting the demo environment."""
    print("=" * 65)
    print("SENTINEL DEMO RESET: Dynamic State & Governance Reset (§8.8)")
    print("=" * 65)
    print(f"Project ID:  {settings.PROJECT_ID}")
    print(f"BQ Dataset:  {settings.BQ_DATASET}")
    print(f"GCP Region:  {settings.GCP_REGION}")
    print("-" * 65)

    creds = _get_credentials()

    # --- 1. BigQuery Reset ---
    print("\n[1/3] Resetting BigQuery Dynamic Tables...")
    try:
        bq_client = bigquery.Client(project=settings.PROJECT_ID, credentials=creds)
        counts = reset_bigquery_tables(bq_client, settings.BQ_DATASET)

        print(f"{'Table Name':<25} {'Before':<12} {'After':<12} {'Status'}")
        print("-" * 65)
        for table, c in counts.items():
            before_str = str(c["before"]) if c["before"] is not None else "NOT FOUND"
            after_str = str(c["after"]) if c["after"] is not None else "NOT FOUND"
            status_str = "TRUNCATED" if (c["before"] or 0) > 0 else "CLEAN"
            if c["before"] is None:
                status_str = "SKIPPED (ABSENT)"
            print(f"{table:<25} {before_str:<12} {after_str:<12} {status_str}")

    except Exception as e:
        print(f"ERROR: Failed resetting BigQuery tables: {e}", file=sys.stderr)
        return 1

    # --- 2. Firestore Reset ---
    print("\n[2/3] Clearing Firestore Reservation Leases & Leases...")
    try:
        fs_client = firestore.Client(project=settings.PROJECT_ID, credentials=creds)
        fs_result = clear_firestore_leases_and_killswitch(fs_client)
        if fs_result.get("status") == "UNPROVISIONED":
            print("  • Note: Firestore (default) database not yet provisioned in GCP project; skipped.")
            print("\n[3/3] Setting Kill-Switch Flag to False...")
            print("  • Skipped (Firestore unprovisioned)")
        else:
            print(f"  • Cleared reservation documents: {fs_result['deleted_reservations']}")
            print(f"  • Cleared lease documents:        {fs_result['deleted_leases']}")
            print("  • Cleared active leases in 'fleet/control'")

            # --- 3. Kill-Switch Reset ---
            print("\n[3/3] Setting Kill-Switch Flag to False...")
            print(f"  • fleet/control.paused     = {fs_result['paused']}")
            print(f"  • fleet/control.kill_switch = {fs_result['kill_switch']}")

    except Exception as e:
        print(f"ERROR: Failed resetting Firestore state: {e}", file=sys.stderr)
        return 1

    print("\n" + "=" * 65)
    print("SUCCESS: Demo state reset complete. Operational & seed tables preserved.")
    print("=" * 65)
    return 0


if __name__ == "__main__":
    sys.exit(reset_demo())
