from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compatibility_review_pr",
    ROOT / "scripts" / "compatibility_review_pr.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _decision() -> dict:
    return {
        "schema_version": "1.0.0",
        "identity": {
            "upstream_sha": "a" * 40,
            "engine_sha": "b" * 40,
            "freqtrade_digest": "sha256:" + "c" * 64,
            "semantic_profile_sha256": "d" * 64,
            "strategy_sha256": "e" * 64,
        },
        "trading_mode": "futures",
        "automation_route": "semantic_review_draft_pr",
        "execution_route": "official_only",
        "review_kind": "new_opcode",
        "verification": {"exact": False},
        "action": {
            "native_promotion_allowed": False,
            "draft_pr_allowed": True,
            "draft_pr_kind": "new_opcode",
            "automatic_semantic_merge_allowed": False,
        },
        "action_fingerprint": "f" * 64,
    }


def test_review_plan_is_evidence_only_deterministic_and_allowlisted(tmp_path: Path) -> None:
    first = MODULE.build_review_plan(_decision(), tmp_path)
    second = MODULE.build_review_plan(_decision(), tmp_path)

    assert first == second
    assert first["branch"] == "automation/futures-semantic-review-ffffffffffffffff"
    assert first["destination"] == (
        "planning/compatibility-reviews/futures-ffffffffffffffff.json"
    )
    assert first["document"]["decision"]["execution_route"] == "official_only"
    assert "never merge automatically" in first["document"]["review_requirements"][-1]


def test_review_plan_rejects_native_or_automatically_mergeable_decisions(
    tmp_path: Path,
) -> None:
    native = _decision()
    native["execution_route"] = "native"
    with pytest.raises(ValueError, match="not fail-closed"):
        MODULE.build_review_plan(native, tmp_path)

    mergeable = _decision()
    mergeable["action"]["automatic_semantic_merge_allowed"] = True
    with pytest.raises(ValueError, match="not fail-closed"):
        MODULE.build_review_plan(mergeable, tmp_path)


def test_review_plan_rejects_existing_base_destination(tmp_path: Path) -> None:
    destination = (
        tmp_path
        / "planning"
        / "compatibility-reviews"
        / "futures-ffffffffffffffff.json"
    )
    destination.parent.mkdir(parents=True)
    destination.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        MODULE.build_review_plan(_decision(), tmp_path)


def test_publisher_opens_only_a_draft_without_evidence_only_ci() -> None:
    source = (ROOT / "scripts" / "compatibility_review_pr.py").read_text(
        encoding="utf-8"
    )

    assert '"--draft"' in source
    assert '"workflow"' not in source
    assert '"ci.yml"' not in source
    assert '"pr", "merge"' not in source
    assert '"pr", "review"' not in source


def test_pending_review_deduplicates_engine_only_identity_changes() -> None:
    plan = MODULE.build_review_plan(_decision(), Path.cwd())
    body = (
        "<!-- nfi-semantic-review:futures:" + "1" * 64 + " -->\n\n"
        "- Upstream: `" + "a" * 40 + "`\n"
        "- Engine: `" + "2" * 40 + "`\n"
        "- Review kind: `new_opcode`\n"
    )
    record = {
        "body": body,
        "headRefName": "automation/futures-semantic-review-existing",
        "state": "OPEN",
        "url": "https://example.invalid/pull/1",
    }

    assert MODULE.find_pending_review([record], plan) == record

    changed_upstream = dict(plan)
    changed_upstream["upstream_sha"] = "9" * 40
    assert MODULE.find_pending_review([record], changed_upstream) is None
