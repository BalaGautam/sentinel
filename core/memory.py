"""Sentinel Vertex AI Memory Bank Integration (§5, I-8).

Maintains deterministic computed supplier reliability metrics in Vertex AI Memory Bank.
Provides scoped memory retrieval with provenance="computed" per Invariant I-8:
Only deterministic computed values go into memory. No LLM-generated text, ever.
"""

import sys
import json
from typing import Dict, Any, Optional, List
import google.auth
from google.auth.exceptions import RefreshError, DefaultCredentialsError
from google.oauth2 import credentials
from google.cloud import bigquery
import vertexai

from config import settings


def _get_credentials():
    """Retrieve credentials via google.auth with fallback to active gcloud token."""
    try:
        creds, _ = google.auth.default()
        import google.auth.transport.requests
        creds.refresh(google.auth.transport.requests.Request())
        return creds
    except (RefreshError, DefaultCredentialsError, Exception):
        import subprocess
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


_vertex_client_cache: Optional[vertexai.Client] = None
_memory_store_name_cache: Optional[str] = None


def get_vertex_client() -> vertexai.Client:
    """Return a cached singleton vertexai.Client instance."""
    global _vertex_client_cache
    if _vertex_client_cache is None:
        creds = _get_credentials()
        _vertex_client_cache = vertexai.Client(
            project=settings.PROJECT_ID,
            location="us-central1",
            credentials=creds,
        )
    return _vertex_client_cache


def get_or_create_memory_store(
    client: Optional[vertexai.Client] = None,
    display_name: str = "sentinel-supplier-memory-bank",
) -> str:
    """Find existing or create new Memory Bank store (AgentEngine ReasoningEngine)."""
    global _memory_store_name_cache
    if _memory_store_name_cache is not None:
        return _memory_store_name_cache

    cli = client or get_vertex_client()
    try:
        engines = list(cli.agent_engines.list())
        for e in engines:
            api_res = getattr(e, "api_resource", None)
            d_name = getattr(api_res, "display_name", None) or getattr(e, "display_name", None)
            if d_name == display_name:
                res_name = getattr(api_res, "name", None) or getattr(e, "name", None)
                if res_name:
                    _memory_store_name_cache = res_name
                    return res_name
    except Exception as exc:
        print(f"Warning: Listing agent engines encountered error: {exc}", file=sys.stderr)

    # Create new lightweight store
    engine = cli.agent_engines.create(
        agent=None,
        config=dict(display_name=display_name),
    )
    res_name = getattr(engine.api_resource, "name", None) or engine.name
    _memory_store_name_cache = res_name
    return res_name


def seed_supplier_reliability_memories(
    bq_client: bigquery.Client,
    dataset_id: str,
    client: Optional[vertexai.Client] = None,
    store_name: Optional[str] = None,
) -> int:
    """Seed computed supplier reliability metrics from BigQuery into Memory Bank (I-8)."""
    cli = client or get_vertex_client()
    name = store_name or get_or_create_memory_store(cli)

    query = f"""
    SELECT supplier_id, on_time_rate_90d, avg_lead_time_drift_days, quote_variance_rate, sample_size, provenance
    FROM `{bq_client.project}.{dataset_id}.supplier_reliability`
    ORDER BY supplier_id
    """
    rows = list(bq_client.query(query).result())
    count = 0

    for r in rows:
        sup_id = r["supplier_id"]
        fact_dict = {
            "supplier_id": sup_id,
            "on_time_rate_90d": float(r["on_time_rate_90d"]) if r["on_time_rate_90d"] is not None else None,
            "avg_lead_time_drift_days": float(r["avg_lead_time_drift_days"]) if r["avg_lead_time_drift_days"] is not None else 0.0,
            "quote_variance_rate": float(r["quote_variance_rate"]) if r["quote_variance_rate"] is not None else 0.0,
            "sample_size": int(r["sample_size"]),
            "provenance": "computed",  # Invariant I-8
        }
        cli.agent_engines.memories.create(
            name=name,
            fact=json.dumps(fact_dict),
            scope={"supplier_id": sup_id},
        )
        count += 1

    return count


def read_supplier_reliability_memory(
    supplier_id: str,
    client: Optional[vertexai.Client] = None,
    store_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve computed supplier reliability metrics from Vertex AI Memory Bank (I-8)."""
    try:
        cli = client or get_vertex_client()
        name = store_name or get_or_create_memory_store(cli)
        memories = list(cli.agent_engines.memories.retrieve(
            name=name,
            scope={"supplier_id": supplier_id},
        ))
        if memories:
            latest = memories[0]
            fact_str = latest.memory.fact if latest.memory else None
            if fact_str:
                data = json.loads(fact_str)
                data["_source"] = "VERTEX_AI_MEMORY_BANK"
                data["_store"] = name
                return data
    except Exception as exc:
        print(f"[Memory Bank Warning] Memory Bank read for {supplier_id} failed ({exc}). Triggering BigQuery fallback.", file=sys.stderr)

    return None
