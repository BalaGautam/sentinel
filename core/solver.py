"""Sentinel Optimization Solver (§8.1, I-1, I-2, I-10, I-11).

Formulates and solves a Mixed-Integer Linear Program (MILP) using OR-Tools (CBC)
to generate three scored mitigation scenarios for a supply chain deviation.

Scenarios:
    1. STATUS_QUO     - Restricted to CONTRACT options only
    2. AIR_EXPEDITE   - CONTRACT, SPOT, AIR_EXPEDITE options
    3. LINE_REBALANCE - CONTRACT, DC_REBALANCE options

Invariants:
    - I-1: Pure deterministic optimization; no LLM authors or modifies numbers.
    - I-2: Result SHA-256 computed over canonical JSON with sort_keys=True.
    - I-10: Strict input bound (reject > 25 supply options).
    - I-11: 2000ms solver time limit with heuristic fallback (forces degraded=True).
"""

import sys
import time
import json
import hashlib
import argparse
from typing import List, Dict, Tuple, Optional, Any
from decimal import Decimal
from datetime import datetime, timezone
import subprocess

import google.auth
from google.auth.exceptions import RefreshError, DefaultCredentialsError
from google.oauth2 import credentials
from google.cloud import bigquery
from ortools.linear_solver import pywraplp

from config import settings
from contracts.models import Deviation, SupplyOption, Scenario, ScenarioSet

# Scenario Mode Mapping (§8.1)
SCENARIO_MODES: Dict[str, set] = {
    "STATUS_QUO": {"CONTRACT"},
    "AIR_EXPEDITE": {"CONTRACT", "SPOT", "AIR_EXPEDITE"},
    "LINE_REBALANCE": {"CONTRACT", "DC_REBALANCE"},
}

MAX_SUPPLY_OPTIONS: int = 25
SOLVER_TIME_LIMIT_MS: int = 2000


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


