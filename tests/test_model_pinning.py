"""Model Pinning Tests (§2.1, I-13).

Enforces structural model pinning requirements:
1. Settings load strictly from environment with no default fallback values.
2. MODEL_ID is pinned to gemini-3.7-flash on the global endpoint.
3. Codebase grep assertion ensures no hardcoded gemini-[0-9] strings exist in application code.
"""

import os
import re
import sys
import subprocess
from pathlib import Path
import pytest

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))


def test_settings_missing_env_raises():
    """Verify missing required environment variables hard-fail at import/load (§2.1 rule 1)."""
    # Test by running a python one-liner in an environment without MODEL_ID
    env = dict(os.environ)
    env["MODEL_ID"] = ""  # empty string to prevent fallback or reload
    proc = subprocess.run(
        [sys.executable, "-c", "import config.settings"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "Missing required environment variable: MODEL_ID" in proc.stderr


def test_settings_missing_region_raises():
    """Verify missing GCP_REGION hard-fails at import/load (§2.1 rule 1)."""
    env = dict(os.environ)
    env["GCP_REGION"] = ""
    proc = subprocess.run(
        [sys.executable, "-c", "import config.settings"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "Missing required environment variable: GCP_REGION" in proc.stderr


def test_model_id_and_location_pinned():
    """Verify MODEL_ID, VERTEX_INFERENCE_LOCATION, and GCP_REGION values (§2.1, §2.6)."""
    from config import settings

    assert settings.MODEL_ID == "gemini-3.7-flash"
    assert settings.VERTEX_INFERENCE_LOCATION == "global"
    assert settings.GCP_REGION == "us-central1"
    # Ensure they are not collapsed into a single variable (§2.1 rule 6)
    assert settings.VERTEX_INFERENCE_LOCATION != settings.GCP_REGION


def test_repo_grep_model_pinning():
    """Assert no hardcoded gemini-[0-9] strings exist in application source code files (§2.1 rule 4)."""
    repo_root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"gemini-[0-9]")

    # Ignored directories, extensions, and test files
    ignored_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "tests"}
    ignored_extensions = {".md", ".txt", ".json", ".log"}
    ignored_files = {".env", ".env.example", ".gitignore"}


    matches = []

    for path in repo_root.rglob("*"):
        if path.is_file():
            if any(part in ignored_dirs for part in path.parts):
                continue
            if path.name in ignored_files or path.suffix in ignored_extensions:
                continue

            try:
                content = path.read_text(encoding="utf-8")
                for line_no, line in enumerate(content.splitlines(), start=1):
                    if pattern.search(line):
                        matches.append(f"{path.relative_to(repo_root)}:{line_no} -> {line.strip()}")
            except Exception:
                pass

    assert len(matches) == 0, f"Found unauthorized hardcoded model strings in code files:\n" + "\n".join(matches)
