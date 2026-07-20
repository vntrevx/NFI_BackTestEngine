from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.branch_coverage import (
    derive_fixture_observed,
    validate_fixture_coverage,
)
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import sha256_file, validate_fixture
from nfi_backtest_engine.fixture_capture import finalize_fixture_v3, stage_fixture_v3
from nfi_backtest_engine.specs import validate_fixture_manifest
from nfi_backtest_engine.state_trace import StateTraceWriter

ROOT = Path(__file__).parents[1]
SOURCE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "normal-routing-spot-2025-01-01_04"
)
TAG_121_FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-tag121-spot-v17.4.421-2023-01-01_02"
)


def _required_coverage() -> dict:
    return {
        "callbacks": ["adjust_trade_position", "custom_exit"],
        "entry_tags": ["contract_route"],
        "compound_tags": [],
        "protection_methods": [],
        "exit_reasons": ["contract_timed_exit"],
        "sides": ["long"],
        "minimum_lock_count": 0,
        "minimum_distinct_leverages": 1,
        "require_rejected_locked_entry": False,
    }


def test_v3_fixture_binds_provenance_and_reached_branches(tmp_path: Path) -> None:
    strategy = SOURCE / "inputs" / "strategy.py"
    destination = tmp_path / "probe"
    stage_fixture_v3(
        destination,
        fixture_id="x7-callback-route-probe",
        description="Official callback route probe",
        probe_kind="callback-route",
        strategy_provenance={
            "upstream_repository": "https://github.com/iterativv/NostalgiaForInfinity",
            "upstream_commit": "a" * 40,
            "base_source_sha256": sha256_file(strategy),
            "effective_source_sha256": sha256_file(strategy),
            "transformations": [
                {
                    "kind": "observer-only",
                    "description": "Read-only state tracer",
                }
            ],
        },
        required_coverage=_required_coverage(),
        strategy=strategy,
        config=SOURCE / "inputs" / "config.json",
        inputs=[
            ("candles", SOURCE / "inputs" / "candles" / "BTC_USDT-5m.feather"),
            (
                "market_metadata",
                SOURCE / "inputs" / "market_metadata" / "markets.json",
            ),
        ],
    )
    source_manifest = read_json(SOURCE / "manifest.json")
    manifest = finalize_fixture_v3(
        destination,
        freqtrade_result=SOURCE / "artifacts" / "freqtrade-result.zip",
        trade_surface=SOURCE / "artifacts" / "trade-surface.json",
        state_trace=SOURCE / "artifacts" / "state-trace.nfitrace",
        freqtrade=source_manifest["freqtrade"],
    )

    assert manifest["schema_version"] == "3.0.0"
    assert manifest["probe_kind"] == "callback-route"
    assert validate_fixture(destination / "manifest.json")["fixture_id"] == (
        "x7-callback-route-probe"
    )

    manifest["required_coverage"]["entry_tags"] = ["121"]
    write_json(destination / "manifest.json", manifest)
    with pytest.raises(SpecValidationError, match="entry_tags:121"):
        validate_fixture(destination / "manifest.json")


def test_real_tag_121_fixture_is_fully_sealed_and_branch_reaching() -> None:
    manifest_path = TAG_121_FIXTURE / "manifest.json"
    manifest = validate_fixture(manifest_path)
    coverage = validate_fixture_coverage(manifest_path, manifest)

    assert manifest["schema_version"] == "3.0.0"
    assert manifest["probe_kind"] == "tag-121"
    assert manifest["strategy_provenance"]["upstream_commit"] == (
        "5e168431991e05a889514eb1e16fdbebc6a09811"
    )
    assert coverage["met"] is True
    assert coverage["observed"]["entry_tags"] == ["121"]
    assert coverage["observed"]["callbacks"] == [
        "adjust_trade_position",
        "confirm_trade_entry",
        "custom_exit",
    ]


def test_v3_provenance_must_match_effective_strategy_hash(tmp_path: Path) -> None:
    manifest = read_json(SOURCE / "manifest.json")
    manifest.update(
        {
            "schema_version": "3.0.0",
            "fixture_kind": "x7-branch-probe",
            "probe_kind": "callback-route",
            "strategy_provenance": {
                "upstream_repository": (
                    "https://github.com/iterativv/NostalgiaForInfinity"
                ),
                "upstream_commit": "a" * 40,
                "base_source_sha256": "b" * 64,
                "effective_source_sha256": "b" * 64,
                "transformations": [],
            },
            "required_coverage": _required_coverage(),
        }
    )
    manifest["artifacts"]["coverage_report"] = {
        "path": "artifacts/coverage-report.json",
        "sha256": "c" * 64,
        "bytes": 1,
    }
    # The schema check happens before artifact IO, so this can use the original
    # immutable fixture paths without copying its large trace.
    with pytest.raises(SpecValidationError, match="effective_source_sha256"):
        validate_fixture_manifest(manifest)


def test_lock_coverage_is_derived_from_official_state_and_rejection_event(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "locks.nfitrace"
    state = {
        "locks": [
            {
                "pair": "BTC/USDT",
                "side": "long",
                "lock_timestamp": 1_000,
                "lock_end_timestamp": 61_000,
                "reason": "Cooldown period for 1 candle.",
            }
        ]
    }
    with StateTraceWriter(
        trace,
        source="freqtrade-reference",
        run_id="lock-probe",
        input_sha256="a" * 64,
        strategy_sha256="b" * 64,
        profile_sha256="c" * 64,
        trading_mode="spot",
        include_state=True,
    ) as writer:
        writer.append(
            timestamp_ms=1_000,
            phase="candle.after",
            pair="BTC/USDT",
            state=state,
        )
        writer.append(
            timestamp_ms=2_000,
            phase="entry.lock_rejected",
            pair="BTC/USDT",
            callback="PairLocks.is_pair_locked",
            state=state,
        )

    observed = derive_fixture_observed(
        {"trades": [], "locks": []},
        trace,
        configured_protection_methods=["CooldownPeriod"],
    )

    assert observed["lock_count"] == 1
    assert observed["protection_methods"] == ["CooldownPeriod"]
    assert observed["rejected_locked_entry"] is True
