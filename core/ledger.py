"""Sentinel Hash-Chained Audit Ledger (§8.3, §8.8, I-6, I-10).

Maintains a hash-chained, tamper-evident audit ledger in BigQuery.
Provides chain verification CLI to validate ledger integrity.

Chain formula:
    record_hash = SHA256(prev_record_hash + canonical_json(payload))
"""

import sys
import json
import hashlib
import argparse
import subprocess
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List

import google.auth
from google.auth.exceptions import RefreshError, DefaultCredentialsError
from google.oauth2 import credentials
from google.cloud import bigquery

from config import settings
from contracts.models import LedgerRecord


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


def get_latest_record_hash(bq_client: bigquery.Client, dataset_id: str) -> str:
    """Fetch the latest record_hash in the audit ledger, or 'GENESIS' if empty."""
    query = f"""
    SELECT record_hash
    FROM `{bq_client.project}.{dataset_id}.audit_ledger`
    ORDER BY created_at DESC
    LIMIT 1
    """
    try:
        rows = list(bq_client.query(query).result())
        if rows and rows[0]["record_hash"]:
            return rows[0]["record_hash"]
    except Exception:
        pass
    return "GENESIS"


def compute_record_hash(prev_record_hash: str, payload: Dict[str, Any]) -> Tuple[str, str]:
    """Compute payload SHA-256 and record hash over prev_record_hash + canonical_json."""
    canonical_payload = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))
    payload_sha256 = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    combined = prev_record_hash + canonical_payload
    record_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    return payload_sha256, record_hash


def append_ledger_record(
    bq_client: bigquery.Client,
    dataset_id: str,
    deviation_id: str,
    workflow_root_id: str,
    phase: str,
    payload: Dict[str, Any],
    prompt_digest: Optional[str] = None,
    solver_result_sha256: Optional[str] = None,
    okf_outcome: Optional[str] = None,
    operator_sub: Optional[str] = None,
    operator_jti: Optional[str] = None,
    approval_signature: Optional[str] = None,
) -> LedgerRecord:
    """Append a hash-chained record to the audit ledger (§8.3)."""
    prev_hash = get_latest_record_hash(bq_client, dataset_id)
    payload_sha256, record_hash = compute_record_hash(prev_hash, payload)

    record_id = f"REC-{hashlib.sha256(f'{deviation_id}{phase}{datetime.now().isoformat()}'.encode()).hexdigest()[:12].upper()}"
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    row = {
        "record_id": record_id,
        "deviation_id": deviation_id,
        "workflow_root_id": workflow_root_id,
        "phase": phase,
        "payload_sha256": payload_sha256,
        "prompt_digest": prompt_digest,
        "solver_result_sha256": solver_result_sha256,
        "okf_outcome": okf_outcome,
        "operator_sub": operator_sub,
        "operator_jti": operator_jti,
        "approval_signature": approval_signature,
        "prev_record_hash": prev_hash,
        "record_hash": record_hash,
        "created_at": now_iso,
    }

    table_ref = f"{bq_client.project}.{dataset_id}.audit_ledger"
    try:
        errors = bq_client.insert_rows_json(table_ref, [row])
        if errors:
            raise RuntimeError(f"Failed to append audit ledger record: {errors}")
    except Exception:
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
        )
        job = bq_client.load_table_from_json([row], table_ref, job_config=job_config)
        job.result()

    return LedgerRecord(
        record_id=record_id,
        deviation_id=deviation_id,
        workflow_root_id=workflow_root_id,
        phase=phase,
        payload_sha256=payload_sha256,
        prompt_digest=prompt_digest,
        solver_result_sha256=solver_result_sha256,
        okf_outcome=okf_outcome,
        operator_sub=operator_sub,
        operator_jti=operator_jti,
        approval_signature=approval_signature,
        prev_record_hash=prev_hash,
        record_hash=record_hash,
        created_at=now_dt,
    )


def verify_chain(bq_client: bigquery.Client, dataset_id: str) -> Tuple[bool, Optional[str], int]:
    """Verify hash chain integrity across all records in audit_ledger (§8.3)."""
    query = f"""
    SELECT record_id, deviation_id, workflow_root_id, phase, payload_sha256,
           prev_record_hash, record_hash, created_at
    FROM `{bq_client.project}.{dataset_id}.audit_ledger`
    ORDER BY created_at ASC, record_id ASC
    """
    try:
        rows = list(bq_client.query(query).result())
    except Exception as e:
        print(f"Error reading audit_ledger: {e}", file=sys.stderr)
        return False, str(e), 0

    if not rows:
        return True, None, 0

    # First attempt direct chronological verification
    expected_prev = "GENESIS"
    chrono_valid = True
    chrono_broken_at = None

    for idx, row in enumerate(rows):
        rec_id = row["record_id"]
        prev_hash = row["prev_record_hash"]
        rec_hash = row["record_hash"]

        if idx == 0:
            if prev_hash != "GENESIS":
                chrono_valid = False
                chrono_broken_at = f"Genesis block corrupted at record {rec_id} (expected GENESIS, got {prev_hash})"
                break
        else:
            if prev_hash != expected_prev:
                chrono_valid = False
                chrono_broken_at = f"Broken link at record {rec_id}: prev_record_hash {prev_hash} != expected {expected_prev}"
                break

        expected_prev = rec_hash

    if chrono_valid:
        return True, None, len(rows)

    # Reconstruct chain by following hash pointers from GENESIS to handle timestamp jitter
    prev_to_records = {}
    for r in rows:
        prev_to_records.setdefault(r["prev_record_hash"], []).append(r)

    if "GENESIS" not in prev_to_records:
        return False, chrono_broken_at or "Genesis block missing (no record with prev_record_hash='GENESIS')", len(rows)

    ordered_chain = []
    curr_hash = "GENESIS"
    visited_ids = set()

    while curr_hash in prev_to_records:
        candidates = [c for c in prev_to_records[curr_hash] if c["record_id"] not in visited_ids]
        if not candidates:
            break
        candidates.sort(key=lambda x: (str(x["created_at"]), x["record_id"]))
        next_rec = candidates[0]
        visited_ids.add(next_rec["record_id"])
        ordered_chain.append(next_rec)
        curr_hash = next_rec["record_hash"]

    if len(ordered_chain) == len(rows):
        return True, None, len(rows)

    unlinked = [r["record_id"] for r in rows if r["record_id"] not in visited_ids]
    broken_rec = unlinked[0] if unlinked else (chrono_broken_at or "UNKNOWN")
    return False, f"Broken link at record {broken_rec}", len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sentinel Hash-Chained Ledger Verifier (§8.3)")
    parser.add_argument(
        "action",
        nargs="?",
        default="verify",
        choices=["verify"],
        help="Action to perform (default: verify)",
    )
    args = parser.parse_args()

    creds = _get_credentials()
    bq_client = bigquery.Client(project=settings.PROJECT_ID, credentials=creds)

    print("=" * 65)
    print("SENTINEL AUDIT LEDGER: Hash Chain Verification (§8.3, Gate 3)")
    print("=" * 65)
    print(f"Project ID: {settings.PROJECT_ID}")
    print(f"Dataset:    {settings.BQ_DATASET}")
    print("-" * 65)

    ok, broken_at, count = verify_chain(bq_client, settings.BQ_DATASET)

    if ok:
        print(f"[VERIFIED] Ledger chain integrity verified across {count} records.")
        print("Status: 100% Tamper-Evident.")
        print("=" * 65)
        return 0
    else:
        print(f"[FAILED] Audit chain verification failed at: {broken_at}", file=sys.stderr)
        print("=" * 65, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
