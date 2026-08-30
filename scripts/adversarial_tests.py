"""Sentinel Adversarial Governance & Security Test Suite (§8.8, §8.13, Gate 2).

Executes four rigorous adversarial security and policy tests:
    1. INJECTION TEST   (DEV-002) - Raw note carries prompt-injection/override -> Blocked at Guardrail.
    2. SMURFING TEST    (DEV-003) - 3 x $4,900 txns to SUP-01 with distinct keys -> 2nd trips VELOCITY_CAP.
    3. REPLAY TEST      (DEV-001) - Identical deviation redelivered with same key -> 2nd returns duplicate=True.
    4. CONCURRENCY TEST (I-4)     - In-flight reservation lease blocks concurrent breach before commit; release unblocks.

Unconditionally emits audit records across SENSE, SANITIZE, SCORE, POLICY, INTENT, OUTCOME per I-10.
"""

import sys
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, List

import google.auth
from google.auth.exceptions import RefreshError, DefaultCredentialsError
from google.oauth2 import credentials
from google.cloud import bigquery
from google.cloud import firestore

from config import settings
from contracts.models import Deviation, Scenario, ScenarioSet
from core.guardrail import sanitize
from core.solver import load_deviation_from_bq, solve_mitigation, write_scenarios_to_bq
from core.okf import OKFGovernor
from core.ledger import append_ledger_record, verify_chain
from core.heal import execute_healing_action, compute_idempotency_key
from scripts.reset_demo import reset_bigquery_tables, clear_firestore_leases_and_killswitch


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


