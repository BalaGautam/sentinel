"""Sentinel Sourcing Specialist Agent (§5, §2.1).

ADK Sub-Agent specialized in analyzing supplier reliability metrics,
lead-time risk, contract terms, and qualitative sourcing trade-offs.
"""

from google.adk.agents.llm_agent import LlmAgent as Agent
from agents.factory import build_gemini_model


def create_sourcing_specialist_agent() -> Agent:
    """Instantiate the Sourcing Specialist ADK sub-agent (§5)."""
    instruction = (
        "You are the Sentinel Sourcing Specialist. Your role is to evaluate supplier options, "
        "historical reliability trends, lead-time variances, and supply mode trade-offs "
        "(Contract vs Spot vs Air Expedite vs DC Rebalance). Provide concise, qualitative "
        "sourcing risk assessments and recommendations to the Triage Orchestrator. "
        "Do NOT invent prices, quantities, or mathematical formulas."
    )

    return Agent(
        name="sourcing_specialist",
        description="Analyzes supplier reliability, lead-time risk, and sourcing modes.",
        model=build_gemini_model(),
        instruction=instruction,
    )
