from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from changed_target_ledger_support import _documents
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.changed_target_ledger import (
    _sha256_json,
    build_changed_target_ledger,
)
from nfi_backtest_engine.changed_target_workflow import validate_changed_target_promotion
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.semantic_registry import _registry_fingerprint


def _codes(report: dict) -> set[str]:
    return {str(item["code"]) for item in report["hard_blockers"]}


def _reseal_registry(sources) -> str:
    registry = read_json(sources.semantic_registry)
    registry["fingerprint"] = _registry_fingerprint(registry)
    write_json(sources.semantic_registry, registry)
    fingerprint = registry["fingerprint"]
    fixtures = read_json(sources.fixture_registry)
    for bundle in fixtures["bundles"]:
        bundle["semantic_registry_fingerprint"] = fingerprint
    write_json(sources.fixture_registry, fixtures)
    for path in sources.targeted_reports.values():
        targeted = read_json(path)
        targeted["semantic_registry_fingerprint"] = fingerprint
        for run in targeted["runs"]:
            run["semantic_registry_fingerprint"] = fingerprint
            run["capture"]["semantic_registry_fingerprint"] = fingerprint
        write_json(path, targeted)
    return fingerprint


def test_removed_fixture_link_blocks_resolved_proof(tmp_path: Path) -> None:
    sources = _documents(tmp_path)
    fixtures = read_json(sources.fixture_registry)
    fixtures["bundles"][0]["fixture_ids"].remove("candidate-spot")
    write_json(sources.fixture_registry, fixtures)

    report = build_changed_target_ledger(sources)

    assert report["summary"]["native_promotion_allowed"] is False
    assert "MISSING_FIXTURE_LINK" in _codes(report)


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("report", "semantic_profile_sha256"),
        ("report", "semantic_registry_fingerprint"),
        ("report", "freqtrade_digest"),
        ("run", "oracle_digest"),
        ("capture", "upstream_commit"),
        ("capture", "source_sha256"),
    ],
)
def test_crossed_proof_identity_is_a_typed_hard_blocker(
    tmp_path: Path,
    location: str,
    field: str,
) -> None:
    sources = _documents(tmp_path)
    targeted = read_json(sources.targeted_reports["spot"])
    selected = targeted if location == "report" else targeted["runs"][0]
    if location == "capture":
        selected = selected["capture"]
    selected[field] = "9" * (40 if field == "upstream_commit" else 64)
    write_json(sources.targeted_reports["spot"], targeted)

    report = build_changed_target_ledger(sources)

    assert report["summary"]["native_promotion_allowed"] is False
    assert _codes(report) & {"STALE_TARGETED_PROOF", "STALE_ORACLE_PROOF"}


def test_blocked_top_level_report_overrides_green_nested_run(tmp_path: Path) -> None:
    sources = _documents(tmp_path)
    targeted = read_json(sources.targeted_reports["spot"])
    targeted["complete"] = False
    targeted["verification_state"] = "latest_checked"
    targeted["proof"]["complete"] = False
    targeted["blockers"] = [{"code": "REPORT_BLOCKED", "message": "blocked"}]
    targeted["qualification"]["blockers"] = [
        {"code": "QUALIFICATION_BLOCKED", "message": "blocked"}
    ]
    write_json(sources.targeted_reports["spot"], targeted)

    report = build_changed_target_ledger(sources)

    assert report["summary"]["native_promotion_allowed"] is False
    assert "BLOCKED_TARGETED_REPORT" in _codes(report)


def test_duplicate_semantic_preimage_with_distinct_ids_blocks(tmp_path: Path) -> None:
    sources = _documents(tmp_path)
    registry = read_json(sources.semantic_registry)
    duplicate = copy.deepcopy(registry["obligation_groups"][0]["obligations"][0])
    duplicate["obligation_id"] = "obl-signal-" + "9" * 64
    registry["obligation_groups"][0]["obligations"].append(duplicate)
    write_json(sources.semantic_registry, registry)
    _reseal_registry(sources)

    report = build_changed_target_ledger(sources)

    assert report["summary"]["native_promotion_allowed"] is False
    assert "DUPLICATE_STATIC_OWNERSHIP" in _codes(report)


