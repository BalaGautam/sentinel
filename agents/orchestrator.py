"""Sentinel Triage Orchestrator Agent (§5, I-1, I-2, I-10, I-12).

Orchestrates the mitigation workflow:
1. Queries ATP and customer order commitments.
2. Retrieves computed memory (supplier reliability).
3. Invokes Sourcing Specialist sub-agent for qualitative options analysis.
4. Invokes deterministic MILP solver for 3 scored mitigation scenarios.
5. Produces OrchestratorNarrative (strictly non-numeric per I-1).
6. Enforces egress guardrail sanitization on all tool returns and agent narratives (I-12).
"""

import json
from decimal import Decimal
from typing import Dict, Any, Tuple, Optional, List
from google.cloud import bigquery
from google.genai import types
from google.adk.agents.llm_agent import LlmAgent as Agent

from config import settings
from contracts.models import Deviation, ScenarioSet, Scenario, SupplyOption, OrchestratorNarrative
from core.guardrail import sanitize, GuardrailResult
from core.solver import load_deviation_from_bq, solve_mitigation, write_scenarios_to_bq
from agents.factory import build_gemini_model, get_genai_client
from agents.sourcing import create_sourcing_specialist_agent


def create_triage_orchestrator_tools(bq_client: bigquery.Client, dataset_id: str):
    """Create tool callables wired with egress guardrail sanitization (I-12)."""

    def memory_read(supplier_id: str) -> str:
        """Read computed supplier reliability metrics from Vertex AI Memory Bank, with BigQuery fallback (I-8)."""
        import sys
        from core.memory import read_supplier_reliability_memory

        # 1. Primary: Vertex AI Memory Bank (I-8)
        mem_data = read_supplier_reliability_memory(supplier_id)
        if mem_data:
            print(f"[Memory Bank Read] Retrieved reliability for {supplier_id} from Vertex AI Memory Bank: {mem_data}", file=sys.stderr)
            raw_res = json.dumps({
                "supplier_id": mem_data["supplier_id"],
                "on_time_rate_90d": mem_data["on_time_rate_90d"],
                "avg_lead_time_drift_days": mem_data["avg_lead_time_drift_days"],
                "quote_variance_rate": mem_data["quote_variance_rate"],
                "sample_size": mem_data["sample_size"],
                "provenance": mem_data.get("provenance", "computed"),
                "source": "VERTEX_AI_MEMORY_BANK",
            })
            sanitized = sanitize(raw_res, direction="egress")
            return sanitized.clean_text

        # 2. Fallback: BigQuery supplier_reliability table
        print(f"[Memory Read Warning] Vertex AI Memory Bank unavailable for {supplier_id}. Triggering BigQuery fallback...", file=sys.stderr)
        query = f"""
        SELECT supplier_id, on_time_rate_90d, avg_lead_time_drift_days, quote_variance_rate, sample_size, provenance
        FROM `{bq_client.project}.{dataset_id}.supplier_reliability`
        WHERE supplier_id = @sup_id
        LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("sup_id", "STRING", supplier_id)]
        )
        try:
            rows = list(bq_client.query(query, job_config=job_config).result())
            if rows:
                r = rows[0]
                raw_res = json.dumps({
                    "supplier_id": r["supplier_id"],
                    "on_time_rate_90d": float(r["on_time_rate_90d"]) if r["on_time_rate_90d"] is not None else None,
                    "avg_lead_time_drift_days": float(r["avg_lead_time_drift_days"]) if r["avg_lead_time_drift_days"] is not None else 0.0,
                    "quote_variance_rate": float(r["quote_variance_rate"]) if r["quote_variance_rate"] is not None else 0.0,
                    "sample_size": r["sample_size"],
                    "provenance": r["provenance"],
                    "source": "BIGQUERY_FALLBACK",
                })
            else:
                raw_res = f"Supplier {supplier_id} not found in reliability records."
        except Exception as e:
            raw_res = f"Error reading supplier reliability from BigQuery fallback: {e}"

        # Egress sanitization (I-12)
        sanitized = sanitize(raw_res, direction="egress")
        return sanitized.clean_text

    def atp_query(sku_id: str, dc_id: str) -> str:
        """Query available-to-promise and customer order commitments."""
        query = f"""
        SELECT order_id, customer_tier, qty, promise_date, sla_penalty_per_day_usd
        FROM `{bq_client.project}.{dataset_id}.customer_orders`
        WHERE sku_id = @sku_id AND dc_id = @dc_id
        LIMIT 10
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("sku_id", "STRING", sku_id),
                bigquery.ScalarQueryParameter("dc_id", "STRING", dc_id),
            ]
        )
        try:
            rows = list(bq_client.query(query, job_config=job_config).result())
            orders = [
                {
                    "order_id": r["order_id"],
                    "customer_tier": r["customer_tier"],
                    "qty": r["qty"],
                    "promise_date": str(r["promise_date"]),
                    "sla_penalty_per_day_usd": float(r["sla_penalty_per_day_usd"]) if r["sla_penalty_per_day_usd"] is not None else 0.0,
                }
                for r in rows
            ]
            raw_res = json.dumps({"orders_found": len(orders), "orders": orders})
        except Exception as e:
            raw_res = f"Error querying ATP: {e}"

        # Egress sanitization (I-12)
        sanitized = sanitize(raw_res, direction="egress")
        return sanitized.clean_text

    return memory_read, atp_query


