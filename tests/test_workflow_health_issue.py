from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "workflow_health_issue",
    ROOT / "scripts" / "workflow_health_issue.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_same_health_failure_is_not_duplicated() -> None:
    stages = {
        "discover": "success",
        "diff": "failure",
        "x7": "success",
        "targeted": "skipped",
        "publish": "skipped",
    }
    initial = MODULE.build_health_issue_plan(
        stages,
        [],
        changed=True,
        run_url="https://example.invalid/1",
    )
    fingerprint = initial["fingerprint"]
    existing = [
        {
            "number": 12,
            "body": (
                "<!-- nfi-automation-health-fingerprint:"
                f"{fingerprint} -->"
            ),
        }
    ]

    repeated = MODULE.build_health_issue_plan(
        stages,
        existing,
        changed=True,
        run_url="https://example.invalid/2",
    )

    assert repeated["create"] is None
    assert repeated["close"] == []
    assert repeated["healthy"] is False


def test_no_change_discovery_only_run_is_healthy() -> None:
    plan = MODULE.build_health_issue_plan(
        {
            "discover": "success",
            "diff": "skipped",
            "x7": "skipped",
            "targeted": "skipped",
            "publish": "skipped",
        },
        [{"number": 14, "body": "old"}],
        changed=False,
        run_url="https://example.invalid/3",
    )

    assert plan["healthy"] is True
    assert plan["create"] is None
    assert plan["close"] == [14]