def _solve_scenario_milp(
    scenario_id: str,
    label: str,
    allowed_modes: set,
    options: List[SupplyOption],
    magnitude_units: int,
    required_day: int,
    penalty_rate_per_day: Decimal,
) -> Scenario:
    """Solve MILP formulation for a single scenario with specified mode gating."""
    filtered_options = [opt for opt in options if opt.mode in allowed_modes]
    
    if not filtered_options:
        return Scenario(
            scenario_id=scenario_id,
            label=label,
            selected=[],
            total_cost_usd=None,
            sla_penalty_usd=None,
            total_exposure_usd=None,
            days_to_coverage=None,
            feasible=False,
            solver_status="INFEASIBLE",
        )

    # Check aggregate capacity before setting up MILP
    total_capacity = sum(opt.max_qty for opt in filtered_options)
    if total_capacity < magnitude_units:
        return Scenario(
            scenario_id=scenario_id,
            label=label,
            selected=[],
            total_cost_usd=None,
            sla_penalty_usd=None,
            total_exposure_usd=None,
            days_to_coverage=None,
            feasible=False,
            solver_status="INFEASIBLE",
        )

    # Create OR-Tools CBC MILP solver
    solver = pywraplp.Solver.CreateSolver("CBC")
    if not solver:
        solver = pywraplp.Solver.CreateSolver("CBC_MIXED_INTEGER_PROGRAMMING")
    if not solver:
        # Fallback to SCIP if CBC is unavailable
        solver = pywraplp.Solver.CreateSolver("SCIP")

    solver.SetTimeLimit(SOLVER_TIME_LIMIT_MS)

    n = len(filtered_options)
    qty_vars = []
    use_vars = []

    for i, opt in enumerate(filtered_options):
        qty_vars.append(solver.IntVar(0, int(opt.max_qty), f"qty_{i}"))
        use_vars.append(solver.BoolVar(f"use_{i}"))

    coverage_day = solver.NumVar(0.0, solver.infinity(), "coverage_day")
    sla_slack = solver.NumVar(0.0, solver.infinity(), "sla_slack")

    # Demand constraint: sum(qty) >= magnitude_units
    solver.Add(solver.Sum(qty_vars) >= magnitude_units)

    # Big-M linkage: moq * use <= qty <= max_qty * use
    for i, opt in enumerate(filtered_options):
        solver.Add(qty_vars[i] <= int(opt.max_qty) * use_vars[i])
        solver.Add(qty_vars[i] >= int(opt.moq) * use_vars[i])

    # Lead time coverage day linkage
    for i, opt in enumerate(filtered_options):
        solver.Add(coverage_day >= int(opt.lead_time_days) * use_vars[i])

    # Linearized SLA delay: sla_slack >= coverage_day - required_day
    solver.Add(sla_slack >= coverage_day - float(required_day))
    solver.Add(sla_slack >= 0.0)

    # Objective: Minimize sum(unit_price * qty + fixed_fee * use) + penalty_rate_per_day * sla_slack
    objective_terms = []
    for i, opt in enumerate(filtered_options):
        objective_terms.append(float(opt.unit_price_usd) * qty_vars[i])
        objective_terms.append(float(opt.fixed_fee_usd) * use_vars[i])

    if penalty_rate_per_day > 0:
        objective_terms.append(float(penalty_rate_per_day) * sla_slack)

    solver.Minimize(solver.Sum(objective_terms))

    status = solver.Solve()

    if status in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        selected = []
        max_lead_time = 0
        total_cost = Decimal("0.00")

        for i, opt in enumerate(filtered_options):
            q = int(round(qty_vars[i].solution_value()))
            if q > 0:
                cost = (Decimal(str(opt.unit_price_usd)) * Decimal(str(q))) + Decimal(str(opt.fixed_fee_usd))
                total_cost += cost
                if opt.lead_time_days > max_lead_time:
                    max_lead_time = opt.lead_time_days
                selected.append({
                    "option_id": opt.option_id,
                    "qty": q,
                    "cost_usd": str(cost.quantize(Decimal("0.01"))),
                })

        days_to_coverage = max_lead_time
        delay_days = max(0, days_to_coverage - required_day)
        sla_penalty = (Decimal(str(penalty_rate_per_day)) * Decimal(str(delay_days))).quantize(Decimal("0.01"))
        total_exposure = (total_cost + sla_penalty).quantize(Decimal("0.01"))

        return Scenario(
            scenario_id=scenario_id,
            label=label,
            selected=selected,
            total_cost_usd=total_cost.quantize(Decimal("0.01")),
            sla_penalty_usd=sla_penalty,
            total_exposure_usd=total_exposure,
            days_to_coverage=days_to_coverage,
            feasible=True,
            solver_status="OPTIMAL" if status == pywraplp.Solver.OPTIMAL else "FEASIBLE",
        )

    # On timeout or solver failure, fall back to heuristic (§8.1)
    return _heuristic_fallback(
        scenario_id=scenario_id,
        label=label,
        filtered_options=filtered_options,
        magnitude_units=magnitude_units,
        required_day=required_day,
        penalty_rate_per_day=penalty_rate_per_day,
    )