def create_triage_orchestrator_agent(bq_client: bigquery.Client, dataset_id: str) -> Agent:
    """Instantiate the Triage Orchestrator ADK agent (§5)."""
    memory_read, atp_query = create_triage_orchestrator_tools(bq_client, dataset_id)
    sourcing_agent = create_sourcing_specialist_agent()

    instruction = (
        "You are the Sentinel Triage Orchestrator. When a supply chain deviation occurs, "
        "you coordinate analysis between supplier reliability, customer commitments, and "
        "sourcing strategies. Provide a clear, non-numeric business narrative explaining the "
        "situation and a risk summary. You must NEVER author, modify, or output numbers."
    )

    return Agent(
        name="triage_orchestrator",
        description="Coordinates deviation triage and synthesizes non-numeric narrative.",
        model=build_gemini_model(),
        instruction=instruction,
        sub_agents=[sourcing_agent],
        tools=[memory_read, atp_query],
        output_schema=OrchestratorNarrative,
    )


class TriageOrchestrator:
    """Executive runner for the Triage Orchestrator workflow."""

    def __init__(self, bq_client: bigquery.Client, dataset_id: str):
        self.bq_client = bq_client
        self.dataset_id = dataset_id
        self.agent = create_triage_orchestrator_agent(bq_client, dataset_id)
        self.genai_client = get_genai_client()

    def orchestrate(
        self,
        deviation: Deviation,
        workflow_root_id: Optional[str] = None,
    ) -> Tuple[ScenarioSet, OrchestratorNarrative]:
        """Execute deterministic solver + qualitative LLM synthesis (I-1, I-2, I-10, I-12)."""
        wf_id = workflow_root_id or f"WF-{deviation.deviation_id}"

        # 1. Fetch Supply Options & SLA Penalty Rate directly
        opt_query = f"""
        SELECT option_id, supplier_id, sku_id, mode, unit_price_usd, moq, max_qty, lead_time_days, fixed_fee_usd
        FROM `{self.bq_client.project}.{self.dataset_id}.supply_options`
        WHERE sku_id = @sku_id
        ORDER BY option_id
        """
        opt_rows = list(self.bq_client.query(opt_query, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("sku_id", "STRING", deviation.sku_id)]
        )).result())
        options = [
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

        sla_query = f"""
        SELECT COALESCE(SUM(sla_penalty_rate_usd_per_day), 0) AS total_sla_rate
        FROM `{self.bq_client.project}.{self.dataset_id}.customer_orders`
        WHERE sku_id = @sku_id AND dc_id = @dc_id
        """
        sla_rows = list(self.bq_client.query(sla_query, job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("sku_id", "STRING", deviation.sku_id),
                bigquery.ScalarQueryParameter("dc_id", "STRING", deviation.dc_id),
            ]
        )).result())
        penalty_rate = Decimal(str(sla_rows[0]["total_sla_rate"])) if sla_rows else Decimal("0.00")

        # 2. Deterministic MILP solve (I-1, I-2)
        scenario_set = solve_mitigation(
            deviation=deviation,
            supply_options=options,
            penalty_rate_per_day=penalty_rate,
        )

        # 3. Write scenarios to BigQuery scenario_library + log SCORE to audit_ledger (§8.1, I-10)
        write_scenarios_to_bq(self.bq_client, self.dataset_id, scenario_set)

        # 4. Retrieve qualitative supplier context from Memory Bank (I-8)
        rec_scenario = next(
            s for s in scenario_set.scenarios if s.scenario_id == scenario_set.recommended_scenario_id
        )
        selected_mode = rec_scenario.label

        supplier_id = "SUP-11"
        if rec_scenario.selected and "option_id" in rec_scenario.selected[0]:
            opt_id = rec_scenario.selected[0]["option_id"]
            matched_opts = [o for o in options if o.option_id == opt_id]
            if matched_opts:
                supplier_id = matched_opts[0].supplier_id

        # Query supplier reliability memory from Vertex AI Memory Bank (I-8)
        memory_read_fn, _ = create_triage_orchestrator_tools(self.bq_client, self.dataset_id)
        supplier_reliability_context = memory_read_fn(supplier_id)

        # 5. Generate structured OrchestratorNarrative (enforcing I-1)
        prompt = (
            f"Synthesize an operational mitigation narrative and risk summary for deviation "
            f"'{deviation.deviation_id}' affecting SKU '{deviation.sku_id}' at facility '{deviation.dc_id}'. "
            f"The deterministic solver selected strategy '{selected_mode}' for supplier '{supplier_id}'. "
            f"Inbound note context: '{deviation.raw_note}'. "
            f"Supplier reliability context: '{supplier_reliability_context}'. "
            f"CRITICAL REQUIREMENT (Invariant I-1): Your response must be strictly qualitative text in "
            f"the fields 'narrative' and 'risk_summary'. Do NOT include any numeric values or currency amounts."
        )

        response = self.genai_client.models.generate_content(
            model=settings.MODEL_ID,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=OrchestratorNarrative,
            ),
        )

        try:
            narrative = OrchestratorNarrative.model_validate_json(response.text)
        except Exception:
            # Fallback compliant qualitative structure if json parse fails
            narrative = OrchestratorNarrative(
                narrative="The fleet identified a demand deviation and selected an optimal mitigation strategy through the deterministic solver.",
                risk_summary="Standard operational risk managed within established governance thresholds.",
            )

        # 5. Egress guardrail sanitization on LLM narrative output (I-12)
        san_narrative = sanitize(narrative.narrative, direction="egress").clean_text
        san_risk = sanitize(narrative.risk_summary, direction="egress").clean_text
        final_narrative = OrchestratorNarrative(narrative=san_narrative, risk_summary=san_risk)

        return scenario_set, final_narrative
