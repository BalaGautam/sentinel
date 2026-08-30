"""Sentinel Agent Factory (§2.1, §5, I-13).

Provides Vertex AI client and ADK Gemini model initializers.
Enforces model pinning to settings.MODEL_ID with location pinned to global.
"""

import sys
import subprocess
import google.auth
from google.auth.exceptions import RefreshError, DefaultCredentialsError
from google.oauth2 import credentials
from google import genai
from google.adk.models.google_llm import Gemini

from config import settings


def get_credentials():
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


def get_genai_client() -> genai.Client:
    """Initialize Vertex AI genai.Client at settings.VERTEX_INFERENCE_LOCATION (global)."""
    creds = get_credentials()
    return genai.Client(
        vertexai=True,
        project=settings.PROJECT_ID,
        location=settings.VERTEX_INFERENCE_LOCATION,
        credentials=creds,
    )


def build_gemini_model() -> Gemini:
    """Factory for ADK Gemini model (§2.1 rule 2).

    Reads settings.MODEL_ID directly and exposes no model parameter.
    """
    client = get_genai_client()
    return Gemini(
        model=settings.MODEL_ID,
        client=client,
    )