def run_adversarial_tests() -> int:
    print("=" * 70)
    print("SENTINEL ADVERSARIAL TEST SUITE (Gate 2: Security & Policy Invariants)")
    print("=" * 70)
    print(f"Project ID: {settings.PROJECT_ID}")
    print(f"Dataset:    {settings.BQ_DATASET}")
    print(f"Region:     {settings.GCP_REGION}")
    print("-" * 70)

    creds = _get_credentials()
    bq_client = bigquery.Client(project=settings.PROJECT_ID, credentials=creds)
    try:
        fs_client = firestore.Client(project=settings.PROJECT_ID, credentials=creds)
    except Exception:
        fs_client = None

    # Step 0: Clean reset of dynamic tables to ensure zero counter pollution
    print("\n[SETUP] Initializing clean state for test run...")
    reset_bigquery_tables(bq_client, settings.BQ_DATASET)
    if fs_client:
        clear_firestore_leases_and_killswitch(fs_client)
    print("  • Dynamic tables truncated & counters zeroed.")

    governor = OKFGovernor(bq_client, settings.BQ_DATASET, fs_client)
    all_passed = True

    # -------------------------------------------------------------------------
    # TEST 1: Prompt Injection & Policy Override Evasion (DEV-002)
    # -------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("TEST 1: Prompt Injection & Override Attack (DEV-002)")
    print("=" * 70)
    dev_002, _, _ = load_deviation_from_bq(bq_client, settings.BQ_DATASET, "DEV-002")
    print(f"Inbound Raw Note: \"{dev_002.raw_note}\"")

    # Ingress SENSE record per I-10
    append_ledger_record(
        bq_client=bq_client,
        dataset_id=settings.BQ_DATASET,
        deviation_id="DEV-002",
        workflow_root_id="WF-DEV-002",
        phase="SENSE",
        payload={"raw_note": dev_002.raw_note, "sku_id": dev_002.sku_id, "magnitude_units": dev_002.magnitude_units},
    )

    guard_res = sanitize(dev_002.raw_note, direction="ingress")

    print(f"\nGuardrail Result:")
    print(f"  • Passed:             {guard_res.passed}")
    print(f"  • Injection Detected: {guard_res.injection}")
    print(f"  • Trigger Reason:     {guard_res.reason}")

    # SANITIZE record per I-10
    append_ledger_record(
        bq_client=bq_client,
        dataset_id=settings.BQ_DATASET,
        deviation_id="DEV-002",
        workflow_root_id="WF-DEV-002",
        phase="SANITIZE",
        payload={"passed": guard_res.passed, "injection": guard_res.injection, "reason": guard_res.reason},
        prompt_digest=guard_res.reason,
        okf_outcome="BLOCKED",
    )

    if not guard_res.passed and guard_res.injection:
        print("\n>>> TEST 1 RESULT: PASS [Blocked at Guardrail, Schema/Execution Rejected]")
    else:
        print("\n>>> TEST 1 RESULT: FAIL [Malicious prompt bypassed guardrail!]", file=sys.stderr)
        all_passed = False

    # -------------------------------------------------------------------------
    # TEST 2: Anti-Smurfing Velocity Cap Attack (DEV-003a, DEV-003b, DEV-003c)
    # -------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("TEST 2: Anti-Smurfing Multi-Dimensional Velocity Cap Attack (DEV-003)")
    print("=" * 70)
    print("Target: Supplier SUP-01 ($7,500 24h ceiling). Submitting 3 x $4,900 transactions...")

    smurfing_batches = ["DEV-003a", "DEV-003b", "DEV-003c"]
    batch_results = []

    for idx, dev_id in enumerate(smurfing_batches, start=1):
        dev, options, penalty = load_deviation_from_bq(bq_client, settings.BQ_DATASET, dev_id)

        # SENSE record per I-10
        append_ledger_record(
            bq_client=bq_client,
            dataset_id=settings.BQ_DATASET,
            deviation_id=dev_id,
            workflow_root_id=f"WF-{dev_id}",
            phase="SENSE",
            payload={"sku_id": dev.sku_id, "magnitude_units": dev.magnitude_units, "delay_days": dev.delay_days},
        )

        scenario_set = solve_mitigation(dev, options, penalty_rate_per_day=penalty)
        write_scenarios_to_bq(bq_client, settings.BQ_DATASET, scenario_set)  # Emits SCORE record
        rec_scenario = next(s for s in scenario_set.scenarios if s.scenario_id == scenario_set.recommended_scenario_id)

        # OKF Governor evaluation emits POLICY record per I-10
        decision = governor.evaluate(
            deviation=dev,
            scenario_set=scenario_set,
            recommended_scenario=rec_scenario,
            supplier_id="SUP-01",
            workflow_root_id=f"WF-{dev_id}",
        )

        triggered_str = ", ".join(decision.triggered_rules) if decision.triggered_rules else "NONE"
        supplier_used = decision.counters_snapshot.get("supplier", {}).get("used", "0.00")
        supplier_projected = decision.counters_snapshot.get("supplier", {}).get("projected", "0.00")
        supplier_ceiling = decision.counters_snapshot.get("supplier", {}).get("ceiling", "7500.00")

        print(f"\nBatch {idx}/3 ({dev_id}):")
        print(f"  • Amount:              ${rec_scenario.total_cost_usd}")
        print(f"  • Supplier 24h Spend:  ${supplier_used} -> Projected: ${supplier_projected} (Ceiling: ${supplier_ceiling})")
        print(f"  • OKF Outcome:         {decision.outcome}")
        print(f"  • Triggered Rules:     [{triggered_str}]")

        if decision.outcome == "AUTO_HEAL":
            # Commit transaction to spend_transactions table
            governor.commit_reservation(
                reservation_id=decision.reservation_id,
                workflow_root_id=f"WF-{dev_id}",
                tenant="SENTINEL_CORP",
                supplier_id="SUP-01",
                sku_id=dev.sku_id,
                cost_center="CC_LOGISTICS",
                amount_usd=rec_scenario.total_cost_usd,
            )
            heal_res = execute_healing_action(
                bq_client, settings.BQ_DATASET, dev, rec_scenario, status="AUTO_HEALED"
            )  # Emits INTENT and OUTCOME records
            print(f"  • Healing Action:      Executed (action_id: {heal_res['action_id']}, duplicate: {heal_res['duplicate']})")
        else:
            print(f"  • Healing Action:      HELD FOR HUMAN REVIEW (Auto-heal blocked by policy)")

        batch_results.append(decision)

    # Verification: Batch 1 is AUTO_HEAL, Batch 2 and 3 are REQUIRE_HITL with VELOCITY_CAP
    b1_ok = batch_results[0].outcome == "AUTO_HEAL"
    b2_ok = batch_results[1].outcome == "REQUIRE_HITL" and any("VELOCITY_CAP" in r for r in batch_results[1].triggered_rules)
    b3_ok = batch_results[2].outcome == "REQUIRE_HITL" and any("VELOCITY_CAP" in r for r in batch_results[2].triggered_rules)

    if b1_ok and b2_ok and b3_ok:
        print("\n>>> TEST 2 RESULT: PASS [Batch 1 Auto-Healed; Batch 2 & 3 Tripped VELOCITY_CAP:SUPPLIER]")
    else:
        print("\n>>> TEST 2 RESULT: FAIL [Velocity cap did not engage properly]", file=sys.stderr)
        all_passed = False

    # -------------------------------------------------------------------------
    # TEST 3: Duplicate Delivery & Replay Protection (DEV-001)
    # -------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("TEST 3: Duplicate Delivery & Replay Protection (DEV-001)")
    print("=" * 70)
    print("Submitting DEV-001 twice with identical parameters and idempotency key...")

    dev_001, opt_001, pen_001 = load_deviation_from_bq(bq_client, settings.BQ_DATASET, "DEV-001")

    # Delivery 1: SENSE, SCORE, POLICY, INTENT, OUTCOME
    append_ledger_record(
        bq_client=bq_client,
        dataset_id=settings.BQ_DATASET,
        deviation_id="DEV-001",
        workflow_root_id="WF-DEV-001",
        phase="SENSE",
        payload={"sku_id": dev_001.sku_id, "magnitude_units": dev_001.magnitude_units},
    )
    scen_set_001 = solve_mitigation(dev_001, opt_001, penalty_rate_per_day=pen_001)
    write_scenarios_to_bq(bq_client, settings.BQ_DATASET, scen_set_001)
    rec_001 = next(s for s in scen_set_001.scenarios if s.scenario_id == scen_set_001.recommended_scenario_id)

    print("\n[Delivery 1/2]:")
    dec_1 = governor.evaluate(dev_001, scen_set_001, rec_001, supplier_id="SUP-11", workflow_root_id="WF-DEV-001")
    print(f"  • OKF Outcome:     {dec_1.outcome}")
    heal_1 = execute_healing_action(bq_client, settings.BQ_DATASET, dev_001, rec_001, status="AUTO_HEALED")
    print(f"  • Action ID:       {heal_1['action_id']}")
    print(f"  • Duplicate Flag:  {heal_1['duplicate']}")
    print(f"  • Idempotency Key: {heal_1['idempotency_key']}")

    # Delivery 2 (Replay of exact same message): SENSE, INTENT, OUTCOME (duplicate: True)
    print("\n[Delivery 2/2 - Redelivered Message]:")
    append_ledger_record(
        bq_client=bq_client,
        dataset_id=settings.BQ_DATASET,
        deviation_id="DEV-001",
        workflow_root_id="WF-DEV-001",
        phase="SENSE",
        payload={"sku_id": dev_001.sku_id, "replay": True},
    )
    heal_2 = execute_healing_action(bq_client, settings.BQ_DATASET, dev_001, rec_001, status="AUTO_HEALED")
    print(f"  • Action ID:       {heal_2['action_id']}")
    print(f"  • Duplicate Flag:  {heal_2['duplicate']}")
    print(f"  • Idempotency Key: {heal_2['idempotency_key']}")

    if not heal_1["duplicate"] and heal_2["duplicate"] and heal_1["action_id"] == heal_2["action_id"]:
        print("\n>>> TEST 3 RESULT: PASS [1st Execution Succeeded; 2nd Replay Detected duplicate=True]")
    else:
        print("\n>>> TEST 3 RESULT: FAIL [Replay was not caught as a duplicate]", file=sys.stderr)
        all_passed = False

    # -------------------------------------------------------------------------
    # TEST 4: Concurrent In-Flight Reservation Lease Race (I-4)
    # -------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("TEST 4: Concurrent In-Flight Reservation Lease Race (I-4)")
    print("=" * 70)
    print("Proving reserve-then-commit: 2 concurrent workflows against SUP-02 ($4,900 each, $7,500 ceiling)")

    governor._in_memory_reservations.clear()

    # Define isolated test deviation & scenario for Test 4 ($4,900 on SKU-007 / SUP-02)
    dev_concur = Deviation(
        deviation_id="DEV-CONCUR",
        sku_id="SKU-007",
        dc_id="DC-EAST",
        deviation_type="ASN_DEVIATION",
        magnitude_units=100,
        delay_days=0,
        source_system="WMS",
        raw_note="Concurrent lease test deviation",
        detected_at=datetime.now(timezone.utc),
    )
    scen_concur = Scenario(
        scenario_id="SCEN-CONCUR-01",
        label="AIR_EXPEDITE",
        selected=[{"option_id": "OPT-CONCUR", "qty": 100, "cost_usd": 4900.0}],
        days_to_coverage=1,
        total_cost_usd=Decimal("4900.00"),
        sla_penalty_usd=Decimal("0.00"),
        total_exposure_usd=Decimal("4900.00"),
        feasible=True,
        solver_status="OPTIMAL",
    )
    scen_set_concur = ScenarioSet(
        deviation_id="DEV-CONCUR",
        scenarios=[scen_concur],
        recommended_scenario_id="SCEN-CONCUR-01",
        solve_ms=12,
        degraded=False,
        result_sha256="concur-test-sha256-hash",
    )

    # Workflow A reserves $4,900 against SUP-02
    print("\n[Workflow A - Evaluation & In-Flight Reservation]:")
    dec_wf_a = governor.evaluate(
        deviation=dev_concur,
        scenario_set=scen_set_concur,
        recommended_scenario=scen_concur,
        supplier_id="SUP-02",
        workflow_root_id="WF-CONCUR-A",
        tenant="TENANT-CONCUR",
    )
    print(f"  • Workflow A Outcome:        {dec_wf_a.outcome}")
    print(f"  • In-Flight Reservation ID:  {dec_wf_a.reservation_id}")
    print(f"  • Status:                    HELD IN PENDING (Uncommitted)")

    # Workflow B evaluates concurrently BEFORE Workflow A settles/commits
    print("\n[Workflow B - Concurrent Evaluation against SUP-02 while Lease A is Pending]:")
    dec_wf_b1 = governor.evaluate(
        deviation=dev_concur,
        scenario_set=scen_set_concur,
        recommended_scenario=scen_concur,
        supplier_id="SUP-02",
        workflow_root_id="WF-CONCUR-B",
        tenant="TENANT-CONCUR",
    )
    b1_supplier_used = dec_wf_b1.counters_snapshot.get("supplier", {}).get("used", "0.00")
    b1_supplier_projected = dec_wf_b1.counters_snapshot.get("supplier", {}).get("projected", "0.00")
    print(f"  • Committed + In-Flight Spend: ${b1_supplier_used}")
    print(f"  • Projected Spend:             ${b1_supplier_projected} (Ceiling: $7500.00)")
    print(f"  • Workflow B Outcome:          {dec_wf_b1.outcome}")
    print(f"  • Triggered Rules:             {dec_wf_b1.triggered_rules}")

    # Case 3: Workflow A is released/aborted -> Workflow B subsequently retries and passes
    print("\n[Workflow A - Reservation Released / Aborted]:")
    governor.release_reservation(dec_wf_a.reservation_id)
    print(f"  • Lease {dec_wf_a.reservation_id} released.")

    print("\n[Workflow B - Subsequent Retry after Lease Release]:")
    dec_wf_b2 = governor.evaluate(
        deviation=dev_concur,
        scenario_set=scen_set_concur,
        recommended_scenario=scen_concur,
        supplier_id="SUP-02",
        workflow_root_id="WF-CONCUR-B",
        tenant="TENANT-CONCUR",
    )
    b2_supplier_used = dec_wf_b2.counters_snapshot.get("supplier", {}).get("used", "0.00")
    b2_supplier_projected = dec_wf_b2.counters_snapshot.get("supplier", {}).get("projected", "0.00")
    print(f"  • Committed + In-Flight Spend: ${b2_supplier_used}")
    print(f"  • Projected Spend:             ${b2_supplier_projected} (Ceiling: $7500.00)")
    print(f"  • Workflow B Retry Outcome:    {dec_wf_b2.outcome}")
    print(f"  • Triggered Rules:             {dec_wf_b2.triggered_rules}")

    t4_ok = (
        dec_wf_a.outcome == "AUTO_HEAL"
        and dec_wf_b1.outcome == "REQUIRE_HITL"
        and any("VELOCITY_CAP" in r for r in dec_wf_b1.triggered_rules)
        and dec_wf_b2.outcome == "AUTO_HEAL"
    )

    if t4_ok:
        print("\n>>> TEST 4 RESULT: PASS [In-flight lease blocked concurrent breach; release unblocked retry]")
    else:
        print("\n>>> TEST 4 RESULT: FAIL [Concurrent lease isolation failed]", file=sys.stderr)
        all_passed = False

    # -------------------------------------------------------------------------
    # Audit Chain Verification
    # -------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("AUDIT CHAIN INTEGRITY CHECK (Post-Adversarial Tests)")
    print("=" * 70)
    chain_ok, broken_at, total_records = verify_chain(bq_client, settings.BQ_DATASET)
    print(f"Total Audit Records Written: {total_records}")
    print(f"Hash Chain Status:           {'Tamper-Evident (100% Verified)' if chain_ok else f'BROKEN ({broken_at})'}")

    print("\n" + "=" * 70)
    if all_passed and chain_ok:
        print("GATE 2 STATUS: GREEN (All 4 Adversarial, Concurrency & Security Tests PASSED)")
        print("=" * 70)
        return 0
    else:
        print("GATE 2 STATUS: RED (One or more adversarial tests failed)", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run_adversarial_tests())
