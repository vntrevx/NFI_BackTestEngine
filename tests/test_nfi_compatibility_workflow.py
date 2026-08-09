from __future__ import annotations

from pathlib import Path

WORKFLOW = (
    Path(__file__).parents[1]
    / ".github"
    / "workflows"
    / "nfi-compatibility.yml"
)


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


def test_workflow_seals_both_modes_before_atomic_identity_advancement() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    canary = text[text.index("  canary:") : text.index("  publish:")]
    publish = text[text.index("  publish:") : text.index("  health:")]

    assert "scripts/compatibility_canary.py" in canary
    assert "Seal independent Spot and Futures watcher result" in canary
    assert "latest-nfi-x7-hosted-canary" in canary
    assert "if-no-files-found: error" in canary
    assert "scripts/compatibility_canary.py" in publish
    assert "cmp .compatibility/results/hosted-canary.json" in publish
    assert "compatibility-identity.json" in publish
    assert "hosted-canary.json" in publish
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
        text.index("Collect compact targeted diagnostics") :
        text.index("Preserve ledger and reconcile compatibility issue")
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
    assert "--stage \"publish=${{ needs.publish.result }}\"" in text