def _heuristic_fallback(
    scenario_id: str,
    label: str,
    filtered_options: List[SupplyOption],
    magnitude_units: int,
    required_day: int,
    penalty_rate_per_day: Decimal,
) -> Scenario:
    """Cheapest-first-meeting-lead-time greedy heuristic on solver timeout/failure."""
    sorted_options = sorted(
        filtered_options,
        key=lambda o: (
            0 if o.lead_time_days <= required_day else 1,
            float(o.unit_price_usd) + (float(o.fixed_fee_usd) / max(1, o.moq)),
            o.lead_time_days,
        ),
    )

    remaining = magnitude_units
    selected = []
    total_cost = Decimal("0.00")
    max_lead_time = 0

    for opt in sorted_options:
        if remaining <= 0:
            break
        if opt.max_qty < opt.moq:
            continue
        qty_to_take = min(max(remaining, opt.moq), opt.max_qty)
        if qty_to_take < opt.moq:
            continue

        cost = (Decimal(str(opt.unit_price_usd)) * Decimal(str(qty_to_take))) + Decimal(str(opt.fixed_fee_usd))
        total_cost += cost
        if opt.lead_time_days > max_lead_time:
            max_lead_time = opt.lead_time_days

        selected.append({
            "option_id": opt.option_id,
            "qty": int(qty_to_take),
            "cost_usd": str(cost.quantize(Decimal("0.01"))),
        })
        remaining -= qty_to_take

    if remaining <= 0:
        days_to_coverage = max_lead_time
        delay_days = max(0, days_to_coverage - required_day)
        sla_penalty = (Decimal(str(penalty_rate_per_day)) * Decimal(str(delay_days))).quantize(Decimal("0.01"))
        total_exposure = (total_cost + sla_penalty).quantize(Decimal("0.01"))

        return Scenario(
            scenario_id=scenario_id,
            label=label,
            selected=selected,
            total_cost_usd=total_cost.quantize(Decimal("0.01")),
            sla_penalty_usd=sla_penalty,
            total_exposure_usd=total_exposure,
            days_to_coverage=days_to_coverage,
            feasible=True,
            solver_status="HEURISTIC_FALLBACK",
        )

    return Scenario(
        scenario_id=scenario_id,
        label=label,
        selected=[],
        total_cost_usd=None,
        sla_penalty_usd=None,
        total_exposure_usd=None,
        days_to_coverage=None,
        feasible=False,
        solver_status="INFEASIBLE",
    )


def solve_mitigation(
    deviation: Deviation,
    supply_options: List[SupplyOption],
    penalty_rate_per_day: Decimal = Decimal("0.00"),
    required_day: Optional[int] = None,
) -> ScenarioSet:
    """Generate and score three mitigation scenarios for a deviation (§8.1)."""
    start_time = time.perf_counter()

    # Input guard per I-10 (§8.1)
    if len(supply_options) > MAX_SUPPLY_OPTIONS:
        raise ValueError(
            f"Input supply options ({len(supply_options)}) exceed maximum allowed limit of {MAX_SUPPLY_OPTIONS}"
        )

    if required_day is None:
        required_day = deviation.delay_days

    scenarios: List[Scenario] = []
    scenario_order = ["STATUS_QUO", "AIR_EXPEDITE", "LINE_REBALANCE"]

    for idx, label in enumerate(scenario_order, start=1):
        scenario_id = f"{deviation.deviation_id}-SCEN-{idx:02d}"
        allowed_modes = SCENARIO_MODES[label]
        scenario = _solve_scenario_milp(
            scenario_id=scenario_id,
            label=label,
            allowed_modes=allowed_modes,
            options=supply_options,
            magnitude_units=deviation.magnitude_units,
            required_day=required_day,
            penalty_rate_per_day=penalty_rate_per_day,
        )
        scenarios.append(scenario)

    # Evaluate degraded state (§8.1, I-11)
    degraded = any(s.solver_status == "HEURISTIC_FALLBACK" for s in scenarios)

    # Compute recommended_scenario_id in Python: min total_exposure_usd among feasible scenarios
    feasible_scenarios = [s for s in scenarios if s.feasible]
    if feasible_scenarios:
        best_scenario = min(
            feasible_scenarios,
            key=lambda s: (s.total_exposure_usd, s.days_to_coverage, s.total_cost_usd),
        )
        recommended_id = best_scenario.scenario_id
    else:
        recommended_id = scenarios[0].scenario_id

    solve_ms = int((time.perf_counter() - start_time) * 1000)

    # Compute deterministic result_sha256 over canonical JSON (§8.1, I-2)
    canonical = {
        "deviation_id": deviation.deviation_id,
        "scenarios": [
            {
                "scenario_id": s.scenario_id,
                "label": s.label,
                "selected": [
                    {
                        "option_id": item["option_id"],
                        "qty": int(item["qty"]),
                        "cost_usd": str(item["cost_usd"]),
                    }
                    for item in s.selected
                ],
                "total_cost_usd": str(s.total_cost_usd) if s.total_cost_usd is not None else None,
                "sla_penalty_usd": str(s.sla_penalty_usd) if s.sla_penalty_usd is not None else None,
                "total_exposure_usd": str(s.total_exposure_usd) if s.total_exposure_usd is not None else None,
                "days_to_coverage": s.days_to_coverage,
                "feasible": s.feasible,
                "solver_status": s.solver_status,
            }
            for s in scenarios
        ],
        "recommended_scenario_id": recommended_id,
        "degraded": degraded,
    }

    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    result_sha256 = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    return ScenarioSet(
        deviation_id=deviation.deviation_id,
        scenarios=scenarios,
        recommended_scenario_id=recommended_id,
        solve_ms=solve_ms,
        degraded=degraded,
        result_sha256=result_sha256,
    )


