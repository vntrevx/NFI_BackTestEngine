from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = (
    Path(__file__).parents[1]
    / ".github"
    / "workflows"
    / "nfi-compatibility.yml"
)


def _job(text: str, job_id: str) -> str:
    matches = list(re.finditer(r"(?m)^  [a-z][a-z0-9-]*:\s*$", text))
    selected = next(index for index, match in enumerate(matches) if match.group() == f"  {job_id}:")
    start = matches[selected].start()
    end = matches[selected + 1].start() if selected + 1 < len(matches) else len(text)
    return text[start:end]


def test_workflow_rechecks_four_part_identity_every_four_hours_with_manual_force() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert 'cron: "17 */4 * * *"' in text
    assert "force:" in text
    assert "scripts/compatibility_identity.py" in text
    assert "previous_engine_sha" in text
    assert "previous_freqtrade_digest" in text
    assert "previous_semantic_profile_sha256" in text
    assert '--freqtrade-digest "${freqtrade_digest}"' in text
    assert '--semantic-profile-sha256 "${semantic_profile_sha256}"' in text
    assert "previous_check_path" in text
    assert 'baseline_sha="${previous_sha}"' in text
    assert 'baseline_sha="${stored_baseline_sha:-${previous_sha}}"' in text


def test_workflow_runs_targeted_exact_gate_before_ledger_publish() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "strategy verify-targeted" in text
    assert "needs.targeted.result == 'success'" in text
    assert "needs.canary.result == 'success'" in text
    assert text.index("Reconcile deduplicated compatibility issue") < text.index(
        "Publish append-only compatibility ledger"
    )
    assert (
        'checks/${UPSTREAM_SHA}/${ENGINE_SHA}/${oracle_key}/${profile_key}/runs/'
        '${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}'
    ) in text
    assert "freqtrade_digest: $freqtrade_digest" in text
    assert "semantic_profile_sha256: $semantic_profile_sha256" in text
    assert "baseline_upstream_sha: $baseline_upstream_sha" in text


def test_workflow_exports_paired_sources_and_four_part_identity() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert ".compatibility/old.py" in text
    assert ".compatibility/new.py" in text
    assert ".compatibility/compatibility-identity.json" in text
    assert "--arg freqtrade_digest" in text
    assert "--arg semantic_profile_sha256" in text
    assert "--arg baseline_upstream_sha" in text
    assert "scripts/resolve_upstream_source.py" in text
    assert 'jq -er \'.old.sha256\'' in text
    assert 'sha256sum .compatibility/old.py' in text
    assert ".compatibility/baseline-resolution.json" in text


def test_workflow_builds_changed_target_ledger_only_for_native_routes() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    canary = text[text.index("  canary:") : text.index("  publish:")]
    publish = text[text.index("  publish:") : text.index("  health:")]

    assert "scripts/changed_target_ledger.py" in canary
    assert "strategy semantic-registry-packaged-integrity" in canary
    assert "freqtrade-nfi-semantic-obligation-registry.json.gz" in canary
    assert "--semantic-registry .compatibility/results/semantic-registry.json" in canary
    assert '--upstream-head "$(jq -er .upstream_sha' in canary
    assert '--baseline-commit "$(jq -er .baseline_upstream_sha' in canary
    assert "scripts/validate_changed_target_promotion.py" in canary
    assert 'all(.[]; .execution_route == "native")' in canary
    assert canary.index('all(.[]; .execution_route == "native")') < canary.index(
        "strategy semantic-registry-packaged-integrity"
    )
    assert canary.index("Generate current-HEAD changed-target ledger") < canary.index(
        "Validate four-identity dual-mode completion"
    )
    assert "changed-target-ledger.json" in publish
    assert publish.index("validate_changed_target_promotion.py") < publish.index(
        "Publish append-only compatibility ledger"
    )
    assert "if test -f .compatibility/results/changed-target-ledger.json" in publish


def test_workflow_seals_both_modes_before_atomic_identity_advancement() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    canary = text[text.index("  canary:") : text.index("  publish:")]
    publish = text[text.index("  publish:") : text.index("  health:")]

    assert "scripts/compatibility_canary.py" in canary
    assert "--decisions .compatibility/results" in canary
    assert "Seal independent Spot and Futures watcher result" in canary
    assert "latest-nfi-x7-hosted-canary" in canary
    assert "if-no-files-found: error" in canary
    assert "scripts/compatibility_canary.py" in publish
    assert "cmp .compatibility/results/hosted-canary.json" in publish
    assert "compatibility-identity.json" in publish
    assert "hosted-canary.json" in publish
    assert "automation-decision-spot.json" in publish
    assert "automation-decision-futures.json" in publish
    assert publish.index("Revalidate atomic hosted canary") < publish.index(
        "Publish append-only compatibility ledger"
    )


