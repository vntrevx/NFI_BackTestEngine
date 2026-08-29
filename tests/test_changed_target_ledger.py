from __future__ import annotations

import copy
from pathlib import Path

import pytest
from changed_target_ledger_support import HEAD, _documents, _target
from nfi_backtest_engine import changed_target_ledger
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.changed_target_ledger import build_changed_target_ledger
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.semantic_registry import _registry_fingerprint


def _reseal_registry(sources) -> None:
    registry = read_json(sources.semantic_registry)
    registry["fingerprint"] = _registry_fingerprint(registry)
    write_json(sources.semantic_registry, registry)
    fingerprint = registry["fingerprint"]
    fixtures = read_json(sources.fixture_registry)
    for bundle in fixtures["bundles"]:
        bundle["semantic_registry_fingerprint"] = fingerprint
    write_json(sources.fixture_registry, fixtures)
    for path in sources.targeted_reports.values():
        report = read_json(path)
        report["semantic_registry_fingerprint"] = fingerprint
        for run in report["runs"]:
            run["semantic_registry_fingerprint"] = fingerprint
            run["capture"]["semantic_registry_fingerprint"] = fingerprint
        write_json(path, report)


def test_ledger_is_deterministic_complete_and_covers_all_modes(tmp_path: Path) -> None:
    sources = _documents(tmp_path)

    first = build_changed_target_ledger(sources)
    second = build_changed_target_ledger(sources)

    assert first == second
    assert first["identity"]["upstream_head"] == HEAD
    assert first["summary"] == {
        "target_count": 1,
        "blocked_target_count": 0,
        "hard_blocker_count": 0,
        "native_promotion_allowed": True,
    }
    target = first["targets"][0]
    assert target["affected_modes"] == ["futures", "spot"]
    assert target["dependencies"] == ["helpers.py"]
    assert target["ownership"]["mapping"] == "compiled-program"
    assert [proof["trading_mode"] for proof in target["mode_proofs"]] == ["futures", "spot"]


def test_nested_helper_change_fans_out_to_every_semantic_caller(tmp_path: Path) -> None:
    target = _target(
        "2",
        value="leaf",
        callers=["populate_entry_trend", "populate_exit_trend"],
    )
    sources = _documents(tmp_path, targets=[target])
    registry = read_json(sources.semantic_registry)
    registry["obligation_groups"][0]["kind"] = "ast-node"
    registry["obligation_groups"][0]["obligations"][0]["preimage"][
        "normalized_semantics"
    ][0] = "@strategy:leaf:Compare"
    write_json(sources.semantic_registry, registry)
    _reseal_registry(sources)

    report = build_changed_target_ledger(sources)

    assert report["targets"][0]["semantic_callers"] == [
        "populate_entry_trend",
        "populate_exit_trend",
    ]
    assert report["targets"][0]["reachability"]["transitive"] is True


@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
def test_missing_or_duplicate_static_ownership_is_a_hard_blocker(
    tmp_path: Path,
    mutation: str,
) -> None:
    sources = _documents(tmp_path)
    registry = read_json(sources.semantic_registry)
    obligations = registry["obligation_groups"][0]["obligations"]
    if mutation == "missing":
        obligations.clear()
    else:
        obligations.append(copy.deepcopy(obligations[0]))
    write_json(sources.semantic_registry, registry)
    _reseal_registry(sources)

    report = build_changed_target_ledger(sources)

    assert report["summary"]["native_promotion_allowed"] is False
    assert {item["code"] for item in report["hard_blockers"]} >= {
        "MISSING_STATIC_OWNERSHIP"
        if mutation == "missing"
        else "DUPLICATE_STATIC_OWNERSHIP"
    }


@pytest.mark.parametrize(
    "mutation",
    ["missing-fixture", "stale-fixture", "missing-proof", "stale-proof"],
)
def test_stale_or_missing_fixture_and_proof_block_promotion(
    tmp_path: Path,
    mutation: str,
) -> None:
    sources = _documents(tmp_path)
    if mutation in {"missing-fixture", "stale-fixture"}:
        registry = read_json(sources.fixture_registry)
        bundle = registry["bundles"][0]
        if mutation == "missing-fixture":
            registry["bundles"].remove(bundle)
        else:
            bundle["upstream_commit"] = "9" * 40
        write_json(sources.fixture_registry, registry)
    elif mutation == "missing-proof":
        sources.targeted_reports["spot"].unlink()
    else:
        proof = read_json(sources.targeted_reports["spot"])
        proof["upstream_commit"] = "9" * 40
        write_json(sources.targeted_reports["spot"], proof)

    report = build_changed_target_ledger(sources)

    assert report["summary"]["native_promotion_allowed"] is False
    assert any(item["trading_mode"] == "spot" for item in report["hard_blockers"])


def test_malformed_source_diff_identity_fails_without_publication(tmp_path: Path) -> None:
    sources = _documents(tmp_path)
    difference = read_json(sources.strategy_diff)
    difference["new"]["sha256"] = "malformed"
    write_json(sources.strategy_diff, difference)
    destination = tmp_path / "ledger.json"

    with pytest.raises(SpecValidationError, match="strategy diff"):
        build_changed_target_ledger(sources, output_path=destination)

    assert not destination.exists()


def test_green_infrastructure_with_unresolved_target_cannot_promote(tmp_path: Path) -> None:
    sources = _documents(tmp_path)
    proof = read_json(sources.targeted_reports["spot"])
    proof["runs"][0]["target_ids"] = []
    proof["runs"][0]["coverage"]["reached_target_ids"] = []
    write_json(sources.targeted_reports["spot"], proof)

    report = build_changed_target_ledger(sources)

    assert report["summary"]["native_promotion_allowed"] is False
    assert "UNRESOLVED_CHANGED_TARGET" in {
        item["code"] for item in report["hard_blockers"]
    }


def test_interrupted_publication_leaves_no_authoritative_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _documents(tmp_path)
    destination = tmp_path / "ledger.json"

    def interrupt(_temporary: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(changed_target_ledger, "_publication_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        build_changed_target_ledger(sources, output_path=destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".ledger.json.*.tmp"))
