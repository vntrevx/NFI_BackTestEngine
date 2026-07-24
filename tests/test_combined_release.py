from __future__ import annotations

import json
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.combined_release import combine_full_x7_release
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.evidence_bundle import write_evidence_bundle

WHEEL_SHA = "a" * 64
NATIVE_SHA = "b" * 64
STRATEGY_SHA = "c" * 64


def _certificate(tmp_path: Path, mode: str) -> Path:
    root = tmp_path / mode
    root.mkdir()
    trading_mode = "spot" if mode == "binance-spot" else "futures"
    report = {
        "schema_version": "2.0.0",
        "created_at": "2026-07-25T00:00:00Z",
        "status": "certified",
        "release_certified": True,
        "claim_scope": {
            "strategy": "NostalgiaForInfinityX7",
            "upstream_commit": "d" * 40,
            "mode_contract": mode,
            "trading_mode": trading_mode,
            "margin_mode": None if trading_mode == "spot" else "isolated",
            "exchange": "binance",
            "settlement_currency": "USDT",
            "required_data_roles": (
                ["candles"]
                if trading_mode == "spot"
                else ["candles", "funding_rate", "mark"]
            ),
            "timerange": "20210101-20260101",
            "pair_count": 80,
            "timeframes": ["5m", "15m", "1h", "4h", "1d"],
            "continuous_timerange": True,
            "evidence": "continuous-oracle-plus-official-full-state-probes",
        },
        "inputs": {
            "release_lock": {"sha256": "e" * 64},
            "mode_contract": mode,
            "reference": {
                "version": "2026.5.1",
                "image_platform_digest": "sha256:" + "f" * 64,
            },
            "strategy_sha256": STRATEGY_SHA,
            "config_sha256": "1" * 64,
            "data_aggregate_sha256": "2" * 64,
            "engine_market_snapshot_sha256": "3" * 64,
            "reference_market_snapshot_sha256": "4" * 64,
        },
        "environment": {
            "hardware": {},
            "execution_profile": {},
            "package_version": "1.0.0",
            "engine_build": {
                "source_fingerprint": "5" * 64,
            },
        },
        "measurement": {
            "native_warmups_excluded": 1,
            "native_initial_repetitions": 3,
            "native_measured_repetitions": 3,
            "native_maximum_repetitions": 5,
            "native_spread_threshold": 0.05,
            "engine_relative_spread": 0.01,
            "official_reference_repetitions": 1,
            "official_reference_role": "single-continuous-exact-parity-oracle",
            "resumed": False,
        },
        "runs": {
            "engine": [{}, {}, {}],
            "official_reference": {},
            "engine_summary": {},
            "official_reference_summary": {},
        },
        "state_probes": [{}, {}, {}],
        "gates": {
            "input_lock": {"met": True},
            "installed_wheel": {
                "met": True,
                "sha256": WHEEL_SHA,
                "native_member_sha256": NATIVE_SHA,
            },
            "native_pipeline": {"met": True},
            "official_parity": {"met": True},
            "determinism": {"met": True},
            "speed": {"met": True},
            "memory": {"met": True},
            "state_probes": {"met": True},
        },
    }
    report_path = root / "full-x7-certification.json"
    write_json(report_path, report)
    bundle = write_evidence_bundle(
        root,
        evidence_id="e" * 64,
        release_certified=True,
        archive_name="full-x7-certification-bundle.zip",
        include_paths=[report_path],
    )
    path = root / "full-x7-result.json"
    write_json(path, {**report, "bundle": bundle})
    return path


def _platform_evidence(tmp_path: Path, mode: str) -> Path:
    root = tmp_path / f"{mode}-platform"
    root.mkdir()
    path = root / "platform-evidence.json"
    evidence = {
        "schema_version": "1.0.0",
        "release_certified": True,
        "mode_contract": mode,
        "package_version": "1.0.0",
        "workload": {
            "mode_contract": mode,
            "strategy_sha256": STRATEGY_SHA,
        },
        "platforms": [
            {
                "system": system,
                "wheel_sha256": WHEEL_SHA if system == "linux" else "9" * 64,
            }
            for system in ("windows", "linux", "darwin")
        ],
    }
    write_json(path, evidence)
    write_evidence_bundle(
        root,
        evidence_id=mode,
        release_certified=True,
        archive_name="platform-evidence-bundle.zip",
        include_paths=[path],
    )
    return path


def test_combined_release_stays_preview_without_both_platform_modes(
    tmp_path: Path,
) -> None:
    result = combine_full_x7_release(
        spot_certificate_path=_certificate(tmp_path, "binance-spot"),
        futures_certificate_path=_certificate(
            tmp_path,
            "binance-usdtm-isolated",
        ),
        platform_evidence_paths=[],
        output_directory=tmp_path / "release",
    )

    assert result["status"] == "preview"
    assert result["release_certified"] is False
    assert result["gates"]["platform_evidence"]["met"] is False


def test_combined_release_certifies_two_modes_and_three_os_evidence(
    tmp_path: Path,
) -> None:
    spot = _certificate(tmp_path, "binance-spot")
    futures = _certificate(tmp_path, "binance-usdtm-isolated")
    result = combine_full_x7_release(
        spot_certificate_path=spot,
        futures_certificate_path=futures,
        platform_evidence_paths=[
            _platform_evidence(tmp_path, "binance-spot"),
            _platform_evidence(tmp_path, "binance-usdtm-isolated"),
        ],
        output_directory=tmp_path / "release",
    )

    assert result["status"] == "certified"
    assert result["release_certified"] is True
    assert result["gates"]["platform_evidence"]["met"] is True
    assert (
        tmp_path
        / "release"
        / result["certificates"]["binance-spot"]["file"]
    ).is_file()
    assert (
        tmp_path
        / "release"
        / "evidence"
        / "binance-usdtm-isolated"
        / "platform-bundle.zip"
    ).is_file()


def test_combined_release_rejects_a_certificate_changed_after_bundling(
    tmp_path: Path,
) -> None:
    spot = _certificate(tmp_path, "binance-spot")
    futures = _certificate(tmp_path, "binance-usdtm-isolated")
    document = json.loads(futures.read_text(encoding="utf-8"))
    document["gates"]["installed_wheel"]["sha256"] = "0" * 64
    write_json(futures, document)

    with pytest.raises(SpecValidationError, match="does not contain its report"):
        combine_full_x7_release(
            spot_certificate_path=spot,
            futures_certificate_path=futures,
            platform_evidence_paths=[],
            output_directory=tmp_path / "release",
        )


def test_combined_release_rejects_different_bundled_candidate_wheels(
    tmp_path: Path,
) -> None:
    spot = _certificate(tmp_path, "binance-spot")
    futures = _certificate(tmp_path, "binance-usdtm-isolated")
    root = futures.parent
    report_path = root / "full-x7-certification.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["gates"]["installed_wheel"]["sha256"] = "0" * 64
    write_json(report_path, report)
    bundle = write_evidence_bundle(
        root,
        evidence_id="e" * 64,
        release_certified=True,
        archive_name="full-x7-certification-bundle.zip",
        include_paths=[report_path],
    )
    write_json(futures, {**report, "bundle": bundle})

    with pytest.raises(SpecValidationError, match="different strategy, wheel"):
        combine_full_x7_release(
            spot_certificate_path=spot,
            futures_certificate_path=futures,
            platform_evidence_paths=[],
            output_directory=tmp_path / "release",
        )
