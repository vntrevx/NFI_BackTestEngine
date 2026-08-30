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
    assert repeated["update"]["number"] == 7
    assert f"Upstream commit: `{'b' * 40}`" in repeated["update"]["body"]


def test_changed_blocker_reuses_the_existing_compatibility_issue() -> None:
    previous_reports = {
        "spot": {"native_compatible": False, "blockers": [{"code": "OLD"}]},
        "futures": {"native_compatible": True, "blockers": []},
    }
    previous = MODULE.build_issue_plan(
        previous_reports,
        [],
        upstream_sha="a" * 40,
    )
    issues = [
        {
            "number": 7,
            "title": previous["create"]["title"],
            "body": previous["create"]["body"],
        },
        {
            "number": 8,
            "title": "Older compatibility blocker",
            "body": "<!-- nfi-compatibility-fingerprint:" + "f" * 64 + " -->",
        },
    ]
    current_reports = {
        "spot": {
            "native_compatible": False,
            "blockers": [{"code": "NEW", "message": "changed lowering"}],
        },
        "futures": {"native_compatible": True, "blockers": []},
    }

    plan = MODULE.build_issue_plan(
        current_reports,
        issues,
        upstream_sha="b" * 40,
    )

    assert plan["create"] is None
    assert plan["update"]["number"] == 7
    assert "NEW" in plan["update"]["body"]
    assert f"Upstream commit: `{'b' * 40}`" in plan["update"]["body"]
    assert plan["close"] == [8]


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


def test_issue_packet_includes_identity_route_coverage_and_run_link() -> None:
    reports = {
        "spot": {
            "native_compatible": False,
            "blockers": [
                {
                    "code": "EXACT_LOWERING_REVIEW_REQUIRED",
                    "message": "system adjustment shape changed",
                }
            ],
        },
        "futures": {"native_compatible": True, "blockers": []},
    }
    targeted = {
        "spot": {
            "verification_state": "latest_checked",
            "plan": {"status": "coverage-gap", "missing_targets": ["a", "b"]},
            "blockers": [],
        },
        "futures": {
            "verification_state": "quick_verified",
            "plan": {"status": "complete", "missing_targets": []},
            "blockers": [],
        },
    }
    identity = {
        "engine_sha": "b" * 40,
        "freqtrade_digest": "sha256:" + "c" * 64,
        "semantic_profile_sha256": "d" * 64,
        "source_sha256": "e" * 64,
    }
    decisions = {
        "spot": {
            "automation_route": "semantic_review_issue",
            "review_kind": "generic_lowering",
        },
        "futures": {
            "automation_route": "native_exact",
            "review_kind": None,
        },
    }

    result = MODULE.build_issue_plan(
        reports,
        [],
        upstream_sha="a" * 40,
        targeted_reports=targeted,
        identity=identity,
        decisions=decisions,
        run_url="https://github.example/actions/runs/123",
    )

    body = result["create"]["body"]
    assert f"- Engine commit: `{'b' * 40}`" in body
    assert "- Automation route: `semantic_review_issue`" in body
    assert "- Review kind: `generic_lowering`" in body
    assert "- Missing behavior targets: `2`" in body
    assert "https://github.example/actions/runs/123" in body
