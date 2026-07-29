from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
DISCOVERY = ROOT / ".github" / "workflows" / "nfi-futures-discovery.yml"
COMPATIBILITY = ROOT / ".github" / "workflows" / "nfi-compatibility.yml"


def test_fast_lane_exports_identity_for_separate_deep_search() -> None:
    text = COMPATIBILITY.read_text(encoding="utf-8")

    assert ".compatibility/compatibility-identity.json" in text
    assert "--arg upstream_sha" in text
    assert "--arg engine_sha" in text
    assert "--arg source_sha256" in text


def test_discovery_is_separate_resumable_and_resource_bounded() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")

    assert 'workflows: ["Latest NFI compatibility"]' in text
    assert 'cron: "47 2 * * *"' in text
    assert "'.run_url | split(\"/\")[-1]'" in text
    assert r"split(\"/\")" not in text
    assert "cancel-in-progress: false" in text
    assert "timeout-minutes: 125" in text
    assert "planning/futures-discovery-policy.json" in text
    assert "uv run python scripts/select_discovery_cursor.py" in text
    assert "\n            python scripts/select_discovery_cursor.py" not in text
    assert "--cursor .discovery/previous-cursor.json" in text
    assert "discovery/latest.json" in text
    assert "retention-days: 30" in text


def test_candidate_job_has_scoped_write_permissions_and_never_merges() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")
    candidate = text[text.index("  candidate-pr:") : text.index("  health:")]

    assert "permissions:\n  actions: read\n  contents: read" in text
    assert "actions: write" in candidate
    assert "contents: write" in candidate
    assert "pull-requests: write" in candidate
    assert "uv run python scripts/futures_candidate_pr.py" in candidate
    assert "\n          python scripts/futures_candidate_pr.py" not in candidate
    assert "gh pr merge" not in candidate
    assert "gh pr review" not in candidate
    assert "auto-merge" not in candidate
    assert "pull_request_target" not in text
    assert "secrets." not in text


def test_discovery_semantic_and_infrastructure_issues_are_separate() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")

    assert "scripts/futures_discovery_issue.py" in text
    assert "nfi-futures-discovery" in text
    assert "scripts/futures_discovery_health_issue.py" in text
    assert "nfi-futures-discovery-health" in text
