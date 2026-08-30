"""Sentinel Hygiene Agent (§5, §8.4, I-10, I-12).

Validates inbound deviation payloads against the Deviation contract
and applies ingress guardrail sanitization to prevent prompt injection
and policy bypass attempts.
"""

from typing import Dict, Any, Tuple, Optional
from datetime import datetime, timezone
from google.cloud import bigquery

from config import settings
from contracts.models import Deviation
from core.guardrail import sanitize, GuardrailResult
from core.ledger import append_ledger_record


class HygieneAgent:
    """Hygiene Agent validating schema contracts and ingress safety."""

    def __init__(self, bq_client: bigquery.Client, dataset_id: str):
        self.bq_client = bq_client
        self.dataset_id = dataset_id

    def process(
        self, raw_payload: Dict[str, Any], workflow_root_id: Optional[str] = None
    ) -> Tuple[Optional[Deviation], GuardrailResult]:
        """Validate inbound payload, apply ingress guardrail, and log audit records."""
        # 1. Parse into Deviation model
        try:
            payload_copy = dict(raw_payload)
            if "deviation_type" not in payload_copy and "disruption_type" in payload_copy:
                payload_copy["deviation_type"] = payload_copy["disruption_type"]
            if isinstance(payload_copy.get("detected_at"), str):
                payload_copy["detected_at"] = datetime.fromisoformat(
                    payload_copy["detected_at"].replace("Z", "+00:00")
                )
            deviation = Deviation(**payload_copy)
        except Exception as e:
            # SENSE record with schema error
            dev_id = raw_payload.get("deviation_id", "DEV-UNKNOWN")
            wf_id = workflow_root_id or f"WF-{dev_id}"
            append_ledger_record(
                bq_client=self.bq_client,
                dataset_id=self.dataset_id,
                deviation_id=dev_id,
                workflow_root_id=wf_id,
                phase="SENSE",
                payload={"raw_payload": raw_payload, "error": str(e)},
            )
            append_ledger_record(
                bq_client=self.bq_client,
                dataset_id=self.dataset_id,
                deviation_id=dev_id,
                workflow_root_id=wf_id,
                phase="SANITIZE",
                payload={"passed": False, "reason": f"Contract validation error: {e}"},
                okf_outcome="BLOCKED",
            )
            return None, GuardrailResult(passed=False, reason=f"Contract validation error: {e}")

        wf_id = workflow_root_id or f"WF-{deviation.deviation_id}"

        # 2. Log SENSE record per I-10
        append_ledger_record(
            bq_client=self.bq_client,
            dataset_id=self.dataset_id,
            deviation_id=deviation.deviation_id,
            workflow_root_id=wf_id,
            phase="SENSE",
            payload={
                "deviation_type": deviation.deviation_type,
                "sku_id": deviation.sku_id,
                "dc_id": deviation.dc_id,
                "magnitude_units": deviation.magnitude_units,
                "delay_days": deviation.delay_days,
                "source_system": deviation.source_system,
                "raw_note": deviation.raw_note,
            },
        )

        # 3. Apply Ingress Guardrail Sanitization (I-12)
        guard_result = sanitize(deviation.raw_note, direction="ingress")

        # 4. Log SANITIZE record per I-10
        append_ledger_record(
            bq_client=self.bq_client,
            dataset_id=self.dataset_id,
            deviation_id=deviation.deviation_id,
            workflow_root_id=wf_id,
            phase="SANITIZE",
            payload={
                "passed": guard_result.passed,
                "injection": guard_result.injection,
                "pii": guard_result.pii,
                "reason": guard_result.reason,
                "clean_text": guard_result.clean_text,
            },
            prompt_digest=guard_result.reason,
            okf_outcome="BLOCKED" if not guard_result.passed else None,
        )

        if not guard_result.passed:
            return None, guard_result

        # Update deviation with sanitized note
        sanitized_deviation = deviation.model_copy(update={"raw_note": guard_result.clean_text})
        return sanitized_deviation, guard_result
