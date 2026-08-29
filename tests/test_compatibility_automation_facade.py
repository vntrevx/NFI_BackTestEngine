from __future__ import annotations

from nfi_backtest_engine import compatibility_automation, compatibility_status


def test_r2_status_api_remains_available_from_compatibility_automation() -> None:
    assert (
        compatibility_automation.CompatibilityRunObservation
        is compatibility_status.CompatibilityRunObservation
    )
    assert compatibility_automation.DiscoveryExecution is compatibility_status.DiscoveryExecution
    assert compatibility_automation.WorkflowExecution is compatibility_status.WorkflowExecution
    assert (
        compatibility_automation.classify_compatibility_status
        is compatibility_status.classify_compatibility_status
    )
