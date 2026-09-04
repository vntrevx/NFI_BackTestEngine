from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from nfi_backtest_engine import platform_benchmark
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import validate_fixture as validate_fixture_real
from nfi_backtest_engine.platform_benchmark import (
    EXACT_FIXTURE_LANE,
    RAW_INPUT_LANE,
    REQUIRED_PLATFORM_SLUGS,
    _fixture_result_sha256,
    _portable_timerange,
    seal_platform_evidence,
)
from nfi_backtest_engine.release_provenance import workload_identity
from provenance_support import TEST_POLICY, sign_report


def _report(
    system: str,
    machine: str,
    *,
    result: str = "a" * 64,
    mode_contract: str = "binance-usdtm-isolated",
    lane: str = RAW_INPUT_LANE,
    slug: str | None = None,
    wsl: bool | None = None,
) -> dict:
    workload = {
        "mode_contract": mode_contract,
        "identity_sha256": "d" * 64,
    }
    package = {
        "version": "1.0.0",
        "wheel_sha256": {"linux": "b", "darwin": "c"}[system] * 64,
        "native_extension_sha256": {"linux": "2", "darwin": "3"}[system] * 64,
        "installed_extension_sha256": {"linux": "2", "darwin": "3"}[system] * 64,
        "installed_extension_equal": True,
        "portable_package_sha256": "c" * 64,
    }
    if lane == RAW_INPUT_LANE:
        workload["pairs"] = [f"PAIR-{index}" for index in range(20)]
    else:
        workload.update(
            {
                "lane": EXACT_FIXTURE_LANE,
                "fixture_id": "x7-futures-short",
                "manifest_sha256": "e" * 64,
                "strategy_sha256": "f" * 64,
                "base_strategy_sha256": "b" * 64,
                "verification_level": "full",
            }
        )
    return {
        "schema_version": "1.2.0",
        "complete": True,
        "lane": lane,
        "platform": {
            "slug": slug,
            "system": system,
            "machine": machine,
            "wsl": system == "linux" if wsl is None else wsl,
        },
        "package": package,
        "workload": workload,
        "measurement": {
            "result_sha256": [result],
            "wall_time_seconds": {"median": 10.0},
            "peak_rss_bytes": {"maximum": 1000},
            "measured_repetitions": 3,
        },
    }


@pytest.mark.parametrize("mutation", ["final", "parent", "in-place", "hardlink"])
def test_platform_fixture_consumer_uses_retained_config_after_validation_swap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str
) -> None:
    source = (
        Path(__file__).parents[1]
        / "benchmarks/fixtures/captured/stops-only-spot-2025-01-01_04"
    )
    root = tmp_path / "fixture"
    shutil.copytree(source, root)
    manifest_path = root / "manifest.json"
    manifest = validate_fixture_real(manifest_path)
    config = root / next(
        item["path"] for item in manifest["inputs"] if item["role"] == "config"
    )
    original = config.read_bytes()
    outside = tmp_path / "outside.json"
    outside.write_bytes(original)
    outside_parent = tmp_path / "outside-inputs"
    shutil.copytree(config.parent, outside_parent)

    def validate_then_swap(*args, **kwargs):
        validated = validate_fixture_real(*args, **kwargs)
        if mutation == "final":
            config.unlink()
            config.symlink_to(outside)
        elif mutation == "parent":
            config.parent.rename(root / "inputs-retained")
            (root / "inputs").symlink_to(outside_parent, target_is_directory=True)
        else:
            target = config
            if mutation == "hardlink":
                target = tmp_path / "config-alias.json"
                target.hardlink_to(config)
            changed = bytearray(original)
            changed[-2] = ord(" ") if changed[-2] != ord(" ") else ord("\t")
            target.write_bytes(changed)
        return validated

    class ConsumerReached(RuntimeError):
        pass

    def stop_at_config(path: str | Path, **_kwargs):
        consumed = Path(path)
        assert consumed.resolve() != outside
        assert consumed.read_bytes() == original
        raise ConsumerReached

    monkeypatch.setattr(platform_benchmark, "validate_fixture", validate_then_swap)
    monkeypatch.setattr(platform_benchmark, "read_json", stop_at_config)

    with pytest.raises(ConsumerReached):
        platform_benchmark.run_platform_fixture_benchmark(
            manifest_path,
            tmp_path / "output",
            wheel_path=tmp_path / "candidate.whl",
            repetitions=3,
            timeout_seconds=30,
        )


def test_portable_workload_uses_last_complete_year_of_release_timerange() -> None:
    assert _portable_timerange("20210101-20260101") == "20250101-20260101"


def test_platform_seal_requires_supported_systems_and_one_result(tmp_path: Path) -> None:
    paths = []
    for system, machine in (("linux", "x86_64"), ("darwin", "arm64")):
        path = tmp_path / f"{system}.json"
        write_json(path, _report(system, machine))
        sign_report(path, run_id=1)
        paths.append(path)

    evidence = seal_platform_evidence(
        paths, tmp_path / "sealed", provenance_policy=TEST_POLICY
    )

    assert evidence["release_certified"] is True
    assert evidence["mode_contract"] == "binance-usdtm-isolated"
    assert evidence["workload"]["identity_sha256"] == workload_identity(
        evidence["workload"]
    )
    assert evidence["result_sha256"] == "a" * 64
    assert [item["system"] for item in evidence["platforms"]] == ["darwin", "linux"]


def test_platform_v2_seal_requires_four_exact_target_slugs(tmp_path: Path) -> None:
    targets = (
        ("linux-x86_64", "linux", "x86_64", False),
        ("linux-aarch64", "linux", "aarch64", False),
        ("macos-arm64", "darwin", "arm64", False),
        ("windows-wsl2-x86_64", "linux", "x86_64", True),
    )
    paths = []
    for slug, system, machine, wsl in targets:
        path = tmp_path / f"{slug}.json"
        write_json(path, _report(system, machine, slug=slug, wsl=wsl))
        sign_report(path, run_id=1)
        paths.append(path)

    evidence = seal_platform_evidence(
        paths,
        tmp_path / "sealed-v2",
        provenance_policy=TEST_POLICY,
        required_platform_slugs=REQUIRED_PLATFORM_SLUGS,
    )

    assert [item["slug"] for item in evidence["platforms"]] == sorted(
        REQUIRED_PLATFORM_SLUGS
    )


def test_platform_seal_rejects_native_windows_report(tmp_path: Path) -> None:
    paths = []
    for system, machine in (("linux", "x86_64"), ("darwin", "arm64")):
        path = tmp_path / f"{system}.json"
        write_json(path, _report(system, machine))
        sign_report(path, run_id=1)
        paths.append(path)
    windows_report = _report("linux", "x86_64")
    windows_report["platform"] = {"system": "windows", "machine": "amd64", "wsl": False}
    windows_path = tmp_path / "windows.json"
    write_json(windows_path, windows_report)
    sign_report(windows_path, run_id=1)
    paths.append(windows_path)

    with pytest.raises(SpecValidationError, match="unsupported platform evidence system"):
        seal_platform_evidence(
            paths, tmp_path / "sealed", provenance_policy=TEST_POLICY
        )


def test_platform_seal_rejects_cross_system_result_drift(tmp_path: Path) -> None:
    paths = []
    for system, machine, result in (
        ("linux", "x86_64", "a" * 64),
        ("darwin", "arm64", "b" * 64),
    ):
        path = tmp_path / f"{system}.json"
        write_json(path, _report(system, machine, result=result))
        sign_report(path, run_id=1)
        paths.append(path)

    with pytest.raises(SpecValidationError, match="differs"):
        seal_platform_evidence(
            paths, tmp_path / "sealed", provenance_policy=TEST_POLICY
        )


def test_platform_seal_accepts_exact_fixture_lane(tmp_path: Path) -> None:
    paths = []
    for system, machine in (("linux", "x86_64"), ("darwin", "arm64")):
        path = tmp_path / f"{system}.json"
        write_json(path, _report(system, machine, lane=EXACT_FIXTURE_LANE))
        sign_report(path, run_id=1)
        paths.append(path)

    evidence = seal_platform_evidence(
        paths, tmp_path / "sealed", provenance_policy=TEST_POLICY
    )

    assert evidence["release_certified"] is True
    assert evidence["lane"] == EXACT_FIXTURE_LANE
    assert evidence["workload"]["base_strategy_sha256"] == "b" * 64
    assert {
        item["system"]: item["native_extension_sha256"]
        for item in evidence["platforms"]
    } == {"linux": "2" * 64, "darwin": "3" * 64}


def test_fixture_result_identity_ignores_host_paths() -> None:
    def report(root: str) -> dict:
        return {
            "fixture_id": "x7-futures-short",
            "artifacts": {
                "trade_surface": {
                    "path": f"{root}/trade-surface.json",
                    "sha256": "a" * 64,
                }
            },
            "parity": {
                "state_trace": {
                    "actual": {
                        "path": f"{root}/state.trace",
                        "input_sha256": "b" * 64,
                        "profile_sha256": "c" * 64,
                        "stream_hash": "d" * 64,
                    }
                }
            },
        }

    trade_surface = {
        "schema_version": "1.0.0",
        "strategy": "NostalgiaForInfinityX7",
        "trades": [],
    }
    assert _fixture_result_sha256(
        report("C:/runner"),
        trade_surface,
    ) == _fixture_result_sha256(
        report("/home/runner"),
        trade_surface,
    )
