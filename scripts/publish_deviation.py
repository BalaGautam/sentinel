"""Sentinel Deviation Publisher (§8.8, §8.12).

Publishes a detected deviation to the Pub/Sub topic `sentinel-deviations`
and injects the W3C `traceparent` attribute for distributed trace propagation.
"""

import sys
import json
import uuid
import secrets
import argparse
import subprocess
from datetime import datetime, timezone

import google.auth
from google.auth.exceptions import RefreshError, DefaultCredentialsError
from google.oauth2 import credentials
from google.cloud import bigquery

from config import settings
from core.solver import load_deviation_from_bq


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


def generate_w3c_traceparent() -> str:
    """Generate a valid W3C traceparent header string (§8.12).

    Format: version-trace_id-parent_id-trace_flags
    Example: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
    """
    version = "00"
    trace_id = secrets.token_hex(16)  # 32 hex chars
    span_id = secrets.token_hex(8)    # 16 hex chars
    trace_flags = "01"                # sampled
    return f"{version}-{trace_id}-{span_id}-{trace_flags}"


def publish_deviation(deviation_id: str, topic_id: str = "sentinel-deviations") -> str:
    """Publish a deviation to Pub/Sub with injected traceparent attribute (§8.12)."""
    creds = _get_credentials()
    bq_client = bigquery.Client(project=settings.PROJECT_ID, credentials=creds)

    # 1. Load deviation from BigQuery
    deviation, _, _ = load_deviation_from_bq(bq_client, settings.BQ_DATASET, deviation_id)
    payload_dict = deviation.model_dump(mode="json")
    data_str = json.dumps(payload_dict)

    # 2. Generate and inject W3C traceparent (§8.12)
    traceparent = generate_w3c_traceparent()
    now_iso = datetime.now(timezone.utc).isoformat()

    print("=" * 60)
    print(f"PUBLISHING DEVIATION TO PUB/SUB: {deviation_id}")
    print("=" * 60)
    print(f"Topic:       projects/{settings.PROJECT_ID}/topics/{topic_id}")
    print(f"Traceparent: {traceparent}")
    print(f"Payload SKU: {deviation.sku_id}, Magnitude: {deviation.magnitude_units} units")
    print("-" * 60)

    # 3. Publish via gcloud pubsub topic publish
    attr_arg = f"traceparent={traceparent},deviation_id={deviation_id},sku_id={deviation.sku_id},published_at={now_iso}"
    cmd = [
        "gcloud", "pubsub", "topics", "publish", topic_id,
        f"--project={settings.PROJECT_ID}",
        f"--message={data_str}",
        f"--attribute={attr_arg}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    message_id = res.stdout.strip().replace("messageIds:\n- '", "").replace("'", "").strip()

    print(f"[SUCCESS] Message successfully published. Pub/Sub Message ID: {message_id}")
    print("=" * 60)
    return message_id


def main():
    parser = argparse.ArgumentParser(description="Publish deviation to Pub/Sub with trace context (§8.8, §8.12)")
    parser.add_argument("--id", "-i", required=True, help="Deviation ID to publish (e.g. DEV-001, DEV-002, DEV-004)")
    parser.add_argument("--topic", "-t", default="sentinel-deviations", help="Pub/Sub topic ID")
    args = parser.parse_args()

    try:
        publish_deviation(args.id, args.topic)
    except Exception as e:
        print(f"ERROR: Failed publishing deviation '{args.id}': {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
