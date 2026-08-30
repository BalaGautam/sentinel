"""Sentinel HMAC-SHA256 Signed HITL Approval Engine (§8.7, I-7, I-10).

Enforces Invariant I-7:
- Human-in-the-loop approvals are HMAC-SHA256 signed.
- Captures operator identity: sub, email, jti, iat, roles, and signature.
- Verifies signature before committing spend reservation and executing remediation.
- Records an APPROVAL phase entry into the hash-chained audit ledger.
- Executes healing action with status='HITL_APPROVED'.
"""

import os
import sys
import json
import uuid
import hmac
import hashlib
import secrets
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple

import google.auth
from google.auth.exceptions import RefreshError, DefaultCredentialsError
from google.oauth2 import credentials
from google.cloud import bigquery
from google.cloud import firestore

from config import settings
from contracts.models import Deviation, Scenario, ScenarioSet, LedgerRecord
from core.ledger import append_ledger_record
from core.heal import execute_healing_action
from core.okf import OKFGovernor


def get_approval_secret_key() -> bytes:
    """Retrieve HMAC signing key from environment or fail hard (§2.5)."""
    raw_key = os.environ.get("APPROVAL_SECRET_KEY")
    if not raw_key or not raw_key.strip():
        raise ValueError("APPROVAL_SECRET_KEY environment variable is not set. Cannot sign or verify approvals.")
    return raw_key.strip().encode("utf-8")


