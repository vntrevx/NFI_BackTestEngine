from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.current_x7_closure import (
    build_current_x7_closure,
    validate_current_x7_closure,
)
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.release_provenance import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = (
    ROOT
    / "benchmarks/evidence/m24/current-x7-e4fb2a7d/closure-analysis.json"
)
CURRENT_EVIDENCE = (
    ROOT
    / "benchmarks/evidence/m24/current-x7-e4fb2a7d-engine-e39439ce/closure-analysis.json"
)
UPSTREAM = "e4fb2a7dabb961a34d54d8052f2deba0105591cb"
BASELINE = "e94a4b2ef941e14f014154469de36bc9a78d46af"
ENGINE = "713e2fe49b6e22212d7f28f274cba5a3c8e8e249"
CURRENT_ENGINE = "e39439ced1db605f5b0065a887f7ff1ca035085c"
OLD_SOURCE = "1" * 64
NEW_SOURCE = "2" * 64
PROFILE = "3" * 64


def _target() -> dict:
    preimage = {
        "kind": "signal",
        "change": "changed",
        "value": "candidate",
        "methods": ["populate_entry_trend"],
        "semantic_callers": ["populate_entry_trend"],
        "tags": ["candidate"],
    }
    payload = json.dumps(
        preimage,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "id": hashlib.sha256(payload).hexdigest(),
        **preimage,
        "proof": {
            "mode": "transition",
            "old_source_spans": [],
            "new_source_spans": [],
            "changed_source_spans": [],
        },
        "runtime_observable": True,
    }


def _inputs() -> tuple[dict, dict, dict, dict]:
    target = _target()
    difference = {
        "schema_version": "1.3.0",
        "classification": "ir-compatible",
        "old": {"sha256": OLD_SOURCE, "commit": BASELINE},
        "new": {"sha256": NEW_SOURCE, "commit": UPSTREAM},
        "changes": {"callbacks": {"changed": ["populate_entry_trend"]}},
        "behavior_targets": [target],
    }
    reports: dict[str, dict] = {}
    requests: dict[str, dict] = {}
    policies: dict[str, dict] = {}
    for index, mode in enumerate(("spot", "futures"), start=4):
        policy_sha = str(index + 2) * 64
        identity = {
            "upstream_commit": UPSTREAM,
            "baseline_upstream_commit": BASELINE,
            "engine_commit": ENGINE,
            "strategy_sha256": NEW_SOURCE,
            "baseline_strategy_sha256": OLD_SOURCE,
            "trading_mode": mode,
            "policy_sha256": policy_sha,
            "target_ids": [target["id"]],
        }
        fingerprint = canonical_sha256(identity)
        reports[mode] = {
            "trading_mode": mode,
            "native_compatible": True,
            "source": {"sha256": NEW_SOURCE},
        }
        requests[mode] = {
            "trading_mode": mode,
            "native_compatible": True,
            "fingerprint": fingerprint,
            "plan_status": "coverage-gap",
            "identity": identity,
            "missing_targets": [target],
            "searchable_targets": [target],
            "unsearchable_targets": [],
            "search": {"timeranges": ["20250101-20260101"]},
        }
        policies[mode] = {
            "trading_mode": mode,
            "universe": {
                "market_catalog": {"path": f"{mode}-catalog.json", "sha256": "8" * 64},
                "market_snapshot": {"path": f"{mode}-snapshot.json", "sha256": "9" * 64},
            },
        }
    return difference, reports, requests, policies


def _build() -> dict:
    difference, reports, requests, policies = _inputs()
    return build_current_x7_closure(
        difference,
        reports,
        requests,
        policies,
        upstream_repository="https://example.invalid/upstream.git",
        upstream_ref="refs/heads/main",
        upstream_commit=UPSTREAM,
        baseline_upstream_commit=BASELINE,
        engine_commit=ENGINE,
        semantic_profile_sha256=PROFILE,
        observed_at="2026-09-03T00:00:00Z",
    )


def test_current_x7_closure_builds_independent_mode_cursors() -> None:
    document = _build()

    assert document["source_analysis"]["target_count"] == 1
    spot = document["modes"]["spot"]["initial_cursor"]
    futures = document["modes"]["futures"]["initial_cursor"]
    assert spot["schema_version"] == "1.0.0"
    assert spot["next_shard"] == 0
    assert spot["shard_count"] == 1
    assert spot["fingerprint"] != futures["fingerprint"]


