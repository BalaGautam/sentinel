"""Sentinel Model Pinning Preflight Gate (§2.1, §8.9, I-13).

Resolves settings.MODEL_ID against Vertex AI on VERTEX_INFERENCE_LOCATION (global),
sends a 1-token ping, and exits non-zero if resolution or execution fails.
"""

import sys
import subprocess
import google.auth
from google.auth.exceptions import RefreshError, DefaultCredentialsError
from google.oauth2 import credentials
from google import genai
from google.genai import types

from config import settings


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


def run_preflight() -> int:
    print("=" * 60)
    print("SENTINEL PREFLIGHT: Model Pinning Gate (§2.1, I-13)")
    print("=" * 60)
    print(f"Project ID:                 {settings.PROJECT_ID}")
    print(f"Compute/Data Region:        {settings.GCP_REGION}")
    print(f"Vertex Inference Location:  {settings.VERTEX_INFERENCE_LOCATION}")
    print(f"Requested Model ID:         {settings.MODEL_ID}")
    print("-" * 60)

    # Enforce global inference endpoint per §2.1
    if settings.VERTEX_INFERENCE_LOCATION != "global":
        print(
            f"ERROR: Invalid VERTEX_INFERENCE_LOCATION '{settings.VERTEX_INFERENCE_LOCATION}'. "
            "Gemini 3.7 Flash requires location='global'.",
            file=sys.stderr,
        )
        return 1

    try:
        creds = _get_credentials()
        client = genai.Client(
            vertexai=True,
            project=settings.PROJECT_ID,
            location=settings.VERTEX_INFERENCE_LOCATION,
            credentials=creds,
        )

        print(f"Sending 1-token ping to '{settings.MODEL_ID}' on Vertex AI ({settings.VERTEX_INFERENCE_LOCATION})...")
        response = client.models.generate_content(
            model=settings.MODEL_ID,
            contents="ping",
            config=types.GenerateContentConfig(max_output_tokens=1),
        )

        print("[PREFLIGHT PASS] Model successfully resolved and verified.")
        print(f"Model ID: {settings.MODEL_ID} (Status: ACTIVE, Endpoint: {settings.VERTEX_INFERENCE_LOCATION})")
        print("=" * 60)
        return 0

    except Exception as e:
        print(f"[PREFLIGHT FAILED] Could not resolve model '{settings.MODEL_ID}': {e}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run_preflight())
