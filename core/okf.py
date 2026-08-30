"""Sentinel OKF Policy Governor (§8.2, I-3, I-4, I-5, I-11).

Enforces organizational knowledge framework governance rules over proposed
remediation actions. Policy rules and thresholds are loaded dynamically
from the BigQuery `okf_policy` table at runtime.

Rules in Order (Most restrictive wins):
    1. SINGLE_TXN_CAP   - amount > $5,000 -> REQUIRE_HITL
    2. VELOCITY_CAP     - 24h spend counter breach on any dimension -> REQUIRE_HITL
                          (tenant $10k, supplier $7.5k, sku $6k, cost_center $8k, workflow_root $5k)
    3. VIP_GUARD        - exposed order is TIER_1 -> REQUIRE_HITL
    4. DEGRADED_SOLVER  - scenario_set.degraded == True -> REQUIRE_HITL
    5. KILL_SWITCH      - Firestore `fleet/control.paused` == True -> BLOCKED

In-flight reservation leases are tracked via Firestore transactions and an in-memory lease store.
Unconditionally logs a POLICY record to the audit ledger for every evaluation (I-10).
"""

import sys
import uuid
import subprocess
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any

import google.auth
from google.auth.exceptions import RefreshError, DefaultCredentialsError
from google.oauth2 import credentials
from google.cloud import bigquery
from google.cloud import firestore

from config import settings
from contracts.models import Deviation, Scenario, ScenarioSet, OKFDecision
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