def test_current_x7_closure_rejects_cross_source_mode_report() -> None:
    difference, reports, requests, policies = _inputs()
    reports["futures"]["source"]["sha256"] = "0" * 64

    with pytest.raises(SpecValidationError, match="futures compatibility identity differs"):
        build_current_x7_closure(
            difference,
            reports,
            requests,
            policies,
            upstream_repository="https://example.invalid/upstream.git",
            upstream_ref="refs/heads/main",
            upstream_commit=UPSTREAM,
            baseline_upstream_commit=BASELINE,
            engine_commit=ENGINE,
            semantic_profile_sha256=PROFILE,
            observed_at="2026-09-03T00:00:00Z",
        )


def test_current_x7_closure_rejects_cursor_target_partition_drift() -> None:
    document = _build()
    document["modes"]["spot"]["searchable_target_ids"] = []
    document["fingerprint"] = canonical_sha256(
        {key: value for key, value in document.items() if key != "fingerprint"}
    )

    with pytest.raises(SpecValidationError, match="spot cursor or target inventory differs"):
        validate_current_x7_closure(document)


def test_current_x7_closure_rejects_fingerprint_drift() -> None:
    document = _build()
    document["observed_at"] = "2026-09-04T00:00:00Z"

    with pytest.raises(SpecValidationError, match="fingerprint differs"):
        validate_current_x7_closure(document)


def test_repository_current_x7_closure_is_identity_bound() -> None:
    evidence = read_json(EVIDENCE)

    validate_current_x7_closure(evidence)
    assert evidence["identity"] == {
        "upstream_repository": "https://github.com/iterativv/NostalgiaForInfinity.git",
        "upstream_ref": "refs/heads/main",
        "upstream_commit": UPSTREAM,
        "baseline_upstream_commit": BASELINE,
        "engine_commit": ENGINE,
        "baseline_source_sha256": (
            "72969d885a7c30d13c25dd02f4d8a0d0bc5cd565596e19abeaf3ede4ddd6f60c"
        ),
        "source_sha256": (
            "35430af20d92548fa86fad95a13778dcebd2acd1a211800625287cea3e67b52d"
        ),
        "semantic_profile_sha256": (
            "5b3c11b13e4a3d9fe00e3231755d5d6369ff9a32957a794bf0ffc807b76ce2a8"
        ),
    }
    assert evidence["source_analysis"]["target_count"] == 5
    for mode in ("spot", "futures"):
        inventory = evidence["modes"][mode]
        assert inventory["plan_status"] == "coverage-gap"
        assert len(inventory["missing_target_ids"]) == 5
        assert inventory["missing_target_ids"] == inventory["searchable_target_ids"]
        assert inventory["unsearchable_target_ids"] == []
        policy = ROOT / f"planning/{mode}-discovery-policy.json"
        assert inventory["policy_sha256"] == sha256_file(policy)


def test_repository_refreshed_current_x7_closure_is_identity_bound() -> None:
    evidence = read_json(CURRENT_EVIDENCE)

    validate_current_x7_closure(evidence)
    assert evidence["identity"]["upstream_commit"] == UPSTREAM
    assert evidence["identity"]["baseline_upstream_commit"] == BASELINE
    assert evidence["identity"]["engine_commit"] == CURRENT_ENGINE
    assert evidence["source_analysis"]["target_count"] == 5
    for mode in ("spot", "futures"):
        inventory = evidence["modes"][mode]
        assert inventory["plan_status"] == "coverage-gap"
        assert inventory["missing_target_ids"] == inventory["target_ids"]
        assert inventory["missing_target_ids"] == inventory["searchable_target_ids"]
        assert inventory["unsearchable_target_ids"] == []
        assert inventory["initial_cursor"]["next_shard"] == 0


def test_current_x7_closure_evidence_mutation_is_detected() -> None:
    evidence = deepcopy(read_json(EVIDENCE))
    evidence["modes"]["futures"]["initial_cursor"]["next_shard"] = 1

    with pytest.raises(SpecValidationError, match="fingerprint differs"):
        validate_current_x7_closure(evidence)
