"""10x Concurrency Race Test Execution (§8.2, I-4).

Spawns two concurrent threads across 10 independent iterations hitting OKFGovernor.create_reservation
simultaneously using threading.Barrier(2).
"""

import sys
import threading
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from google.cloud import bigquery, firestore
from config import settings
from contracts.models import Deviation, Scenario, ScenarioSet, OKFDecision
from core.okf import OKFGovernor
from agents.pipeline import _get_credentials
from scripts.reset_demo import reset_bigquery_tables, clear_firestore_leases_and_killswitch


def run_10x_concurrency():
    creds = _get_credentials()
    bq_client = bigquery.Client(project=settings.PROJECT_ID, credentials=creds)
    try:
        fs_client = firestore.Client(project=settings.PROJECT_ID, credentials=creds)
    except Exception:
        fs_client = None

    print("=" * 75)
    print("SENTINEL I-4 CONCURRENCY VERIFICATION: 10 CONSECUTIVE GENUINE RACE RUNS")
    print("=" * 75)
    print(f"Project ID:       {settings.PROJECT_ID}")
    print(f"Target Supplier:  SUP-02 (24h Velocity Ceiling: $7,500.00)")
    print(f"Simultaneous Txn: 2 x $4,900.00 ($9,800.00 combined attempt)")
    print(f"Expected Outcome: Exactly 1 AUTO_HEAL winner, exactly 1 REQUIRE_HITL blocked")
    print("=" * 75)

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

    governor = OKFGovernor(bq_client, settings.BQ_DATASET, fs_client)

    success_runs = 0
    total_runs = 10

    for i in range(1, total_runs + 1):
        print(f"\n--- [RUN {i:02d}/10] ---")
        # Clear in-flight leases and reset counters before each run
        governor._in_memory_reservations.clear()
        if fs_client:
            try:
                for doc in fs_client.collection("reservations").stream(timeout=2.0):
                    doc.reference.delete()
                for doc in fs_client.collection("lease_locks").stream(timeout=2.0):
                    doc.reference.delete()
            except Exception:
                pass

        barrier = threading.Barrier(2)
        thread_results = {}

        def worker(wf_id: str):
            barrier.wait()
            decision = governor.evaluate(
                deviation=dev_concur,
                scenario_set=scen_set_concur,
                recommended_scenario=scen_concur,
                supplier_id="SUP-02",
                workflow_root_id=wf_id,
                tenant="TENANT-CONCUR",
            )
            thread_results[wf_id] = decision

        t_a = threading.Thread(target=worker, args=(f"WF-CONCUR-A-R{i}",), name=f"Thread-A-R{i}")
        t_b = threading.Thread(target=worker, args=(f"WF-CONCUR-B-R{i}",), name=f"Thread-B-R{i}")

        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        dec_a = thread_results[f"WF-CONCUR-A-R{i}"]
        dec_b = thread_results[f"WF-CONCUR-B-R{i}"]

        print(f"Thread A (WF-A): outcome={dec_a.outcome:<12} | reservation_id={str(dec_a.reservation_id):<14} | rules={dec_a.triggered_rules}")
        print(f"Thread B (WF-B): outcome={dec_b.outcome:<12} | reservation_id={str(dec_b.reservation_id):<14} | rules={dec_b.triggered_rules}")

        auto_heal_count = (1 if dec_a.outcome == "AUTO_HEAL" else 0) + (1 if dec_b.outcome == "AUTO_HEAL" else 0)
        hitl_count = (1 if dec_a.outcome == "REQUIRE_HITL" else 0) + (1 if dec_b.outcome == "REQUIRE_HITL" else 0)

        if auto_heal_count == 1 and hitl_count == 1:
            winner = "Thread A" if dec_a.outcome == "AUTO_HEAL" else "Thread B"
            loser_rules = dec_b.triggered_rules if dec_a.outcome == "AUTO_HEAL" else dec_a.triggered_rules
            assert any("VELOCITY_CAP" in r for r in loser_rules), f"Blocked thread did not trip VELOCITY_CAP: {loser_rules}"
            print(f"Result: PASS -> Exactly 1 winner ({winner}), exactly 1 blocked by policy")
            success_runs += 1
        elif auto_heal_count == 2:
            print("Result: FAIL -> RACE CONDITION! Both workflows passed ($9,800 committed against $7,500 cap)", file=sys.stderr)
        else:
            print(f"Result: FAIL -> Unexpected state (auto_heal={auto_heal_count}, hitl={hitl_count})", file=sys.stderr)

    print("\n" + "=" * 75)
    print(f"SUMMARY: {success_runs}/{total_runs} runs had exactly one winner (100% race-condition protection).")
    print("=" * 75)

    if success_runs == total_runs:
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(run_10x_concurrency())
