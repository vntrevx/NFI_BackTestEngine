from __future__ import annotations

from pathlib import Path

WORKFLOW = (
    Path(__file__).parents[1]
    / ".github"
    / "workflows"
    / "nfi-compatibility.yml"
)


def test_workflow_rechecks_upstream_and_engine_with_manual_force() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert 'cron: "17 */4 * * *"' in text
    assert "force:" in text
    assert "scripts/compatibility_identity.py" in text
    assert "previous_engine_sha" in text
    assert "previous_check_path" in text


def test_workflow_runs_targeted_exact_gate_before_ledger_publish() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "strategy verify-targeted" in text
    assert "needs.targeted.result == 'success'" in text
    assert text.index("Reconcile deduplicated compatibility issue") < text.index(
        "Publish append-only compatibility ledger"
    )
    assert (
        'checks/${UPSTREAM_SHA}/${ENGINE_SHA}/runs/'
        '${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}'
    ) in text


def test_workflow_health_is_separate_from_compatibility_blockers() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "nfi-compatibility" in text
    assert "nfi-automation-health" in text
    assert "scripts/workflow_health_issue.py" in text
    assert "--stage \"publish=${{ needs.publish.result }}\"" in text
