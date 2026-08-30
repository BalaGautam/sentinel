"""Sentinel Data Contracts (§7).

Defines Pydantic models for deviations, supply options, scenarios,
governor decisions, narratives, and ledger records.
"""

from pydantic import BaseModel, Field, condecimal
from typing import Literal, Optional
from datetime import datetime
from decimal import Decimal

Money = condecimal(max_digits=12, decimal_places=2)


class Deviation(BaseModel):
    deviation_id: str
    deviation_type: Literal["DEMAND_SPIKE", "PORT_DELAY", "ASN_DEVIATION", "SUPPLIER_SHORT"]
    sku_id: str
    dc_id: str
    magnitude_units: int = Field(ge=0)
    delay_days: int = Field(ge=0, le=365)
    source_system: str
    raw_note: str = Field(max_length=2000)  # UNTRUSTED — sanitize before any LLM sees it
    detected_at: datetime


class SupplyOption(BaseModel):
    option_id: str
    supplier_id: str
    mode: Literal["CONTRACT", "SPOT", "AIR_EXPEDITE", "DC_REBALANCE"]
    unit_price_usd: Money
    moq: int = Field(ge=0)
    max_qty: int = Field(ge=0)
    lead_time_days: int = Field(ge=0)
    fixed_fee_usd: Money = Decimal("0.00")


class Scenario(BaseModel):
    """Every field below is populated ONLY by solve_mitigation(). See I-1."""
    scenario_id: str
    label: Literal["STATUS_QUO", "AIR_EXPEDITE", "LINE_REBALANCE"]
    selected: list[dict]  # [{option_id, qty, cost_usd}]
    total_cost_usd: Optional[Money] = None
    sla_penalty_usd: Optional[Money] = None
    total_exposure_usd: Optional[Money] = None
    days_to_coverage: Optional[int] = None
    feasible: bool
    solver_status: Literal["OPTIMAL", "FEASIBLE", "HEURISTIC_FALLBACK", "INFEASIBLE"]


class ScenarioSet(BaseModel):
    deviation_id: str
    scenarios: list[Scenario]
    recommended_scenario_id: str  # chosen by the SOLVER (min exposure), never by the LLM
    solve_ms: int
    degraded: bool  # True -> forces REQUIRE_HITL (I-11)
    result_sha256: str


class OrchestratorNarrative(BaseModel):
    """The ONLY structure an LLM may emit. Note the absence of numeric fields. See I-1."""
    narrative: str = Field(max_length=600)
    risk_summary: str = Field(max_length=300)


class OKFDecision(BaseModel):
    outcome: Literal["AUTO_HEAL", "REQUIRE_HITL", "BLOCKED"]
    triggered_rules: list[str]
    amount_usd: Money
    counters_snapshot: dict  # {dimension: {used, ceiling}}
    reservation_id: Optional[str]


class LedgerRecord(BaseModel):
    record_id: str
    deviation_id: str
    workflow_root_id: str
    phase: Literal["SENSE", "SANITIZE", "SCENARIO", "SCORE", "POLICY", "INTENT", "OUTCOME", "APPROVAL"]
    payload_sha256: str
    prompt_digest: Optional[str] = None
    solver_result_sha256: Optional[str] = None
    okf_outcome: Optional[str] = None
    operator_sub: Optional[str] = None
    operator_jti: Optional[str] = None
    approval_signature: Optional[str] = None
    prev_record_hash: str
    record_hash: str
    created_at: datetime
