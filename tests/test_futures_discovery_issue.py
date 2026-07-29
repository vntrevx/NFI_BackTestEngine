from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "futures_discovery_issue",
    ROOT / "scripts" / "futures_discovery_issue.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _report(status: str) -> dict:
    return {
        "status": status,
        "fingerprint": "d" * 64,
        "upstream_commit": "a" * 40,
        "engine_commit": "b" * 40,
        "target_count": 1,
        "searched_shard_count": 20,
        "shard_count": 20,
        "attempts": [{"message": "not observed"}],
    }


def test_terminal_gap_is_deduplicated_by_target_fingerprint() -> None:
    first = MODULE.build_issue_plan(
        _report("coverage_exhausted"),
        [],
        run_url="https://example.invalid/1",
    )
    fingerprint = first["fingerprint"]
    repeated = MODULE.build_issue_plan(
        _report("coverage_exhausted"),
        [
            {
                "number": 17,
                "body": f"<!-- nfi-futures-discovery:{fingerprint} -->",
            }
        ],
        run_url="https://example.invalid/2",
    )

    assert repeated["create"] is None
    assert repeated["close"] == []


def test_budget_resume_does_not_create_an_issue_and_recovery_closes_old_gap() -> None:
    plan = MODULE.build_issue_plan(
        _report("budget_exhausted"),
        [
            {
                "number": 18,
                "body": "<!-- nfi-futures-discovery:" + "e" * 64 + " -->",
            }
        ],
        run_url="https://example.invalid/3",
    )

    assert plan["fingerprint"] is None
    assert plan["create"] is None
    assert plan["close"] == [18]
    assert plan["recovered"] is True