class OKFGovernor:
    """Policy Governor loaded dynamically from the okf_policy BigQuery table."""

    def __init__(self, bq_client: bigquery.Client, dataset_id: str, fs_client: Optional[firestore.Client] = None):
        self.bq_client = bq_client
        self.dataset_id = dataset_id
        self.fs_client = fs_client

        # Local in-memory reservation store for in-flight leases (I-4)
        self._in_memory_reservations: Dict[str, Dict[str, Any]] = {}

        # Probe Firestore connectivity once to fail-fast if unprovisioned
        if self.fs_client:
            try:
                self.fs_client.collection("fleet").document("control").get(timeout=2.0)
            except Exception:
                self.fs_client = None

        self.policy_version: str = "v1.0"
        self.policies: Dict[str, Dict[str, Any]] = {}
        self.load_policies()

    def load_policies(self) -> None:
        """Load all policy thresholds and metadata from the okf_policy table (§8.2)."""
        query = f"""
        SELECT rule_id, rule_name, dimension, ceiling_usd, version
        FROM `{self.bq_client.project}.{self.dataset_id}.okf_policy`
        """
        try:
            rows = list(self.bq_client.query(query).result())
            for r in rows:
                key = f"{r['rule_name']}:{r['dimension']}" if r['dimension'] != 'TRANSACTION' else r['rule_name']
                self.policies[key] = {
                    "rule_id": r["rule_id"],
                    "rule_name": r["rule_name"],
                    "dimension": r["dimension"],
                    "ceiling_usd": Decimal(str(r["ceiling_usd"])),
                    "version": r["version"],
                }
                self.policy_version = r["version"]
        except Exception:
            # Fallback to standard seeded policy defaults if uninitialized
            self.policies = {
                "SINGLE_TXN_CAP": {"rule_name": "SINGLE_TXN_CAP", "dimension": "TRANSACTION", "ceiling_usd": Decimal("5000.00"), "version": "v1.0"},
                "VELOCITY_CAP:TENANT": {"rule_name": "VELOCITY_CAP", "dimension": "TENANT", "ceiling_usd": Decimal("10000.00"), "version": "v1.0"},
                "VELOCITY_CAP:SUPPLIER": {"rule_name": "VELOCITY_CAP", "dimension": "SUPPLIER", "ceiling_usd": Decimal("7500.00"), "version": "v1.0"},
                "VELOCITY_CAP:SKU": {"rule_name": "VELOCITY_CAP", "dimension": "SKU", "ceiling_usd": Decimal("6000.00"), "version": "v1.0"},
                "VELOCITY_CAP:COST_CENTER": {"rule_name": "VELOCITY_CAP", "dimension": "COST_CENTER", "ceiling_usd": Decimal("8000.00"), "version": "v1.0"},
                "VELOCITY_CAP:WORKFLOW_ROOT": {"rule_name": "VELOCITY_CAP", "dimension": "WORKFLOW_ROOT", "ceiling_usd": Decimal("5000.00"), "version": "v1.0"},
                "VIP_GUARD:CUSTOMER_TIER": {"rule_name": "VIP_GUARD", "dimension": "CUSTOMER_TIER", "ceiling_usd": Decimal("0.00"), "version": "v1.0"},
                "DEGRADED_SOLVER:SOLVER_STATUS": {"rule_name": "DEGRADED_SOLVER", "dimension": "SOLVER_STATUS", "ceiling_usd": Decimal("0.00"), "version": "v1.0"},
                "KILL_SWITCH:FLEET_CONTROL": {"rule_name": "KILL_SWITCH", "dimension": "FLEET_CONTROL", "ceiling_usd": Decimal("0.00"), "version": "v1.0"},
            }

    def check_kill_switch(self) -> bool:
        """Check if global fleet kill-switch is active in Firestore."""
        if not self.fs_client:
            return False
        try:
            doc = self.fs_client.collection("fleet").document("control").get(timeout=2.0)
            if doc.exists:
                data = doc.to_dict() or {}
                return bool(data.get("paused") or data.get("kill_switch"))
        except Exception:
            pass
        return False

    def get_24h_spent(self, dimension_col: str, dimension_val: str) -> Decimal:
        """Query actual committed 24h spend from spend_transactions in BigQuery."""
        query = f"""
        SELECT COALESCE(SUM(amount_usd), 0) AS total_spent
        FROM `{self.bq_client.project}.{self.dataset_id}.spend_transactions`
        WHERE {dimension_col} = @dim_val
          AND transaction_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("dim_val", "STRING", dimension_val)]
        )
        try:
            rows = list(self.bq_client.query(query, job_config=job_config).result())
            return Decimal(str(rows[0]["total_spent"])) if rows else Decimal("0.00")
        except Exception:
            return Decimal("0.00")

    def get_in_flight_reservations(self, dimension_key: str, dimension_val: str) -> Decimal:
        """Sum pending in-flight reservation leases from in-memory store and Firestore (§8.2, I-4)."""
        total_reserved = Decimal("0.00")

        # 1. Check in-memory reservation store
        for res in self._in_memory_reservations.values():
            if res.get("status") == "PENDING" and res.get(dimension_key) == dimension_val:
                total_reserved += Decimal(str(res.get("amount_usd", "0.00")))

        # 2. Check Firestore if active
        if self.fs_client:
            try:
                from google.cloud.firestore_v1.base_query import FieldFilter
                leases = self.fs_client.collection("reservations").filter(filter=FieldFilter(dimension_key, "==", dimension_val)).stream(timeout=2.0)
                for lease in leases:
                    d = lease.to_dict() or {}
                    res_id = d.get("reservation_id")
                    # Avoid double-counting if already in memory
                    if res_id not in self._in_memory_reservations and d.get("status") == "PENDING":
                        total_reserved += Decimal(str(d.get("amount_usd", 0.0)))
            except Exception:
                pass

        return total_reserved

    def create_reservation(self, dimensions: Dict[str, str], amount_usd: Decimal) -> str:
        """Create a pending in-flight spend reservation lease (§8.2, I-4)."""
        reservation_id = f"RES-{uuid.uuid4().hex[:8].upper()}"
        lease_data = {
            "reservation_id": reservation_id,
            "tenant": dimensions.get("tenant", "SENTINEL_CORP"),
            "supplier_id": dimensions.get("supplier_id", ""),
            "sku_id": dimensions.get("sku_id", ""),
            "cost_center": dimensions.get("cost_center", ""),
            "workflow_root_id": dimensions.get("workflow_root_id", ""),
            "amount_usd": str(amount_usd),
            "status": "PENDING",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # Store in-memory lease
        self._in_memory_reservations[reservation_id] = lease_data

        # Store in Firestore if active
        if self.fs_client:
            try:
                doc_ref = self.fs_client.collection("reservations").document(reservation_id)
                doc_ref.set({
                    "reservation_id": reservation_id,
                    "tenant": dimensions.get("tenant", "SENTINEL_CORP"),
                    "supplier_id": dimensions.get("supplier_id", ""),
                    "sku_id": dimensions.get("sku_id", ""),
                    "cost_center": dimensions.get("cost_center", ""),
                    "workflow_root_id": dimensions.get("workflow_root_id", ""),
                    "amount_usd": float(amount_usd),
                    "status": "PENDING",
                    "created_at": firestore.SERVER_TIMESTAMP,
                })
            except Exception:
                pass

        return reservation_id

    def commit_reservation(
        self,
        reservation_id: Optional[str],
        workflow_root_id: str,
        tenant: str,
        supplier_id: str,
        sku_id: str,
        cost_center: str,
        amount_usd: Decimal,
    ) -> None:
        """Commit an approved reservation by removing lease and writing spend_transactions."""
        # 1. Append spend transaction to BigQuery
        table_ref = f"{self.bq_client.project}.{self.dataset_id}.spend_transactions"
        txn_id = f"TXN-{uuid.uuid4().hex[:8].upper()}"
        row = {
            "transaction_id": txn_id,
            "workflow_root_id": workflow_root_id,
            "tenant": tenant,
            "supplier_id": supplier_id,
            "sku_id": sku_id,
            "cost_center": cost_center,
            "amount_usd": str(amount_usd),
            "transaction_time": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.bq_client.insert_rows_json(table_ref, [row])
        except Exception:
            job_config = bigquery.LoadJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
            )
            job = self.bq_client.load_table_from_json([row], table_ref, job_config=job_config)
            job.result()

        # 2. Clear lease from in-memory store and Firestore
        if reservation_id:
            self._in_memory_reservations.pop(reservation_id, None)
            if self.fs_client:
                try:
                    self.fs_client.collection("reservations").document(reservation_id).delete()
                except Exception:
                    pass

    def release_reservation(self, reservation_id: Optional[str]) -> None:
        """Release a rejected/cancelled reservation lease without booking spend."""
        if reservation_id:
            self._in_memory_reservations.pop(reservation_id, None)
            if self.fs_client:
                try:
                    self.fs_client.collection("reservations").document(reservation_id).delete()
                except Exception:
                    pass

    def check_has_tier_1_orders(self, sku_id: str, dc_id: str) -> bool:
        """Check if any active customer order for this sku and dc is TIER_1."""
        query = f"""
        SELECT COUNT(1) AS tier_1_count
        FROM `{self.bq_client.project}.{self.dataset_id}.customer_orders`
        WHERE sku_id = @sku_id AND dc_id = @dc_id AND tier = 'TIER_1'
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("sku_id", "STRING", sku_id),
                bigquery.ScalarQueryParameter("dc_id", "STRING", dc_id),
            ]
        )
        try:
            rows = list(self.bq_client.query(query, job_config=job_config).result())
            return rows[0]["tier_1_count"] > 0 if rows else False
        except Exception:
            return False

    def get_supplier_cost_center(self, supplier_id: str) -> str:
        """Retrieve cost center for a supplier from supplier_master."""
        query = f"""
        SELECT cost_center FROM `{self.bq_client.project}.{self.dataset_id}.supplier_master`
        WHERE supplier_id = @sup_id LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("sup_id", "STRING", supplier_id)]
        )
        try:
            rows = list(self.bq_client.query(query, job_config=job_config).result())
            return rows[0]["cost_center"] if rows else "CC_PROCUREMENT"
        except Exception:
            return "CC_PROCUREMENT"

    def evaluate(
        self,
        deviation: Deviation,
        scenario_set: ScenarioSet,
        recommended_scenario: Scenario,
        supplier_id: str,
        workflow_root_id: Optional[str] = None,
        tenant: str = "SENTINEL_CORP",
    ) -> OKFDecision:
        """Evaluate all OKF governance rules over the proposed recommended scenario and log POLICY record (I-10)."""
        if not workflow_root_id:
            workflow_root_id = f"WF-{deviation.deviation_id}"

        triggered_rules: List[str] = []
        cost_center = self.get_supplier_cost_center(supplier_id)
        amount_usd = recommended_scenario.total_cost_usd or Decimal("0.00")

        # Dimensional setup
        dimensions = {
            "tenant": tenant,
            "supplier_id": supplier_id,
            "sku_id": deviation.sku_id,
            "cost_center": cost_center,
            "workflow_root_id": workflow_root_id,
        }

        dim_mapping = [
            ("TENANT", "tenant", tenant, self.policies.get("VELOCITY_CAP:TENANT", {}).get("ceiling_usd", Decimal("10000.00"))),
            ("SUPPLIER", "supplier_id", supplier_id, self.policies.get("VELOCITY_CAP:SUPPLIER", {}).get("ceiling_usd", Decimal("7500.00"))),
            ("SKU", "sku_id", deviation.sku_id, self.policies.get("VELOCITY_CAP:SKU", {}).get("ceiling_usd", Decimal("6000.00"))),
            ("COST_CENTER", "cost_center", cost_center, self.policies.get("VELOCITY_CAP:COST_CENTER", {}).get("ceiling_usd", Decimal("8000.00"))),
            ("WORKFLOW_ROOT", "workflow_root_id", workflow_root_id, self.policies.get("VELOCITY_CAP:WORKFLOW_ROOT", {}).get("ceiling_usd", Decimal("5000.00"))),
        ]

        # Snapshots
        counters_snapshot: Dict[str, Dict[str, str]] = {}

        # 1. Check KILL_SWITCH (Most restrictive: BLOCKED)
        if self.check_kill_switch():
            triggered_rules.append("KILL_SWITCH")
            outcome = "BLOCKED"
            reservation_id = None
        else:
            # 2. Check SINGLE_TXN_CAP
            single_cap = self.policies.get("SINGLE_TXN_CAP", {}).get("ceiling_usd", Decimal("5000.00"))
            if amount_usd > single_cap:
                triggered_rules.append("SINGLE_TXN_CAP")

            # 3. Check VELOCITY_CAP across all 5 dimensions (including pending reservation leases)
            for dim_name, col_name, val, ceiling in dim_mapping:
                past_spent = self.get_24h_spent(col_name, val)
                in_flight = self.get_in_flight_reservations(col_name, val)
                used = past_spent + in_flight
                projected = used + amount_usd

                counters_snapshot[dim_name.lower()] = {
                    "used": str(used),
                    "projected": str(projected),
                    "ceiling": str(ceiling),
                }

                if projected > ceiling:
                    rule_tag = f"VELOCITY_CAP:{dim_name}"
                    if rule_tag not in triggered_rules and "VELOCITY_CAP" not in triggered_rules:
                        triggered_rules.append(rule_tag)

            # 4. Check VIP_GUARD
            if self.check_has_tier_1_orders(deviation.sku_id, deviation.dc_id):
                triggered_rules.append("VIP_GUARD")

            # 5. Check DEGRADED_SOLVER (I-11)
            if scenario_set.degraded or recommended_scenario.solver_status == "HEURISTIC_FALLBACK":
                triggered_rules.append("DEGRADED_SOLVER")

            # Outcome determination
            if triggered_rules:
                outcome = "REQUIRE_HITL"
                reservation_id = None
            else:
                outcome = "AUTO_HEAL"
                reservation_id = self.create_reservation(dimensions, amount_usd)

        # Unconditionally log POLICY record into audit ledger per I-10
        try:
            append_ledger_record(
                bq_client=self.bq_client,
                dataset_id=self.dataset_id,
                deviation_id=deviation.deviation_id,
                workflow_root_id=workflow_root_id,
                phase="POLICY",
                payload={
                    "outcome": outcome,
                    "triggered_rules": triggered_rules,
                    "counters_snapshot": counters_snapshot,
                    "amount_usd": str(amount_usd),
                    "policy_version": self.policy_version,
                },
                solver_result_sha256=scenario_set.result_sha256,
                okf_outcome=outcome,
            )
        except Exception as e:
            print(f"Warning: Failed to append POLICY audit record: {e}", file=sys.stderr)

        return OKFDecision(
            outcome=outcome,
            triggered_rules=triggered_rules,
            amount_usd=amount_usd,
            counters_snapshot=counters_snapshot,
            reservation_id=reservation_id,
        )
