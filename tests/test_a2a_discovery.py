"""Unit tests for A2A discovery endpoints in orchestrator service."""

import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from services.orchestrator_service import app

client = TestClient(app)


def test_get_hygiene_agent_card():
    """Verify GET /agents/hygiene returns the valid hygiene agent card."""
    response = client.get("/agents/hygiene")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    data = response.json()
    assert data["schemaVersion"] == "v1"
    assert data["displayName"] == "Sentinel Hygiene Agent"
    assert data["url"] == "https://sentinel-orchestrator-627057384680.us-central1.run.app/agents/hygiene"
    skill_names = [s["name"] for s in data["skills"]]
    assert "validate_deviation_schema" in skill_names
    assert "sanitize_inbound_text" in skill_names


def test_get_sourcing_agent_card():
    """Verify GET /agents/sourcing returns the valid sourcing agent card."""
    response = client.get("/agents/sourcing")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    data = response.json()
    assert data["schemaVersion"] == "v1"
    assert data["displayName"] == "Sentinel Sourcing Specialist"
    assert data["url"] == "https://sentinel-orchestrator-627057384680.us-central1.run.app/agents/sourcing"
    skill_names = [s["name"] for s in data["skills"]]
    assert "resolve_supply_options" in skill_names


def test_get_orchestrator_agent_card():
    """Verify GET /agents/orchestrator returns the valid orchestrator agent card."""
    response = client.get("/agents/orchestrator")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    data = response.json()
    assert data["schemaVersion"] == "v1"
    assert data["displayName"] == "Sentinel Triage Orchestrator"
    assert data["url"] == "https://sentinel-orchestrator-627057384680.us-central1.run.app/agents/orchestrator"
    skill_names = [s["name"] for s in data["skills"]]
    assert "triage_deviation" in skill_names
    assert "score_mitigation_scenarios" in skill_names


def test_post_not_allowed_on_discovery_routes():
    """Verify POST is rejected (405 Method Not Allowed) on discovery routes."""
    assert client.post("/agents/hygiene", json={}).status_code == 405
    assert client.post("/agents/sourcing", json={}).status_code == 405
    assert client.post("/agents/orchestrator", json={}).status_code == 405


def test_missing_card_returns_404(monkeypatch):
    """Verify 404 is returned when an agent card file does not exist."""
    import services.orchestrator_service
    fake_path = Path("/nonexistent/dir")
    monkeypatch.setattr(services.orchestrator_service, "REGISTRY_DIR", fake_path)
    response = client.get("/agents/hygiene")
    assert response.status_code == 404
