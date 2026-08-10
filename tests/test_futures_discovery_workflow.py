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
    assert "--arg freqtrade_digest" in text
    assert "--arg semantic_profile_sha256" in text
    assert "--arg baseline_upstream_sha" in text
    assert "--arg source_sha256" in text
    assert ".compatibility/old.py" in text
    assert "scripts/resolve_upstream_source.py" in text


def test_discovery_is_separate_resumable_and_resource_bounded() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")

    assert 'workflows: ["Latest NFI compatibility"]' in text
    assert 'cron: "47 2 * * *"' in text
    assert "'.run_url | split(\"/\")[-1]'" in text
    assert r"split(\"/\")" not in text
    assert "cancel-in-progress: false" in text
    assert "timeout-minutes: 125" in text
    assert "github.event.workflow_run.conclusion == 'success'" in text
    assert "stale_trigger: ${{ steps.identity.outputs.stale_trigger }}" in text
    assert 'EVENT_NAME: ${{ github.event_name }}' in text
    assert '[ "${EVENT_NAME}" = "workflow_run" ]' in text
    assert "A newer successful compatibility run owns discovery" in text
    assert 'echo "stale_trigger=true"' in text
    assert 'echo "stale_trigger=false"' in text
    assert 'test "${source_run_id}" = "${ledger_run_id}"' in text
    assert "trading_mode:\n          - spot\n          - futures" in text
    assert 'strategy discover \\' in text
    assert '--trading-mode "${MODE}"' in text
    assert '"planning/${MODE}-discovery-policy.json"' in text
    assert "uv run python scripts/select_discovery_cursor.py" in text
    assert "\n            python scripts/select_discovery_cursor.py" not in text
    assert "--cursor .discovery/previous-cursor.json" in text
    assert "--baseline-source .discovery/input/old.py" in text
    assert '--baseline-upstream-commit "${baseline_commit}"' in text
    assert '"discovery/${MODE}/latest.json"' in text
    assert "${ORACLE_KEY}" in text
    assert "retry_deferred:" in text
    assert "mkdir -p .discovery" in text
    restore = text[
        text.index("      - name: Restore same-mode matching cursor") :
        text.index("      - name: Install uv, Python, and engine")
    ]
    assert restore.index("mkdir -p .discovery") < restore.index(
        'cp "${ledger_dir}/${cursor_path}" .discovery/downloaded-cursor.json'
    )
    assert "steps.restore.outputs.deferred != 'true'" in text
    assert '"identity-change-or-manual"' not in text
    assert ".storage.compact_artifact_retention_days" in text
    assert ".candidate.artifact_retention_days" in text
    assert "retention-days: ${{ steps.retention.outputs.days }}" in text
    assert 'current_status}" = "external_data_deferred"' in text
    assert 'steps.candidate.outputs.found == \'true\'' in text
    assert "deep_search_required: ${{ steps.identity.outputs.deep_search_required }}" in text
    assert "spot_search_required: ${{ steps.identity.outputs.spot_search_required }}" in text
    assert "futures_search_required: ${{ steps.identity.outputs.futures_search_required }}" in text
    assert 'needs.resolve.outputs.deep_search_required == \'true\'' in text
    assert 'jq -er .automation_route "${decision}"' in text
    assert '"bounded_discovery"' in text
    assert "scripts/compatibility_automation.py" in text
    assert "automation-decision.json" in text


def test_exact_fast_lane_closes_discovery_gaps_without_running_deep_search() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")
    health = text[text.index("  health:") :]

    assert "Reconcile gaps already proven exact by the fast lane" in health
    assert "needs.resolve.outputs.deep_search_required == 'false'" in health
    assert 'status: "complete"' in health
    assert "python scripts/futures_discovery_issue.py" in health


def test_candidate_job_has_scoped_write_permissions_and_never_merges() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")
    candidate = text[text.index("  candidate-pr:") : text.index("  health:")]

    assert "permissions:\n  actions: read\n  contents: read" in text
    assert "actions: write" in candidate
    assert "contents: write" in candidate
    assert "pull-requests: write" in candidate
    assert "uv run python scripts/futures_candidate_pr.py" in candidate
    assert '"exact_fixture_draft_pr"' in candidate
    assert "\n          python scripts/futures_candidate_pr.py" not in candidate
    assert "gh pr merge" not in candidate
    assert "gh pr review" not in candidate
    assert "auto-merge" not in candidate
    assert "pull_request_target" not in text
    assert "secrets." not in text


def test_discovery_semantic_and_infrastructure_issues_are_separate() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")

    assert "scripts/futures_discovery_issue.py" in text
    assert "nfi-branch-discovery" in text
    assert "scripts/futures_discovery_health_issue.py" in text
    assert "nfi-branch-discovery-health" in text


def test_discovery_binds_all_checked_runtime_identities_and_keeps_modes_independent() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")

    assert "freqtrade_digest: ${{ steps.identity.outputs.freqtrade_digest }}" in text
    assert "semantic_profile_sha256: ${{ steps.identity.outputs.semantic_profile_sha256 }}" in text
    assert 'test "${freqtrade_digest}" = "${local_digest}"' in text
    assert 'test "${semantic_profile_sha256}" = "${local_profile_sha256}"' in text
    assert ".freqtrade_digest == $freqtrade" in text
    assert ".semantic_profile_sha256 == $semantic_profile" in text
    assert 'name: nfi-branch-discovery-${{ matrix.trading_mode }}' in text
    assert 'run_path="discovery/${MODE}/checks/' in text
    assert "${SEMANTIC_PROFILE_SHA256}" in text
    assert 'git add "discovery/${MODE}"' in text


def test_deferred_reuse_requires_a_fail_closed_classification_canary() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")
    restore = text[
        text.index("      - name: Restore same-mode matching cursor") :
        text.index("      - name: Install uv, Python, and engine")
    ]

    assert 'previous_decision="${ledger_dir}/${run_path}/automation-decision.json"' in restore
    assert '.automation_route == "external_data_deferred"' in restore
    assert '.execution_route == "official_only"' in restore
    assert '.verification.exact == false' in restore
    assert ".action.external_data_deferred_is_exact == false" in restore
    assert "Validate deferred reuse remains official-only" in text
    assert ".action.automatic_semantic_merge_allowed == false" in text
