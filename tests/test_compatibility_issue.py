from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compatibility_issue",
    ROOT / "scripts" / "compatibility_issue.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_same_blocker_fingerprint_does_not_create_duplicate_issue() -> None:
    reports = {
        "spot": {"native_compatible": True, "blockers": []},
        "futures": {
            "native_compatible": False,
            "blockers": [{"code": "NEW_OPCODE", "message": "unsupported"}],
        },
    }
    initial = MODULE.build_issue_plan(reports, [], upstream_sha="a" * 40)
    fingerprint = initial["fingerprint"]
    existing = [
        {
            "number": 7,
            "body": (
                f"<!-- nfi-compatibility-fingerprint:{fingerprint} -->"
            ),
        }
    ]

    repeated = MODULE.build_issue_plan(
        reports,
        existing,
        upstream_sha="b" * 40,
    )

    assert repeated["create"] is None
    assert repeated["close"] == []


def test_recovery_closes_open_compatibility_issues() -> None:
    reports = {
        "spot": {"native_compatible": True, "blockers": []},
        "futures": {"native_compatible": True, "blockers": []},
    }
    issues = [
        {
            "number": 9,
            "body": (
                "<!-- nfi-compatibility-fingerprint:"
                + "c" * 64
                + " -->"
            ),
        }
    ]

    plan = MODULE.build_issue_plan(reports, issues, upstream_sha="d" * 40)

    assert plan["fingerprint"] is None
    assert plan["create"] is None
    assert plan["close"] == [9]
    assert plan["recovered"] is True


def test_targeted_coverage_gap_opens_issue_after_static_success() -> None:
    reports = {
        "spot": {"native_compatible": True, "blockers": []},
        "futures": {"native_compatible": True, "blockers": []},
    }
    targeted = {
        "spot": {
            "verification_state": "latest_checked",
            "plan": {"status": "coverage-gap"},
            "blockers": [
                {
                    "code": "TARGETED_COVERAGE_GAP",
                    "message": "new signal has no fixture",
                }
            ],
        },
        "futures": {
            "verification_state": "latest_checked",
            "plan": {"status": "no-changes"},
            "blockers": [],
        },
    }

    plan = MODULE.build_issue_plan(
        reports,
        [],
        upstream_sha="e" * 40,
        targeted_reports=targeted,
    )

    assert plan["create"] is not None
    assert "TARGETED_COVERAGE_GAP" in plan["create"]["body"]
    assert "### spot" in plan["create"]["body"]
    assert "### futures" not in plan["create"]["body"]
