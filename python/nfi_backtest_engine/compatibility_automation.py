"""Typed public facade for compatibility routing and product status."""

from .compatibility_automation_core import classify_compatibility_automation
from .compatibility_status import (
    CompatibilityRunObservation,
    DiscoveryExecution,
    WorkflowExecution,
    classify_compatibility_status,
)

__all__ = [
    "CompatibilityRunObservation",
    "DiscoveryExecution",
    "WorkflowExecution",
    "classify_compatibility_automation",
    "classify_compatibility_status",
]
