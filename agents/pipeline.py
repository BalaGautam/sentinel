"""Sentinel End-to-End Fleet Pipeline Runner (§5, §8.12, I-1 to I-12).

Orchestrates the full flow:
1. Ingress Guardrail & Hygiene Agent (SENSE, SANITIZE)
2. Triage Orchestration with Sourcing Specialist & MILP Solver (SCORE)
3. OrchestratorNarrative Synthesis & Egress Guardrail (I-1, I-12)
4. OKF Policy Governor Evaluation (POLICY)
5. Autonomous Healing Execution (INTENT, OUTCOME)
"""

import sys
import uuid
import subprocess
from typing import Dict, Any, Optional
from google.cloud import bigquery
from google.cloud import firestore

from config import settings
from contracts.models import Deviation, ScenarioSet, OrchestratorNarrative, OKFDecision
from agents.hygiene import HygieneAgent
from agents.orchestrator import TriageOrchestrator
from core.okf import OKFGovernor
from core.heal import execute_healing_action


def _get_credentials():
    """Retrieve credentials via google.auth with fallback to active gcloud token."""
    import google.auth
    from google.auth.exceptions import RefreshError, DefaultCredentialsError
    from google.oauth2 import credentials
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


def run_sentinel_workflow(
    raw_deviation_payload: Dict[str, Any],
    traceparent: Optional[str] = None,
    tenant: str = "SENTINEL_CORP",
    cost_center: str = "CC_LOGISTICS",
) -> Dict[str, Any]:
    """Execute the fortified agent fleet workflow over an inbound deviation."""
    creds = _get_credentials()
    bq_client = bigquery.Client(project=settings.PROJECT_ID, credentials=creds)
    try:
        fs_client = firestore.Client(project=settings.PROJECT_ID, credentials=creds)
    except Exception:
        fs_client = None

    dev_id = raw_deviation_payload.get("deviation_id", f"DEV-{uuid.uuid4().hex[:6].upper()}")
    workflow_root_id = f"WF-{dev_id}"

    # Step 1: Ingress Guardrail & Hygiene Agent (I-12, I-10)
    hygiene = HygieneAgent(bq_client, settings.BQ_DATASET)
    deviation, guard_res = hygiene.process(raw_deviation_payload, workflow_root_id=workflow_root_id)

    if not deviation or not guard_res.passed:
        return {
            "deviation_id": dev_id,
            "workflow_root_id": workflow_root_id,
            "status": "BLOCKED_BY_GUARDRAIL",
            "reason": guard_res.reason,
            "traceparent": traceparent,
        }

    # Step 2: Triage Orchestrator (Sub-agent + Solver + Egress Guardrail)
    orchestrator = TriageOrchestrator(bq_client, settings.BQ_DATASET)
    scenario_set, narrative = orchestrator.orchestrate(deviation, workflow_root_id=workflow_root_id)

    rec_scenario = next(
        s for s in scenario_set.scenarios if s.scenario_id == scenario_set.recommended_scenario_id
    )

    # Determine supplier id from selected option or fallback
    supplier_id = "SUP-11"
    if rec_scenario.selected and "option_id" in rec_scenario.selected[0]:
        opt_id = rec_scenario.selected[0]["option_id"]
        # Lookup supplier from supply_options
        try:
            q = f"SELECT supplier_id FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.supply_options` WHERE option_id = @opt LIMIT 1"
            res = list(bq_client.query(q, job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("opt", "STRING", opt_id)]
            )).result())
            if res:
                supplier_id = res[0]["supplier_id"]
        except Exception:
            pass

    # Step 3: OKF Policy Governor (I-3, I-4, I-5, I-11)
    governor = OKFGovernor(bq_client, settings.BQ_DATASET, fs_client)
    cost_center = governor.get_supplier_cost_center(supplier_id)
    decision = governor.evaluate(
        deviation=deviation,
        scenario_set=scenario_set,
        recommended_scenario=rec_scenario,
        supplier_id=supplier_id,
        workflow_root_id=workflow_root_id,
        tenant=tenant,
    )

    healing_result = None

    # Step 4: Autonomous Healing or Escalation (I-9, I-10)
    if decision.outcome == "AUTO_HEAL":
        governor.commit_reservation(
            reservation_id=decision.reservation_id,
            workflow_root_id=workflow_root_id,
            tenant=tenant,
            supplier_id=supplier_id,
            sku_id=deviation.sku_id,
            cost_center=cost_center,
            amount_usd=rec_scenario.total_cost_usd,
        )
        healing_result = execute_healing_action(
            bq_client=bq_client,
            dataset_id=settings.BQ_DATASET,
            deviation=deviation,
            scenario=rec_scenario,
            status="AUTO_HEALED",
            workflow_root_id=workflow_root_id,
        )
    else:
        # Require HITL: release reservation hold until approval is received
        if decision.reservation_id:
            governor.release_reservation(decision.reservation_id)

    return {
        "deviation_id": deviation.deviation_id,
        "workflow_root_id": workflow_root_id,
        "status": "COMPLETED",
        "solver_sha256": scenario_set.result_sha256,
        "recommended_scenario_id": scenario_set.recommended_scenario_id,
        "narrative": narrative.narrative,
        "risk_summary": narrative.risk_summary,
        "okf_outcome": decision.outcome,
        "triggered_rules": decision.triggered_rules,
        "healing_action": healing_result,
        "traceparent": traceparent,
    }
