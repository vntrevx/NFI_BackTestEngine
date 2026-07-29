from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "futures_discovery_health_issue",
    ROOT / "scripts" / "futures_discovery_health_issue.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_discovery_health_failure_is_deduplicated() -> None:
    stages = {"resolve": "success", "discover": "failure", "publish": "skipped"}
    initial = MODULE.build_health_plan(
        stages,
        [],
        run_url="https://example.invalid/1",
    )
    repeated = MODULE.build_health_plan(
        stages,
        [
            {
                "number": 21,
                "body": (
                    "<!-- nfi-futures-discovery-health:"
                    f"{initial['fingerprint']} -->"
                ),
            }
        ],
        run_url="https://example.invalid/2",
    )

    assert repeated["create"] is None
    assert repeated["close"] == []
    assert repeated["healthy"] is False


def test_healthy_discovery_closes_only_its_health_issue() -> None:
    plan = MODULE.build_health_plan(
        {"resolve": "success", "discover": "success", "publish": "skipped"},
        [
            {
                "number": 22,
                "body": (
                    "<!-- nfi-futures-discovery-health:" + "a" * 64 + " -->"
                ),
            }
        ],
        run_url="https://example.invalid/3",
    )

    assert plan["healthy"] is True
    assert plan["close"] == [22]
