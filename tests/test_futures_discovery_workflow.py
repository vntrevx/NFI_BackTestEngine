from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DISCOVERY = ROOT / ".github" / "workflows" / "nfi-futures-discovery.yml"
COMPATIBILITY = ROOT / ".github" / "workflows" / "nfi-compatibility.yml"


def _job(text: str, job_id: str) -> str:
    matches = list(re.finditer(r"(?m)^  [a-z][a-z0-9-]*:\s*$", text))
    selected = next(index for index, match in enumerate(matches) if match.group() == f"  {job_id}:")
    start = matches[selected].start()
    end = matches[selected + 1].start() if selected + 1 < len(matches) else len(text)
    return text[start:end]


def _steps(job: str) -> list[str]:
    matches = list(re.finditer(r"(?m)^      - name: .+$", job))
    return [
        job[match.start() : matches[index + 1].start() if index + 1 < len(matches) else len(job)]
        for index, match in enumerate(matches)
    ]


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
    assert "cancel-in-progress: true" in text
    assert "timeout-minutes: 125" in text
    assert "github.event.workflow_run.conclusion == 'success'" not in text
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
    assert '"discovery/${mode}/latest.json"' in text
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
    assert 'current_status}" = "external_data_deferred"' not in text
    assert "discovery_execution=external_data_deferred" in text
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
    assert "needs.product-status.outputs.required_status_passed == 'true'" in health
    assert 'status: "complete"' in health
    assert "python scripts/futures_discovery_issue.py" in health


def test_discovery_product_status_records_blocked_state_and_authorizes_progress() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")
    resolve = text[text.index("  resolve:") : text.index("  discover:")]
    product = _job(text, "product-status")

    assert "github.event.workflow_run.conclusion == 'success'" not in resolve
    assert '.name == "Latest NFI compatibility" and .status == "completed"' in resolve
    assert 'select(.name == "Preserve ledger and reconcile compatibility issue")' in resolve
    assert "name: NFI product compatibility" in product
    assert "if: always()" in product
    assert "git ls-remote origin refs/heads/main" in product
    assert "refs/heads/main" in product
    assert "compatibility-product-status.json" in product
    assert "--workflow-execution" in product
    assert "STALE_TRIGGER: ${{ needs.resolve.outputs.stale_trigger }}" in product
    assert "workflow_execution=stale" in product
    assert "workflow_execution=infrastructure_limited" in product
    assert "--spot-discovery-execution" in product
    assert "--futures-discovery-execution" in product
    assert "if-no-files-found: error" in product
    assert "Validate product compatibility status schema" in product
    assert "Authorize paired discovery publication independently of product status" in product
    assert "scripts/validate_discovery_publication.py" in product
    assert "Install locked product-status runtime" in product
    assert "uv sync --extra dev --frozen" in product
    assert "uv run python scripts/compatibility_automation.py" in product
    assert "uv run python scripts/validate_discovery_publication.py" in product
    assert "jq -e '.required_status_passed == true'" not in product


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped"])
def test_discovery_side_effects_reject_every_non_success_conclusion(conclusion: str) -> None:
    text = DISCOVERY.read_text(encoding="utf-8")
    publish = _job(text, "publish")
    candidate = _job(text, "candidate-pr")

    assert "needs.discover.result == 'success'" in publish
    assert "needs.product-status.result == 'success'" in publish
    assert "needs.discover.result == 'success'" in candidate
    assert "needs.product-status.result == 'success'" in candidate
    assert "result != 'skipped'" not in publish + candidate
    assert conclusion not in {"success"}


def test_discovery_status_precedes_atomic_publication_and_issue_mutation() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")
    product = _job(text, "product-status")
    publish = _job(text, "publish")
    candidate = _job(text, "candidate-pr")
    health = _job(text, "health")

    assert "      - publish\n" not in product
    assert "      - candidate-pr\n" not in product
    assert "      - product-status\n" in publish
    assert "      - product-status\n" in candidate
    assert "matrix:" not in publish
    assert "discovery/spot/latest.json" in publish
    assert "discovery/futures/latest.json" in publish
    assert "source_run_id" in publish
    assert "scripts/check_discovery_authorization.py" in publish
    assert "nfi-discovery-publication-authorization" in publish
    assert '--stage "product-status=${{ needs.product-status.result }}"' in health


def test_candidate_pr_rechecks_current_refs_immediately_at_mutation_boundary() -> None:
    candidate = _job(DISCOVERY.read_text(encoding="utf-8"), "candidate-pr")
    mutation = next(
        step for step in _steps(candidate)
        if "Open Draft PR and dispatch required CI when exact" in step
    )

    assert "needs.discover.result == 'success'" in candidate
    assert "needs.product-status.result == 'success'" in candidate
    assert "needs.publish.result == 'success'" in candidate
    assert "scripts/check_discovery_authorization.py" in mutation
    assert "jq -e '.required_status_passed == true'" not in mutation
    engine_check = mutation.index("git ls-remote origin refs/heads/main")
    upstream_check = mutation.index("iterativv/NostalgiaForInfinity.git")
    invocation = mutation.index("futures_candidate_pr.py")
    assert engine_check < upstream_check < invocation
    between = mutation[upstream_check:invocation]
    assert "actions/checkout" not in between
    assert "uv sync" not in between
    assert "git commit" not in between
    assert "git push" not in between
    assert "curl " not in between
    assert "--expected-engine-sha" in mutation
    assert "--expected-upstream-sha" in mutation


def test_candidate_job_has_scoped_write_permissions_and_never_merges() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")
    candidate = text[text.index("  candidate-pr:") : text.index("  health:")]

    assert "permissions:\n  actions: read\n  contents: read" in text
    assert "actions: write" in candidate
    assert "contents: write" in candidate
    assert "pull-requests: write" in candidate
    assert "uv run python scripts/futures_candidate_pr.py" in candidate
    assert "nfi-discovery-publication-authorization" in candidate
    assert ".modes[$mode].exact_fixture_draft_pr == true" in candidate
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


def test_discovery_binds_all_checked_runtime_identities_and_advances_modes_atomically() -> None:
    text = DISCOVERY.read_text(encoding="utf-8")

    assert "freqtrade_digest: ${{ steps.identity.outputs.freqtrade_digest }}" in text
    assert "semantic_profile_sha256: ${{ steps.identity.outputs.semantic_profile_sha256 }}" in text
    assert 'test "${freqtrade_digest}" = "${local_digest}"' in text
    assert 'test "${semantic_profile_sha256}" = "${local_profile_sha256}"' in text
    assert ".freqtrade_digest == $freqtrade" in text
    assert ".semantic_profile_sha256 == $semantic_profile" in text
    assert 'name: nfi-branch-discovery-${{ matrix.trading_mode }}' in text
    assert 'run_path="discovery/${mode}/checks/' in text
    assert "${SEMANTIC_PROFILE_SHA256}" in text
    assert "git add discovery/spot discovery/futures" in text


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