# -----------------------------------------------------------------------------
# Data Ingestion & BigQuery Persistence
# -----------------------------------------------------------------------------

def load_deviation_from_bq(
    bq_client: bigquery.Client, dataset_id: str, deviation_id: str
) -> Tuple[Deviation, List[SupplyOption], Decimal]:
    """Load deviation, matching supply options, and customer order SLA penalty from BigQuery."""
    # 1. Fetch Deviation
    dev_query = f"""
    SELECT deviation_id, deviation_type, sku_id, dc_id, magnitude_units, delay_days, source_system, raw_note, detected_at
    FROM `{bq_client.project}.{dataset_id}.deviations`
    WHERE deviation_id = @dev_id
    LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("dev_id", "STRING", deviation_id)]
    )
    dev_rows = list(bq_client.query(dev_query, job_config=job_config).result())
    if not dev_rows:
        raise ValueError(f"Deviation '{deviation_id}' not found in dataset '{dataset_id}'")

    d = dev_rows[0]
    deviation = Deviation(
        deviation_id=d["deviation_id"],
        deviation_type=d["deviation_type"],
        sku_id=d["sku_id"],
        dc_id=d["dc_id"],
        magnitude_units=d["magnitude_units"],
        delay_days=d["delay_days"],
        source_system=d["source_system"],
        raw_note=d["raw_note"] or "",
        detected_at=d["detected_at"],
    )

    # 2. Fetch Supply Options for SKU
    opt_query = f"""
    SELECT option_id, supplier_id, sku_id, mode, unit_price_usd, moq, max_qty, lead_time_days, fixed_fee_usd
    FROM `{bq_client.project}.{dataset_id}.supply_options`
    WHERE sku_id = @sku_id
    ORDER BY option_id
    """
    job_config_opt = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("sku_id", "STRING", deviation.sku_id)]
    )
    opt_rows = list(bq_client.query(opt_query, job_config=job_config_opt).result())
    supply_options = [
        SupplyOption(
            option_id=r["option_id"],
            supplier_id=r["supplier_id"],
            sku_id=r["sku_id"],
            mode=r["mode"],
            unit_price_usd=Decimal(str(r["unit_price_usd"])),
            moq=r["moq"],
            max_qty=r["max_qty"],
            lead_time_days=r["lead_time_days"],
            fixed_fee_usd=Decimal(str(r["fixed_fee_usd"])),
        )
        for r in opt_rows
    ]

    # 3. Fetch Customer Order SLA Penalty Rate
    sla_query = f"""
    SELECT COALESCE(SUM(sla_penalty_rate_usd_per_day), 0) AS total_sla_rate
    FROM `{bq_client.project}.{dataset_id}.customer_orders`
    WHERE sku_id = @sku_id AND dc_id = @dc_id
    """
    job_config_sla = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("sku_id", "STRING", deviation.sku_id),
            bigquery.ScalarQueryParameter("dc_id", "STRING", deviation.dc_id),
        ]
    )
    sla_rows = list(bq_client.query(sla_query, job_config=job_config_sla).result())
    penalty_rate = Decimal(str(sla_rows[0]["total_sla_rate"])) if sla_rows else Decimal("0.00")

    return deviation, supply_options, penalty_rate


def write_scenarios_to_bq(
    bq_client: bigquery.Client, dataset_id: str, scenario_set: ScenarioSet
) -> None:
    """Write every scenario to scenario_library BigQuery table and log SCORE to audit ledger (§8.1, I-10)."""
    table_ref = f"{bq_client.project}.{dataset_id}.scenario_library"
    rows_to_insert = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for s in scenario_set.scenarios:
        rows_to_insert.append({
            "scenario_id": s.scenario_id,
            "deviation_id": scenario_set.deviation_id,
            "label": s.label,
            "selected_options_json": json.dumps(s.selected),
            "total_cost_usd": str(s.total_cost_usd) if s.total_cost_usd is not None else None,
            "sla_penalty_usd": str(s.sla_penalty_usd) if s.sla_penalty_usd is not None else None,
            "total_exposure_usd": str(s.total_exposure_usd) if s.total_exposure_usd is not None else None,
            "days_to_coverage": s.days_to_coverage,
            "feasible": s.feasible,
            "solver_status": s.solver_status,
            "result_sha256": scenario_set.result_sha256,
            "created_at": now_iso,
        })

    try:
        errors = bq_client.insert_rows_json(table_ref, rows_to_insert)
        if errors:
            raise RuntimeError(f"Failed writing scenarios to {table_ref}: {errors}")
    except Exception:
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
        )
        job = bq_client.load_table_from_json(rows_to_insert, table_ref, job_config=job_config)
        job.result()

    # Emit SCORE record into audit ledger per I-10
    try:
        from core.ledger import append_ledger_record
        append_ledger_record(
            bq_client=bq_client,
            dataset_id=dataset_id,
            deviation_id=scenario_set.deviation_id,
            workflow_root_id=f"WF-{scenario_set.deviation_id}",
            phase="SCORE",
            payload={
                "recommended_scenario_id": scenario_set.recommended_scenario_id,
                "solve_ms": scenario_set.solve_ms,
                "degraded": scenario_set.degraded,
                "result_sha256": scenario_set.result_sha256,
                "scenario_count": len(scenario_set.scenarios),
            },
            solver_result_sha256=scenario_set.result_sha256,
        )
    except Exception as e:
        print(f"Warning: Failed to append SCORE audit record: {e}", file=sys.stderr)


# -----------------------------------------------------------------------------
# CLI Entrypoint (§8.1, §8.8)
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Sentinel Mitigation Scenario Solver (§8.1)")
    parser.add_argument(
        "--deviation",
        "-d",
        required=True,
        help="Deviation ID to solve (e.g. DEV-001, DEV-003a, DEV-004)",
    )
    args = parser.parse_args()

    creds = _get_credentials()
    bq_client = bigquery.Client(project=settings.PROJECT_ID, credentials=creds)

    try:
        deviation, supply_options, penalty_rate = load_deviation_from_bq(
            bq_client, settings.BQ_DATASET, args.deviation
        )

        scenario_set = solve_mitigation(
            deviation=deviation,
            supply_options=supply_options,
            penalty_rate_per_day=penalty_rate,
        )

        # Persist every scenario to scenario_library (§8.1)
        write_scenarios_to_bq(bq_client, settings.BQ_DATASET, scenario_set)

        # Print canonical JSON output to stdout
        output_dict = scenario_set.model_dump(mode="json")
        print(json.dumps(output_dict, indent=2))
        return 0

    except Exception as e:
        print(f"ERROR: Solver failed for deviation '{args.deviation}': {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
