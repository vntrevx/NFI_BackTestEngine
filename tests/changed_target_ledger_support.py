"""Fixtures for changed-target ledger contract tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.changed_target_ledger import ChangedTargetLedgerSources
from nfi_backtest_engine.semantic_registry import (
    _registry_fingerprint,
    _source_closure_merkle,
)

OLD_SOURCE = b"old source"
NEW_SOURCE = b"new source with change"
OLD_SOURCE_SHA = hashlib.sha256(OLD_SOURCE).hexdigest()
SOURCE_SHA = hashlib.sha256(NEW_SOURCE).hexdigest()
HEAD = "b" * 40
BASELINE = "c" * 40
ORACLE = "sha256:" + "d" * 64
PROFILE = "e" * 64
REGISTRY_FINGERPRINT = "f" * 64

__all__ = ["HEAD", "_documents", "_target"]

type JsonValue = str | int | bool | None | list[JsonValue] | dict[str, JsonValue]


def _target(
    identifier: str,
    *,
    value: str,
    callers: list[str] | None = None,
) -> Mapping[str, JsonValue]:
    del identifier
    identity = {
        "kind": "signal" if value.isdigit() else "callback",
        "change": "changed",
        "value": value,
        "methods": ["leaf" if callers else "populate_entry_trend"],
        "semantic_callers": callers or ["populate_entry_trend"],
        "tags": [value],
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return {
        "id": hashlib.sha256(payload).hexdigest(),
        **identity,
        "proof": {
            "mode": "transition",
            "old_source_spans": [
                {"method": "leaf", "line": 8, "column": 4, "end_line": 8, "end_column": 20}
            ],
            "new_source_spans": [
                {"method": "leaf", "line": 8, "column": 4, "end_line": 8, "end_column": 20}
            ],
        },
        "runtime_observable": True,
    }


def _documents(
    tmp_path: Path,
    *,
    targets: list[Mapping[str, JsonValue]] | None = None,
) -> ChangedTargetLedgerSources:
    selected = targets or [_target("1", value="562")]
    difference = {
        "schema_version": "1.3.0",
        "selected_class": "Demo",
        "old": {
            "name": "old.py",
            "bytes": len(OLD_SOURCE),
            "sha256": OLD_SOURCE_SHA,
            "commit": BASELINE,
        },
        "new": {
            "name": "new.py",
            "bytes": len(NEW_SOURCE),
            "sha256": SOURCE_SHA,
            "commit": HEAD,
        },
        "classification": "ir-compatible",
        "changes": {"opcodes": {"added": [], "removed": []}},
        "behavior_targets": selected,
        "diagnostics": {"old": [], "new": []},
    }
    registry = {
        "schema_version": "semantic-obligation-registry-v1",
        "freqtrade": {
            "reference": {"image_index_digest": ORACLE},
            "semantic_profile": {"fingerprint": PROFILE},
        },
        "strategy": {
            "source": {
                "path": "Demo.py",
                "bytes": len(NEW_SOURCE),
                "sha256": SOURCE_SHA,
            },
            "upstream": {
                "repository": "https://example.invalid/nfi.git",
                "ref": "refs/heads/main",
                "configured_commit": HEAD,
                "observed_commit": HEAD,
            },
        },
        "source_closure": {
            "algorithm": "sha256-merkle-source-closure-v1",
            "root_path": ".",
            "merkle_root": "2" * 64,
            "complete": True,
            "file_count": 2,
            "external_imports": [],
            "missing_local_imports": [],
            "files": [
                {
                    "path": "Demo.py",
                    "bytes": len(NEW_SOURCE),
                    "sha256": SOURCE_SHA,
                    "role": "strategy-root",
                    "imports": ["helpers.py"],
                },
                {
                    "path": "helpers.py",
                    "bytes": 4,
                    "sha256": "3" * 64,
                    "role": "local-dependency",
                    "imports": [],
                },
            ],
        },
        "obligation_groups": [
            {
                "kind": "signal",
                "semantic_owner": "nfi-strategy",
                "mapping": "compiled-program",
                "reachability": "reachable",
                "proof": "source-and-compiler-inventory",
                "obligations": [
                    {
                        "obligation_id": "obl-signal-" + "4" * 64,
                        "preimage": {
                            "source": [SOURCE_SHA, "@strategy", [8, 4, 8, 20]],
                            "normalized_semantics": ["route:562", "5" * 64],
                        },
                    }
                ],
            }
        ],
        "summary": {"native_promotion": True, "every_obligation_mapped_once": True},
        "blockers": [],
        "fingerprint": REGISTRY_FINGERPRINT,
    }
    registry["source_closure"]["merkle_root"] = _source_closure_merkle(
        registry["source_closure"]["files"]
    )
    registry["fingerprint"] = _registry_fingerprint(registry)
    registry_fingerprint = registry["fingerprint"]
    fixture_registry = {
        "schema_version": "1.0.0",
        "bundles": [
            {
                "id": f"current-{mode}",
                "trading_mode": mode,
                "upstream_commit": HEAD,
                "source_sha256": SOURCE_SHA,
                "freqtrade_image_digest": ORACLE,
                "fixture_ids": [f"baseline-{mode}", f"candidate-{mode}"],
                "oracle_digest": ORACLE,
                "semantic_profile_sha256": PROFILE,
                "semantic_registry_fingerprint": registry_fingerprint,
            }
            for mode in ("spot", "futures")
        ],
    }
    (tmp_path / "old.py").write_bytes(OLD_SOURCE)
    (tmp_path / "new.py").write_bytes(NEW_SOURCE)
    paths = {}
    for name, document in (
        ("diff", difference),
        ("registry", registry),
        ("fixtures", fixture_registry),
    ):
        path = tmp_path / f"{name}.json"
        write_json(path, document)
        paths[name] = path
    proof_paths: dict[str, Path] = {}
    for mode in ("spot", "futures"):
        report = {
            "schema_version": "1.0.0",
            "trading_mode": mode,
            "source_sha256": SOURCE_SHA,
            "upstream_commit": HEAD,
            "complete": True,
            "verification_state": "quick_verified",
            "freqtrade_digest": ORACLE,
            "semantic_profile_sha256": PROFILE,
            "semantic_registry_fingerprint": registry_fingerprint,
            "plan": {"status": "ready", "missing_target_count": 0},
            "runs": [
                {
                    "fixture_id": f"candidate-{mode}",
                    "target_ids": [target["id"] for target in selected],
                    "upstream_commit": HEAD,
                    "source_sha256": SOURCE_SHA,
                    "freqtrade_digest": ORACLE,
                    "oracle_digest": ORACLE,
                    "semantic_profile_sha256": PROFILE,
                    "semantic_registry_fingerprint": registry_fingerprint,
                    "capture": {
                        "complete": True,
                        "upstream_commit": HEAD,
                        "source_sha256": SOURCE_SHA,
                        "freqtrade_digest": ORACLE,
                        "oracle_digest": ORACLE,
                        "semantic_profile_sha256": PROFILE,
                        "semantic_registry_fingerprint": registry_fingerprint,
                    },
                    "coverage": {
                        "complete": True,
                        "reached_target_ids": [target["id"] for target in selected],
                        "missing_target_ids": [],
                    },
                    "trade_surface_exact": True,
                    "full_state_exact": True,
                }
            ],
            "proof": {
                "complete": True,
                "changed_branch_reached": True,
                "trade_surface_exact": True,
                "full_state_exact": True,
            },
            "qualification": {
                "trading_mode": mode,
                "verification_state": "quick_verified",
                "changed_branch_reached": True,
                "trade_surface_exact": True,
                "full_state_exact": True,
                "blockers": [],
            },
            "blockers": [],
        }
        path = tmp_path / f"targeted-{mode}.json"
        write_json(path, report)
        proof_paths[mode] = path
    return ChangedTargetLedgerSources(
        strategy_diff=paths["diff"],
        semantic_registry=paths["registry"],
        fixture_registry=paths["fixtures"],
        targeted_reports=proof_paths,
        upstream_repository="https://example.invalid/nfi.git",
        upstream_ref="refs/heads/main",
        upstream_head=HEAD,
        baseline_commit=BASELINE,
    )
