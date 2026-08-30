"""Sentinel Agent Registry Publisher (§2.2, §8.8).

Publishes the manifests of the Sentinel agent fleet into the BigQuery agent_registry table.
"""

import sys
import subprocess
from datetime import datetime, timezone
from google.cloud import bigquery

from config import settings


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


def seed_agent_registry():
    print("=" * 60)
    print("SENTINEL AGENT REGISTRY: Publishing manifests (§2.2, §8.8)")
    print("=" * 60)
    creds = _get_credentials()
    bq_client = bigquery.Client(project=settings.PROJECT_ID, credentials=creds)

    agents = [
        {
            "agent_id": "sentinel-hygiene",
            "agent_name": "Hygiene Agent",
            "version": "v1.0",
            "owning_department": "Data Governance & Ingestion",
            "input_schema_ref": "contracts.models.Deviation",
            "output_schema_ref": "contracts.models.Deviation",
            "rbac_scopes": ["bigquery.dataViewer", "bigquery.dataEditor"],
            "status": "ACTIVE",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "agent_id": "sentinel-sourcing",
            "agent_name": "Sourcing Specialist Sub-Agent",
            "version": "v1.0",
            "owning_department": "Global Procurement",
            "input_schema_ref": "contracts.models.Deviation",
            "output_schema_ref": "contracts.models.SupplyOption",
            "rbac_scopes": ["bigquery.dataViewer", "aiplatform.user"],
            "status": "ACTIVE",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "agent_id": "sentinel-orchestrator",
            "agent_name": "Triage Orchestrator",
            "version": "v1.0",
            "owning_department": "Autonomous Supply Chain Operations",
            "input_schema_ref": "contracts.models.Deviation",
            "output_schema_ref": "contracts.models.OrchestratorNarrative",
            "rbac_scopes": ["bigquery.dataEditor", "aiplatform.user", "pubsub.subscriber"],
            "status": "ACTIVE",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "agent_id": "sentinel-conversational-analytics",
            "agent_name": "Conversational Analytics Data Agent",
            "version": "v1.0",
            "owning_department": "Executive Intelligence",
            "input_schema_ref": "NaturalLanguageQuery",
            "output_schema_ref": "AnalyticalVisualReport",
            "rbac_scopes": ["bigquery.dataViewer"],
            "status": "ACTIVE",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        },
    ]

    table_id = f"{settings.PROJECT_ID}.{settings.BQ_DATASET}.agent_registry"
    try:
        bq_client.delete_table(table_id, not_found_ok=True)
    except Exception:
        pass

    ddl = f"""
    CREATE TABLE `{table_id}` (
        agent_id STRING NOT NULL,
        agent_name STRING NOT NULL,
        version STRING NOT NULL,
        input_schema_ref STRING,
        output_schema_ref STRING,
        rbac_scopes ARRAY<STRING>,
        owning_department STRING NOT NULL,
        status STRING NOT NULL,
        registered_at TIMESTAMP NOT NULL
    )
    """
    bq_client.query(ddl).result()

    errors = bq_client.insert_rows_json(table_id, agents)
    if errors:
        raise RuntimeError(f"Failed writing to agent_registry: {errors}")

    print(f"[SUCCESS] Published {len(agents)} agent manifests to {table_id}.")
    for a in agents:
        print(f"  • {a['agent_name']} ({a['agent_id']}) -> scopes: {a['rbac_scopes']}")
    print("=" * 60)


if __name__ == "__main__":
    seed_agent_registry()
