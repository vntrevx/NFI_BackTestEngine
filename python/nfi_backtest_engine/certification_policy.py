"""Machine-consumed fail-closed semantics for certification evidence."""

from __future__ import annotations

from typing import Any

from .errors import SpecValidationError

_FAILURE_STATUSES = frozenset(
    {"failed", "incomplete", "error", "timed_out", "timeout", "skipped", "unknown"}
)
_UNSAFE_EXECUTION_MODES = frozenset({"unsafe_research_override", "external_override"})


def validate_certification_semantics(document: Any, *, label: str) -> None:
    """Reject semantic failure markers even when surrounding gate booleans are true."""
    stack: list[tuple[Any, str]] = [(document, "$")]
    while stack:
        value, path = stack.pop()
        if isinstance(value, dict):
            status = value.get("status")
            if isinstance(status, str) and status.lower() in _FAILURE_STATUSES:
                raise SpecValidationError(f"{label} has failed semantic status at {path}")
            if value.get("certification_eligible") is False:
                raise SpecValidationError(f"{label} is explicitly certification-ineligible")
            if value.get("execution_mode") in _UNSAFE_EXECUTION_MODES:
                raise SpecValidationError(f"{label} used an unsafe research execution mode")
            if value.get("exact") is False:
                raise SpecValidationError(f"{label} is not exact at {path}")
            if value.get("complete") is False:
                raise SpecValidationError(f"{label} is incomplete at {path}")
            stack.extend((item, f"{path}.{key}") for key, item in value.items())
        elif isinstance(value, list):
            stack.extend((item, f"{path}[{index}]") for index, item in enumerate(value))
