"""Sentinel Configuration Settings (§2.1).

All configuration is loaded strictly from environment variables.
No default fallback strings are permitted — missing variables raise an EnvironmentError at import.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env if present (for local development)
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
else:
    load_dotenv()


def _require_env(key: str) -> str:
    """Retrieve an environment variable or raise an exception if missing/empty."""
    val = os.environ.get(key)
    if not val or not val.strip():
        raise EnvironmentError(f"Missing required environment variable: {key}")
    return val.strip()


# Project & Region (§2.1, §2.6)
_project_id = os.environ.get("GCP_PROJECT_ID") or os.environ.get("PROJECT_ID")
if not _project_id or not _project_id.strip():
    raise EnvironmentError("Missing required environment variable: GCP_PROJECT_ID or PROJECT_ID")
PROJECT_ID: str = _project_id.strip()

# GCP Region for compute and data (pinned to us-central1)
GCP_REGION: str = _require_env("GCP_REGION")

# Vertex Inference Location (pinned to global for Gemini 3.7 Flash)
# Must NOT be collapsed into GCP_REGION per §2.1 rule 6
VERTEX_INFERENCE_LOCATION: str = _require_env("VERTEX_INFERENCE_LOCATION")

# Model Pinning (§2.1 rule 1)
# Pinned to environment variable MODEL_ID with NO default fallback string.
MODEL_ID: str = _require_env("MODEL_ID")

# BigQuery Dataset
BQ_DATASET: str = _require_env("BQ_DATASET")