def test_workflow_materializes_only_digest_bound_release_fixtures() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "scripts/compatibility_fixture_registry.py select" in text
    assert "planning/compatibility-fixtures.json" in text
    assert 'gh release download "${release_tag}"' in text
    assert '--pattern "${asset_name}"' in text
    assert "scripts/compatibility_fixture_registry.py materialize" in text
    assert "--source-sha256" in text
    assert "--freqtrade-digest" in text


def test_workflow_retains_compact_targeted_diagnostics_on_failure() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    diagnostics = text[
        text.index("Collect compact targeted diagnostics") : text.index("  canary:")
    ]

    assert diagnostics.count("if: always()") == 2
    assert "-name run.json" in diagnostics
    assert "-name stderr.log" in diagnostics
    assert "-name stdout.log" in diagnostics
    assert ".nfitrace" not in diagnostics
    assert 'relative="${file#"${source}/"}"' in diagnostics
    assert 'target="${destination}/${relative}"' in diagnostics
    assert "cp --parents" not in diagnostics


def test_workflow_health_is_separate_from_compatibility_blockers() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "nfi-compatibility" in text
    assert "nfi-automation-health" in text
    assert "scripts/workflow_health_issue.py" in text
    assert '--stage "canary=${{ needs.canary.result }}"' in text
    assert "semantic-review" not in text
    assert "--stage \"publish=${{ needs.publish.result }}\"" in text


def test_product_status_precedes_mutation_without_treating_blocker_as_job_failure() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    product = _job(text, "product-status")
    publish = _job(text, "publish")

    assert "      - publish\n" not in product
    assert "      - product-status\n" in publish
    assert "needs.product-status.result == 'success'" in publish
    assert "jq -e '.required_status_passed == true'" not in publish
    validation = '(.schema_version == "compatibility-product-status-v1")'
    assert publish.index(validation) < publish.index("scripts/compatibility_issue.py")
    assert publish.index(validation) < publish.index(
        "git push --quiet origin HEAD:compatibility-ledger"
    )


def test_unchanged_status_uses_authoritative_ledger_without_fallback_identity() -> None:
    product = _job(WORKFLOW.read_text(encoding="utf-8"), "product-status")

    assert "compatibility-ledger" in product
    assert "compatibility-proof-manifest.json" in product
    assert "latest.json" in product
    assert "printf '0%.0s'" not in product
    assert "needs.publish.result" not in product


def test_workflow_emits_a_required_product_status_separate_from_health() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    product = _job(text, "product-status")
    health = text[text.index("  health:") :]

    assert "name: NFI product compatibility" in product
    assert "if: always()" in product
    assert "scripts/compatibility_automation.py" in product
    assert "--workflow-execution" in product
    assert "--proof-dir" in product
    assert "--source-run-id" in product
    assert "--spot-discovery-execution not_required" in product
    assert "--futures-discovery-execution not_required" in product
    assert "compatibility-product-status.json" in product
    assert "if-no-files-found: error" in product
    assert "required_status_passed: ${{ steps.status.outputs.required_status_passed }}" in product
    assert "required_status_passed must be boolean" in product
    assert "Enforce required product compatibility" not in product
    assert "      - product-status\n" in health
    assert '--stage "product-status=${{ needs.product-status.result }}"' in health


def test_workflow_routes_blocked_semantics_to_one_issue_and_never_opens_review_prs() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    targeted = text[text.index("  targeted:") : text.index("  canary:")]
    publish = text[text.index("  publish:") : text.index("  health:")]

    assert "scripts/compatibility_automation.py" in targeted
    assert "automation-decision-${{ matrix.trading_mode }}.json" in targeted
    assert "semantic_review_issue" not in text  # Routing stays inside the decision JSON.
    assert "scripts/compatibility_issue.py" in publish
    assert "Reconcile deduplicated compatibility issue" in publish
    assert "compatibility_review_pr.py" not in text
    assert "pull-requests: write" not in text
    assert "gh pr create" not in text


def test_blocked_observation_is_published_without_native_promotion_proof() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    product = _job(text, "product-status")
    publish = _job(text, "publish")

    assert 'test -f "${proof}/changed-target-ledger.json"' in product
    assert 'test -f "${proof}/hosted-canary.json"' in product
    assert "seal=(--seal-proof-manifest)" in product
    assert (
        "if test -f "
        ".compatibility/results/changed-target-ledger.json"
    ) in publish
    assert "required_status_passed: $required_status_passed" in publish
