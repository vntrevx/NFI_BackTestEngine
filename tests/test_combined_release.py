from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.combined_release import (
    PUBLIC_RELEASE_ASSET_COUNT,
    combine_full_x7_release,
    seal_combined_release_candidate,
    verify_combined_release_candidate,
)
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.evidence_bundle import write_evidence_bundle
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.release_gate import RELEASE_CHECKSUMS_NAME

WHEEL_SHA = "a" * 64
NATIVE_SHA = "b" * 64
STRATEGY_SHA = "c" * 64


def _certificate(
    tmp_path: Path,
    mode: str,
    *,
    wheel_sha256: str = WHEEL_SHA,
    portable_package_sha256: str = "8" * 64,
) -> Path:
    root = tmp_path / mode
    root.mkdir()
    trading_mode = "spot" if mode == "binance-spot" else "futures"
    timerange = (
        "20210101-20260101"
        if trading_mode == "spot"
        else "20210726-20260726"
    )
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
                ["candles"] if trading_mode == "spot" else ["candles", "funding_rate", "mark"]
            ),
            "timerange": timerange,
            "pair_count": 80,
            "timeframes": ["5m", "15m", "1h", "4h", "1d"],
            "history_coverage_policy": (
                "strict" if trading_mode == "spot" else "listing-aware"
            ),
            "continuous_timerange": True,
            "evidence": "continuous-oracle-plus-official-full-state-probes",
        },
        "inputs": {
            "release_lock": {
                "sha256": "e" * 64,
                "identity_sha256": "6" * 64,
            },
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
                "sha256": wheel_sha256,
                "native_member_sha256": NATIVE_SHA,
                "portable_package_sha256": portable_package_sha256,
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


def _platform_evidence(
    tmp_path: Path,
    mode: str,
    *,
    wheel_sha256: str = WHEEL_SHA,
    portable_package_sha256: str = "8" * 64,
    platform_wheel_sha256: dict[str, str] | None = None,
    base_strategy_sha256: str = STRATEGY_SHA,
) -> Path:
    root = tmp_path / f"{mode}-platform"
    root.mkdir()
    path = root / "platform-evidence.json"
    evidence = {
        "schema_version": "1.0.0",
        "release_certified": True,
        "mode_contract": mode,
        "package_version": "1.0.0",
        "portable_package_sha256": portable_package_sha256,
        "workload": {
            "mode_contract": mode,
            "strategy_sha256": "7" * 64,
            "base_strategy_sha256": base_strategy_sha256,
        },
        "platforms": [
            {
                "system": system,
                "wheel_sha256": (
                    platform_wheel_sha256[system]
                    if platform_wheel_sha256 is not None
                    else wheel_sha256
                    if system == "linux"
                    else "9" * 64
                ),
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
        result["mode_scopes"]["binance-spot"]["timerange"]
        == "20210101-20260101"
    )
    assert (
        result["mode_scopes"]["binance-usdtm-isolated"]["timerange"]
        == "20210726-20260726"
    )
    assert (tmp_path / "release" / result["certificates"]["binance-spot"]["file"]).is_file()
    assert (
        tmp_path / "release" / "evidence" / "binance-usdtm-isolated" / "platform-bundle.zip"
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


def test_combined_release_rejects_platform_base_strategy_mismatch(
    tmp_path: Path,
) -> None:
    with pytest.raises(SpecValidationError, match="incomplete"):
        combine_full_x7_release(
            spot_certificate_path=_certificate(tmp_path, "binance-spot"),
            futures_certificate_path=_certificate(
                tmp_path,
                "binance-usdtm-isolated",
            ),
            platform_evidence_paths=[
                _platform_evidence(
                    tmp_path,
                    "binance-spot",
                    base_strategy_sha256="0" * 64,
                ),
                _platform_evidence(
                    tmp_path,
                    "binance-usdtm-isolated",
                    base_strategy_sha256="0" * 64,
                ),
            ],
            output_directory=tmp_path / "release",
        )


def _combined_gate_inputs(tmp_path: Path) -> dict[str, Path | str]:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    wheel_names = (
        "nfi_backtest_engine-1.0.0-cp312-cp312-manylinux_2_17_x86_64.whl",
        "nfi_backtest_engine-1.0.0-cp312-cp312-manylinux_2_17_aarch64.whl",
        "nfi_backtest_engine-1.0.0-cp312-cp312-win_amd64.whl",
        "nfi_backtest_engine-1.0.0-cp312-cp312-macosx_11_0_arm64.whl",
    )
    for index, name in enumerate(wheel_names):
        (candidate / name).write_bytes(f"wheel-{index}".encode())
    (candidate / "nfi_backtest_engine-1.0.0.tar.gz").write_bytes(b"sdist")
    linux_wheel_sha256 = sha256_file(candidate / wheel_names[0])
    platform_wheel_sha256 = {
        "linux": linux_wheel_sha256,
        "windows": sha256_file(candidate / wheel_names[2]),
        "darwin": sha256_file(candidate / wheel_names[3]),
    }

    platform_paths: dict[str, Path] = {}
    for slug, mode in (
        ("spot", "binance-spot"),
        ("futures", "binance-usdtm-isolated"),
    ):
        source = _platform_evidence(
            tmp_path,
            mode,
            wheel_sha256=linux_wheel_sha256,
            platform_wheel_sha256=platform_wheel_sha256,
        ).parent
        destination = candidate / "platform" / slug
        shutil.copytree(source, destination)
        platform_paths[mode] = destination / "platform-evidence.json"

    candidate_assets = sorted(
        (path for path in candidate.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(candidate).as_posix(),
    )
    (candidate / "SHA256SUMS.txt").write_text(
        "".join(
            f"{sha256_file(path)}  {path.relative_to(candidate).as_posix()}\n"
            for path in candidate_assets
        ),
        encoding="utf-8",
    )

    spot = _certificate(
        tmp_path,
        "binance-spot",
        wheel_sha256=linux_wheel_sha256,
    )
    futures = _certificate(
        tmp_path,
        "binance-usdtm-isolated",
        wheel_sha256=linux_wheel_sha256,
    )
    combined_directory = tmp_path / "combined"
    combine_full_x7_release(
        spot_certificate_path=spot,
        futures_certificate_path=futures,
        platform_evidence_paths=list(platform_paths.values()),
        output_directory=combined_directory,
    )
    return {
        "candidate_directory": candidate,
        "combined_release_result_path": (combined_directory / "full-x7-release-result.json"),
        "candidate_commit": "1" * 40,
        "output_directory": tmp_path / "public",
    }


def _reseal_public_checksums(root: Path) -> None:
    assets = sorted(
        (path for path in root.iterdir() if path.is_file() and path.name != RELEASE_CHECKSUMS_NAME),
        key=lambda path: path.name,
    )
    (root / RELEASE_CHECKSUMS_NAME).write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in assets),
        encoding="utf-8",
    )


def test_combined_release_gate_seals_exact_public_asset_set(tmp_path: Path) -> None:
    inputs = _combined_gate_inputs(tmp_path)

    result = seal_combined_release_candidate(**inputs)
    release = Path(inputs["output_directory"])
    verified = verify_combined_release_candidate(
        release,
        expected_commit=str(inputs["candidate_commit"]),
    )

    assert verified == result
    assert len(list(release.iterdir())) == PUBLIC_RELEASE_ASSET_COUNT
    assert len(result["distributions"]) == 5
    assert result["candidate_manifest"]["candidate_file"] == "SHA256SUMS.txt"
    assert result["candidate_manifest"]["sha256"] != sha256_file(
        release / "SHA256SUMS.txt"
    )
    assert set(result["platform_evidence"]) == {
        "binance-spot",
        "binance-usdtm-isolated",
    }
    assert all(
        record["candidate_file"].startswith("platform/")
        for record in result["platform_evidence"].values()
    )


def test_combined_release_gate_rejects_preview_result(tmp_path: Path) -> None:
    inputs = _combined_gate_inputs(tmp_path)
    result_path = Path(inputs["combined_release_result_path"])
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["status"] = "preview"
    result["release_certified"] = False
    write_json(result_path, result)

    with pytest.raises(SpecValidationError, match="preview"):
        seal_combined_release_candidate(**inputs)


def test_combined_release_gate_rejects_incomplete_distribution_set(
    tmp_path: Path,
) -> None:
    inputs = _combined_gate_inputs(tmp_path)
    candidate = Path(inputs["candidate_directory"])
    next(candidate.glob("*win_amd64.whl")).unlink()

    with pytest.raises(SpecValidationError, match="SHA256SUMS"):
        seal_combined_release_candidate(**inputs)


def test_combined_release_verifier_rejects_resealed_report_tamper(
    tmp_path: Path,
) -> None:
    inputs = _combined_gate_inputs(tmp_path)
    seal_combined_release_candidate(**inputs)
    release = Path(inputs["output_directory"])
    report_path = release / "full-x7-release.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["shared_candidate"]["strategy_sha256"] = "0" * 64
    write_json(report_path, report)
    _reseal_public_checksums(release)

    with pytest.raises(SpecValidationError, match="report differs"):
        verify_combined_release_candidate(release)


def test_combined_release_verifier_rejects_resealed_distribution_rename(
    tmp_path: Path,
) -> None:
    inputs = _combined_gate_inputs(tmp_path)
    seal_combined_release_candidate(**inputs)
    release = Path(inputs["output_directory"])
    wheel = next(release.glob("*win_amd64.whl"))
    renamed = wheel.with_name("renamed-distribution.bin")
    wheel.rename(renamed)

    distribution_manifest = release / "SHA256SUMS.txt"
    distribution_manifest.write_text(
        distribution_manifest.read_text(encoding="utf-8").replace(
            wheel.name,
            renamed.name,
        ),
        encoding="utf-8",
    )
    gate_path = release / "release-gate.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    for record in gate["distributions"]:
        if record["file"] == wheel.name:
            record["file"] = renamed.name
            break
    write_json(gate_path, gate)
    _reseal_public_checksums(release)

    with pytest.raises(SpecValidationError, match="exactly four"):
        verify_combined_release_candidate(release)
