"""Sentinel OpenTelemetry Tracing & Observability Engine (§8.12).

Provides:
1. OpenTelemetry TracerProvider configuration and W3C traceparent propagation.
2. Context manager and decorator `@trace_span` for instrumenting fleet reasoning hops.
3. Trace context carrier injection and extraction across Pub/Sub boundaries.
4. Agent ops event streaming to BigQuery `sentinel.agent_ops` table.
"""

import sys
import time
import uuid
import secrets
import logging
from functools import wraps
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Generator, Sequence

from opentelemetry import trace, context
from opentelemetry.trace import Tracer, Span, StatusCode, SpanKind
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.resources import Resource

from google.cloud import bigquery
from config import settings

logger = logging.getLogger("sentinel.telemetry")


class FallbackTraceSpanExporter(SpanExporter):
    """Exports spans to Google Cloud Trace with automatic fallback to ConsoleSpanExporter (§8.12)."""

    def __init__(self, project_id: Optional[str] = None):
        self.project_id = project_id or settings.PROJECT_ID
        self._console_exporter = ConsoleSpanExporter(out=sys.stderr)
        self._cloud_exporter: Optional[CloudTraceSpanExporter] = None
        try:
            self._cloud_exporter = CloudTraceSpanExporter(project_id=self.project_id)
        except Exception as exc:
            logger.warning(
                "Failed to initialize CloudTraceSpanExporter for project %s: %s. Using ConsoleSpanExporter fallback.",
                self.project_id,
                exc,
            )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._cloud_exporter is not None:
            try:
                res = self._cloud_exporter.export(spans)
                if res == SpanExportResult.SUCCESS:
                    return res
                logger.warning(
                    "CloudTraceSpanExporter export returned non-success result (%s). Falling back to ConsoleSpanExporter.",
                    res,
                )
            except Exception as exc:
                logger.warning(
                    "CloudTraceSpanExporter export threw an exception (%s). Falling back to ConsoleSpanExporter.",
                    exc,
                )
        return self._console_exporter.export(spans)

    def shutdown(self) -> None:
        if self._cloud_exporter is not None:
            try:
                self._cloud_exporter.shutdown()
            except Exception:
                pass
        self._console_exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        success = True
        if self._cloud_exporter is not None and hasattr(self._cloud_exporter, "force_flush"):
            try:
                success = self._cloud_exporter.force_flush(timeout_millis)
            except Exception:
                success = False
        if hasattr(self._console_exporter, "force_flush"):
            try:
                self._console_exporter.force_flush(timeout_millis)
            except Exception:
                pass
        return success


# Initialize global OpenTelemetry Tracer Provider
_resource = Resource.create({
    "service.name": "sentinel-fleet",
    "service.namespace": "supply-chain-orchestrator",
    "service.version": "1.0.0",
    "cloud.provider": "gcp",
    "cloud.region": settings.GCP_REGION,
})

_tracer_provider = TracerProvider(resource=_resource)
_trace_exporter = FallbackTraceSpanExporter(project_id=settings.PROJECT_ID)
_tracer_provider.add_span_processor(SimpleSpanProcessor(_trace_exporter))

trace.set_tracer_provider(_tracer_provider)
_tracer = trace.get_tracer("sentinel.orchestrator", "1.0.0")
_propagator = TraceContextTextMapPropagator()


def get_tracer() -> Tracer:
    """Get the active OpenTelemetry tracer."""
    return _tracer


def generate_traceparent() -> str:
    """Generate a valid W3C traceparent header string (§8.12).

    Format: 00-{32_hex_trace_id}-{16_hex_parent_id}-{2_hex_flags}
    """
    trace_id = secrets.token_hex(16)
    parent_id = secrets.token_hex(8)
    return f"00-{trace_id}-{parent_id}-01"


def inject_traceparent(carrier: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Inject current OpenTelemetry trace context into carrier dict as W3C traceparent."""
    if carrier is None:
        carrier = {}
    _propagator.inject(carrier)
    if "traceparent" not in carrier:
        current_span = trace.get_current_span()
        if current_span and current_span.get_span_context().is_valid:
            ctx = current_span.get_span_context()
            carrier["traceparent"] = f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-01"
        else:
            carrier["traceparent"] = generate_traceparent()
    return carrier


def extract_traceparent(carrier: Dict[str, Any]) -> context.Context:
    """Extract W3C traceparent from carrier dictionary to reconstruct distributed context (§8.12)."""
    # Normalize carrier keys to string
    norm_carrier = {str(k).lower(): str(v) for k, v in carrier.items()}
    
    # Check traceparent
    tp = norm_carrier.get("traceparent")
    if tp:
        extracted_ctx = _propagator.extract(norm_carrier)
        return extracted_ctx
    return context.get_current()


@contextmanager
def trace_span(
    name: str,
    attributes: Optional[Dict[str, Any]] = None,
    parent_context: Optional[context.Context] = None,
    kind: SpanKind = SpanKind.INTERNAL,
) -> Generator[Span, None, None]:
    """Context manager to start an active OpenTelemetry span."""
    tracer = get_tracer()
    token = None
    if parent_context is not None:
        token = context.attach(parent_context)

    span = tracer.start_span(name=name, kind=kind, attributes=attributes or {})
    activation = trace.use_span(span, end_on_exit=True)
    try:
        with activation:
            yield span
    except Exception as exc:
        span.record_exception(exc)
        span.set_status(StatusCode.ERROR, str(exc))
        raise exc
    finally:
        if token is not None:
            context.detach(token)


def trace_function(span_name: Optional[str] = None):
    """Decorator to wrap function execution in an OpenTelemetry span."""
    def decorator(fn):
        name = span_name or f"sentinel.{fn.__module__}.{fn.__name__}"
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with trace_span(name):
                return fn(*args, **kwargs)
        return wrapper
    return decorator


def stream_agent_ops_event(
    bq_client: bigquery.Client,
    dataset_id: str,
    workflow_root_id: str,
    agent_id: str,
    step_name: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    tool_calls_count: int = 0,
) -> None:
    """Stream agent reasoning hop telemetry to BigQuery sentinel.agent_ops table (§8.12)."""
    event_id = f"OPS-{uuid.uuid4().hex[:8].upper()}"
    now_iso = datetime.now(timezone.utc).isoformat()
    row = {
        "event_id": event_id,
        "workflow_root_id": workflow_root_id,
        "agent_id": agent_id,
        "step_name": step_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "tool_calls_count": tool_calls_count,
        "created_at": now_iso,
    }
    table_ref = f"{bq_client.project}.{dataset_id}.agent_ops"
    try:
        errors = bq_client.insert_rows_json(table_ref, [row])
        if errors:
            logger.warning(f"Error streaming to agent_ops: {errors}")
    except Exception as e:
        # Fallback append load
        try:
            job_config = bigquery.LoadJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
            )
            job = bq_client.load_table_from_json([row], table_ref, job_config=job_config)
            job.result()
        except Exception as e2:
            logger.debug(f"Could not write to agent_ops: {e2}")
