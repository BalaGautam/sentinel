"""Sentinel Orchestrator Cloud Run Service (§5, §8.12).

Provides HTTP / PubSub push endpoints for executing the autonomous agent fleet.
Propagates W3C traceparent attributes through Pub/Sub push messages (§8.12).
"""

import os
import sys
import json
import base64
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, HTTPException, Response, status
from pydantic import BaseModel
import uvicorn

from config import settings
from agents.pipeline import run_sentinel_workflow

REGISTRY_DIR = Path(__file__).resolve().parent.parent / "config" / "registry"


class TraceFilter(logging.Filter):
    """Ensure all log records have trace_id populated to avoid KeyErrors."""
    def filter(self, record):
        if not hasattr(record, "trace_id"):
            record.trace_id = "NO_TRACE"
        return True


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [trace_id=%(trace_id)s] %(message)s"))
_handler.addFilter(TraceFilter())

logging.root.handlers = [_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("sentinel.orchestrator")


app = FastAPI(
    title="Sentinel Fleet Orchestrator",
    description="Autonomous Fortified Supply Chain Mitigation Agent Fleet",
    version="1.0.0",
)


@app.get("/")
@app.get("/health")
def health_check():
    """Health check endpoint for Cloud Run container probes."""
    return {
        "status": "HEALTHY",
        "service": "sentinel-orchestrator",
        "project": settings.PROJECT_ID,
        "region": settings.GCP_REGION,
        "inference_location": settings.VERTEX_INFERENCE_LOCATION,
        "model_id": settings.MODEL_ID,
    }


def _load_agent_card(agent_name: str) -> Response:
    """Load and return an A2A agent card JSON from config/registry/."""
    card_path = REGISTRY_DIR / f"{agent_name}.agent-card.json"
    if not card_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent card for '{agent_name}' not found")
    try:
        content = card_path.read_text(encoding="utf-8")
        return Response(content=content, media_type="application/json")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading agent card for {agent_name}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error reading agent card: {e}")


@app.get("/agents/hygiene")
def get_hygiene_agent_card():
    """A2A read-only discovery endpoint for Sentinel Hygiene Agent."""
    return _load_agent_card("hygiene")


@app.get("/agents/sourcing")
def get_sourcing_agent_card():
    """A2A read-only discovery endpoint for Sentinel Sourcing Specialist Agent."""
    return _load_agent_card("sourcing")


@app.get("/agents/orchestrator")
def get_orchestrator_agent_card():
    """A2A read-only discovery endpoint for Sentinel Triage Orchestrator Agent."""
    return _load_agent_card("orchestrator")



@app.post("/push")
async def handle_pubsub_push(request: Request):
    """Handle Pub/Sub push subscription deliveries with traceparent propagation (§8.12)."""
    try:
        envelope = await request.json()
    except Exception as e:
        logger.error(f"Invalid JSON in push body: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON envelope")

    if not envelope or "message" not in envelope:
        logger.error("Invalid Pub/Sub envelope structure (missing 'message')")
        raise HTTPException(status_code=400, detail="Missing message in Pub/Sub push envelope")

    pubsub_message = envelope["message"]
    attributes = pubsub_message.get("attributes") or {}

    # Extract W3C traceparent attribute per §8.12
    traceparent = attributes.get("traceparent") or "00-00000000000000000000000000000000-0000000000000000-00"
    trace_id = traceparent.split("-")[1] if "-" in traceparent and len(traceparent.split("-")) >= 2 else "NO_TRACE"

    trace_extra = {"trace_id": trace_id}
    logger.info(f"Received Pub/Sub push message. ID: {pubsub_message.get('messageId')}, traceparent: {traceparent}", extra=trace_extra)

    # Decode payload data
    raw_data = pubsub_message.get("data")
    payload = {}
    if raw_data:
        try:
            decoded_bytes = base64.b64decode(raw_data)
            payload = json.loads(decoded_bytes.decode("utf-8"))
        except Exception as e:
            logger.warning(f"Could not parse base64 data, using attributes: {e}", extra=trace_extra)

    # Fallback to loading deviation if deviation_id is present
    deviation_id = payload.get("deviation_id") or attributes.get("deviation_id")
    if not payload and deviation_id:
        from core.solver import load_deviation_from_bq, _get_credentials
        from google.cloud import bigquery
        creds = _get_credentials()
        bq = bigquery.Client(project=settings.PROJECT_ID, credentials=creds)
        dev_obj, _, _ = load_deviation_from_bq(bq, settings.BQ_DATASET, deviation_id)
        payload = dev_obj.model_dump(mode="json")

    if not payload:
        logger.error("Empty payload received in Pub/Sub message", extra=trace_extra)
        raise HTTPException(status_code=400, detail="Empty deviation payload")

    logger.info(f"Starting autonomous workflow for deviation '{payload.get('deviation_id')}'...", extra=trace_extra)

    try:
        workflow_result = run_sentinel_workflow(
            raw_deviation_payload=payload,
            traceparent=traceparent,
        )
        logger.info(
            f"Workflow completed for '{payload.get('deviation_id')}': "
            f"status={workflow_result.get('status')}, okf_outcome={workflow_result.get('okf_outcome')}",
            extra=trace_extra,
        )
        return {
            "status": "SUCCESS",
            "traceparent": traceparent,
            "result": workflow_result,
        }
    except Exception as e:
        logger.error(f"Workflow execution failed: {e}", exc_info=True, extra=trace_extra)
        return Response(
            content=json.dumps({"status": "ERROR", "error": str(e), "traceparent": traceparent}),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            media_type="application/json",
        )


@app.post("/process")
async def process_deviation(request: Request):
    """Direct processing endpoint for local and synchronous testing."""
    payload = await request.json()
    traceparent = request.headers.get("traceparent")
    result = run_sentinel_workflow(payload, traceparent=traceparent)
    return result


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("services.orchestrator_service:app", host="0.0.0.0", port=port, log_level="info")
