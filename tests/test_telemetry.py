"""OpenTelemetry and Cloud Trace Fallback Exporter Tests (§8.12)."""

import sys
from pathlib import Path
import pytest
from unittest.mock import MagicMock

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from core.telemetry import (
    FallbackTraceSpanExporter,
    generate_traceparent,
    inject_traceparent,
    extract_traceparent,
    trace_span,
    get_tracer,
)
from opentelemetry.sdk.trace.export import SpanExportResult


def test_generate_traceparent_format():
    """Verify traceparent complies with W3C Trace Context spec."""
    tp = generate_traceparent()
    parts = tp.split("-")
    assert len(parts) == 4
    assert parts[0] == "00"
    assert len(parts[1]) == 32  # trace_id 32 hex chars
    assert len(parts[2]) == 16  # parent_id 16 hex chars
    assert parts[3] == "01"


def test_fallback_exporter_primary_success():
    """Verify fallback exporter returns SUCCESS when primary exporter succeeds."""
    exporter = FallbackTraceSpanExporter(project_id="test-project")
    mock_cloud = MagicMock()
    mock_cloud.export.return_value = SpanExportResult.SUCCESS
    exporter._cloud_exporter = mock_cloud

    mock_console = MagicMock()
    exporter._console_exporter = mock_console

    res = exporter.export([])
    assert res == SpanExportResult.SUCCESS
    mock_cloud.export.assert_called_once()
    mock_console.export.assert_not_called()


def test_fallback_exporter_primary_failure_falls_back():
    """Verify fallback exporter calls ConsoleSpanExporter when primary exporter fails."""
    exporter = FallbackTraceSpanExporter(project_id="test-project")
    mock_cloud = MagicMock()
    mock_cloud.export.return_value = SpanExportResult.FAILURE
    exporter._cloud_exporter = mock_cloud

    mock_console = MagicMock()
    mock_console.export.return_value = SpanExportResult.SUCCESS
    exporter._console_exporter = mock_console

    res = exporter.export([])
    assert res == SpanExportResult.SUCCESS
    mock_cloud.export.assert_called_once()
    mock_console.export.assert_called_once()


def test_fallback_exporter_primary_exception_falls_back():
    """Verify fallback exporter calls ConsoleSpanExporter when primary exporter throws an exception."""
    exporter = FallbackTraceSpanExporter(project_id="test-project")
    mock_cloud = MagicMock()
    mock_cloud.export.side_effect = RuntimeError("API unavailable")
    exporter._cloud_exporter = mock_cloud

    mock_console = MagicMock()
    mock_console.export.return_value = SpanExportResult.SUCCESS
    exporter._console_exporter = mock_console

    res = exporter.export([])
    assert res == SpanExportResult.SUCCESS
    mock_cloud.export.assert_called_once()
    mock_console.export.assert_called_once()