@pytest.mark.parametrize("mutation", ["ownership", "target"])
def test_unreachable_ownership_or_target_blocks(tmp_path: Path, mutation: str) -> None:
    sources = _documents(tmp_path)
    if mutation == "ownership":
        registry = read_json(sources.semantic_registry)
        registry["obligation_groups"][0]["reachability"] = "unreachable"
        write_json(sources.semantic_registry, registry)
        _reseal_registry(sources)
    else:
        difference = read_json(sources.strategy_diff)
        difference["behavior_targets"][0]["methods"] = []
        difference["behavior_targets"][0]["semantic_callers"] = []
        identity = {
            key: difference["behavior_targets"][0][key]
            for key in ("kind", "change", "value", "methods", "semantic_callers", "tags")
        }
        difference["behavior_targets"][0]["id"] = _sha256_json(identity)
        write_json(sources.strategy_diff, difference)

    report = build_changed_target_ledger(sources)

    assert report["summary"]["native_promotion_allowed"] is False
    assert _codes(report) & {"UNREACHABLE_STATIC_OWNERSHIP", "UNREACHABLE_CHANGED_TARGET"}


@pytest.mark.parametrize("mutation", ["baseline", "target", "registry", "profile", "source"])
def test_unbound_identity_rejects_before_publication(tmp_path: Path, mutation: str) -> None:
    sources = _documents(tmp_path)
    if mutation == "baseline":
        sources = replace(sources, baseline_commit="9" * 40)
    elif mutation == "target":
        difference = read_json(sources.strategy_diff)
        difference["behavior_targets"][0]["value"] = "999"
        write_json(sources.strategy_diff, difference)
    elif mutation in {"registry", "profile"}:
        registry = read_json(sources.semantic_registry)
        key = "fingerprint" if mutation == "registry" else "fingerprint"
        owner = registry if mutation == "registry" else registry["freqtrade"]["semantic_profile"]
        owner[key] = "9" * 64
        write_json(sources.semantic_registry, registry)
    else:
        (tmp_path / "new.py").write_bytes(b"crossed source")
    destination = tmp_path / "ledger.json"

    with pytest.raises(SpecValidationError):
        build_changed_target_ledger(sources, output_path=destination)

    assert not destination.exists()


def test_workflow_rejects_blocker_boolean_count_and_fingerprint_contradictions(
    tmp_path: Path,
) -> None:
    sources = _documents(tmp_path)
    ledger = build_changed_target_ledger(sources)
    target = ledger["targets"][0]
    blocker = {"code": "REVIEW_BLOCKER", "target_id": target["target_id"]}
    target["hard_blockers"] = [blocker]
    ledger["hard_blockers"] = [blocker]
    ledger["fingerprint"] = _sha256_json({k: v for k, v in ledger.items() if k != "fingerprint"})
    identity = ledger["identity"]
    decision = {
        "identity": {
            "upstream_sha": identity["upstream_head"],
            "strategy_sha256": identity["new_source_sha256"],
            "freqtrade_digest": identity["freqtrade_digest"],
            "semantic_profile_sha256": identity["semantic_profile_sha256"],
        },
        "action": {"native_promotion_allowed": True},
    }

    with pytest.raises(SpecValidationError, match="internally inconsistent"):
        validate_changed_target_promotion(ledger, {"futures": decision, "spot": decision})

    ledger["fingerprint"] = hashlib.sha256(b"wrong").hexdigest()
    with pytest.raises(SpecValidationError, match="fingerprint"):
        validate_changed_target_promotion(ledger, {"futures": decision, "spot": decision})