def generate_operator_token(
    operator_sub: str = "usr-op-7842",
    operator_email: str = "operator@sentinel-corp.internal",
    operator_role: str = "Supply Chain Director",
) -> Dict[str, Any]:
    """Generate a valid operator identity envelope with JTI and timestamp."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    jti = f"jti-{uuid.uuid4().hex[:12]}"
    return {
        "sub": operator_sub,
        "email": operator_email,
        "role": operator_role,
        "jti": jti,
        "iat": now_ts,
        "iss": "sentinel-identity-broker",
        "aud": "sentinel-fleet-orchestrator",
    }


def compute_approval_signature(canonical_payload: str, secret_key: Optional[bytes] = None) -> str:
    """Compute HMAC-SHA256 signature over canonical approval payload."""
    key = secret_key if secret_key is not None else get_approval_secret_key()
    sig = hmac.new(key, canonical_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{sig}"


def create_signed_approval_envelope(
    deviation_id: str,
    workflow_root_id: str,
    selected_scenario: Scenario,
    sku_id: str,
    supplier_id: str,
    operator_sub: str = "usr-op-7842",
    operator_email: str = "operator@sentinel-corp.internal",
    operator_role: str = "Supply Chain Director",
    reservation_id: Optional[str] = None,
    policy_version: str = "v1.0",
    secret_key: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Create an HMAC-SHA256 signed approval envelope for HITL execution (I-7)."""
    identity = generate_operator_token(operator_sub, operator_email, operator_role)
    amount_usd = str(selected_scenario.total_cost_usd or "0.00")

    approval_body = {
        "deviation_id": deviation_id,
        "workflow_root_id": workflow_root_id,
        "scenario_id": selected_scenario.scenario_id,
        "scenario_label": selected_scenario.label,
        "amount_usd": amount_usd,
        "sku_id": sku_id,
        "supplier_id": supplier_id,
        "reservation_id": reservation_id,
        "policy_version": policy_version,
        "operator": identity,
    }

    canonical_str = json.dumps(approval_body, default=str, sort_keys=True, separators=(",", ":"))
    signature = compute_approval_signature(canonical_str, secret_key=secret_key)

    return {
        "approval_id": f"APV-{uuid.uuid4().hex[:8].upper()}",
        "payload": approval_body,
        "canonical_payload": canonical_str,
        "signature": signature,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def verify_approval_signature(approval_envelope: Dict[str, Any], secret_key: Optional[bytes] = None) -> Tuple[bool, Optional[str]]:
    """Verify HMAC-SHA256 signature of an operator approval envelope (I-7)."""
    canonical_payload = approval_envelope.get("canonical_payload")
    if not canonical_payload and "payload" in approval_envelope:
        canonical_payload = json.dumps(approval_envelope["payload"], default=str, sort_keys=True, separators=(",", ":"))

    if not canonical_payload:
        return False, "Missing payload for signature verification"

    provided_sig = approval_envelope.get("signature", "")
    expected_sig = compute_approval_signature(canonical_payload, secret_key=secret_key)

    if not hmac.compare_digest(provided_sig, expected_sig):
        return False, f"Invalid signature. Provided: {provided_sig[:20]}... Expected: {expected_sig[:20]}..."

    return True, None


def execute_signed_approval(
    bq_client: bigquery.Client,
    dataset_id: str,
    deviation: Deviation,
    selected_scenario: Scenario,
    approval_envelope: Dict[str, Any],
    fs_client: Optional[firestore.Client] = None,
    tenant: str = "SENTINEL_CORP",
    cost_center: str = "CC_LOGISTICS",
    secret_key: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Verify HMAC-SHA256 signature, append APPROVAL record to ledger, commit spend, and heal (I-7, I-10)."""
    # 1. Verify signature
    valid, err = verify_approval_signature(approval_envelope, secret_key=secret_key)
    if not valid:
        raise ValueError(f"Approval verification failed: {err}")

    payload = approval_envelope.get("payload", {})
    operator = payload.get("operator", {})
    operator_sub = operator.get("sub", "UNKNOWN_OPERATOR")
    operator_jti = operator.get("jti", f"jti-{uuid.uuid4().hex[:8]}")
    signature = approval_envelope.get("signature", "")
    workflow_root_id = payload.get("workflow_root_id", f"WF-{deviation.deviation_id}")
    reservation_id = payload.get("reservation_id")
    supplier_id = payload.get("supplier_id", "SUP-09")

    # 2. Append APPROVAL record to audit ledger per I-7 & I-10
    approval_record = append_ledger_record(
        bq_client=bq_client,
        dataset_id=dataset_id,
        deviation_id=deviation.deviation_id,
        workflow_root_id=workflow_root_id,
        phase="APPROVAL",
        payload={
            "approval_id": approval_envelope.get("approval_id"),
            "scenario_id": selected_scenario.scenario_id,
            "scenario_label": selected_scenario.label,
            "amount_usd": str(selected_scenario.total_cost_usd),
            "operator_sub": operator_sub,
            "operator_email": operator.get("email"),
            "operator_role": operator.get("role"),
            "operator_jti": operator_jti,
            "signature": signature,
        },
        operator_sub=operator_sub,
        operator_jti=operator_jti,
        approval_signature=signature,
        okf_outcome="HITL_APPROVED",
    )

    # 3. Commit spend lease in Governor
    governor = OKFGovernor(bq_client, dataset_id, fs_client)
    resolved_cost_center = governor.get_supplier_cost_center(supplier_id) or cost_center
    governor.commit_reservation(
        reservation_id=reservation_id,
        workflow_root_id=workflow_root_id,
        tenant=tenant,
        supplier_id=supplier_id,
        sku_id=deviation.sku_id,
        cost_center=resolved_cost_center,
        amount_usd=selected_scenario.total_cost_usd or Decimal("0.00"),
    )

    # 4. Execute idempotent healing action with status='HITL_APPROVED' (I-9, I-10)
    healing_result = execute_healing_action(
        bq_client=bq_client,
        dataset_id=dataset_id,
        deviation=deviation,
        scenario=selected_scenario,
        status="HITL_APPROVED",
        workflow_root_id=workflow_root_id,
    )

    return {
        "status": "APPROVED_AND_EXECUTED",
        "approval_id": approval_envelope.get("approval_id"),
        "approval_ledger_record_id": approval_record.record_id,
        "operator_sub": operator_sub,
        "operator_jti": operator_jti,
        "approval_signature": signature,
        "healing_action": healing_result,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }
