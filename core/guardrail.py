"""Sentinel Guardrail & Sanitization Engine (§8.4, I-12).

Provides deterministic pre-filtering and sanitization for ingress raw notes
and egress tool returns to prevent prompt injection and policy evasion.
"""

import re
from typing import Dict, Any, Literal
from pydantic import BaseModel


class GuardrailResult(BaseModel):
    passed: bool
    reason: str = "CLEAN"
    injection: bool = False
    pii: bool = False
    commercial_leak: bool = False
    clean_text: str = ""


# Ingress Prompt Injection & Policy Override Patterns (§8.4, DEV-002)
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?policy(\s+ceilings)?",
    r"system\s+instruction",
    r"override_okf",
    r"auto-approve\s+all",
    r"bypass\s+code",
    r"role-switch",
    r"disregard\s+(previous\s+)?instructions",
    r"developer\s+mode",
    r"elevate\s+privilege",
]


def sanitize(text: str, direction: Literal["ingress", "egress"] = "ingress") -> GuardrailResult:
    """Sanitize and evaluate text for prompt injections and malicious payloads (§8.4)."""
    if not text:
        return GuardrailResult(passed=True, clean_text="")

    text_lower = text.lower()

    # Deterministic pattern inspection
    for pat in INJECTION_PATTERNS:
        if re.search(pat, text_lower):
            return GuardrailResult(
                passed=False,
                reason=f"Security violation detected: prompt-injection / policy-override attempt matching '{pat}'",
                injection=True,
                clean_text="[FILTERED_INJECTION_ATTEMPT]",
            )

    return GuardrailResult(
        passed=True,
        reason="CLEAN",
        clean_text=text.strip(),
    )
