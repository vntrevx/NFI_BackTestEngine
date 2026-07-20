from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from nfi_backtest_engine import full_x7_certification
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError
from nfi_backtest_engine.full_x7_certification import (
    _determinism,
    _engine_complete,
    _measure_reference,
    _validate_full_x7_timeframes,
    _validate_probe_matrix,
    _validate_release_data_seal,
    verify_installed_wheel,
)

ROOT = Path(__file__).parents[1]
CAPTURED = ROOT / "benchmarks" / "fixtures" / "captured"


def test_candidate_wheel_must_contain_the_imported_native_extension(
    tmp_path: Path,
) -> None:
    native = b"native-extension-bytes"
    wheel = tmp_path / "nfi_backtest_engine-1.0.0-test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("nfi_backtest_engine/_rust.test.pyd", native)
    native_sha = hashlib.sha256(native).hexdigest()

    record = verify_installed_wheel(
        wheel,
        {
            "kind": "pyo3-extension",
            "binary_sha256": native_sha,
        },
    )

    assert record["installed_extension_equal"] is True
    assert record["native_member_sha256"] == native_sha

    with pytest.raises(BenchmarkError, match="does not match"):
        verify_installed_wheel(
            wheel,
            {
                "kind": "pyo3-extension",
                "binary_sha256": "0" * 64,
            },
        )


def test_full_x7_determinism_includes_warmup_native_and_official_hashes() -> None:
    engine = [{"result_sha256": "a" * 64}, {"result_sha256": "a" * 64}]
    reference = [{"result_sha256": "a" * 64}, {"result_sha256": "a" * 64}]
    assert _determinism("a" * 64, engine, reference)["met"] is True

    reference[1]["result_sha256"] = "b" * 64
    assert _determinism("a" * 64, engine, reference)["met"] is False


def test_cold_strict_engine_gate_rejects_checkpoint_or_coverage_shortfall() -> None:
    lock = {"data": {"aggregate_sha256": "d" * 64}}
    measurement = {
        "exit_code": 0,
        "result_sha256": "a" * 64,
        "report": {
            "complete": True,
            "pipeline_evidence": {"cold": True},
            "data": {
                "history_coverage_policy": "strict",
                "coverage_shortfall_count": 0,
                "aggregate_sha256": "d" * 64,
            },
            "capability": {"blockers": []},
        },
    }
    assert _engine_complete(measurement, lock) is True

    measurement["report"]["pipeline_evidence"]["cold"] = False
    assert _engine_complete(measurement, lock) is False


def test_full_x7_probe_matrix_cannot_be_empty() -> None:
    with pytest.raises(SpecValidationError, match="probe matrix is incomplete"):
        _validate_probe_matrix([])


def test_real_full_x7_probe_matrix_covers_every_required_branch() -> None:
    manifests = sorted(
        path / "manifest.json"
        for path in CAPTURED.iterdir()
        if path.name.startswith("x7-") and (path / "manifest.json").is_file()
    )
    upstream_commits = {
        read_json(path)["strategy_provenance"]["upstream_commit"]
        for path in manifests
    }
    assert len(upstream_commits) == 1
    upstream_commit = upstream_commits.pop()

    probes = _validate_probe_matrix(
        manifests,
        expected_upstream_commit=upstream_commit,
    )

    assert probes
    assert len(probes) == len(manifests)
    with pytest.raises(SpecValidationError, match="upstream commit differs"):
        _validate_probe_matrix(
            manifests[:1],
            expected_upstream_commit="0" * 40,
        )


def test_full_x7_release_requires_all_five_timeframes_in_stable_order() -> None:
    _validate_full_x7_timeframes(["5m", "15m", "1h", "4h", "1d"])

    with pytest.raises(SpecValidationError, match="timeframes differ"):
        _validate_full_x7_timeframes(["5m", "1h", "1d"])


def test_full_x7_data_seal_is_bound_to_lock_and_selected_directory(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    pairs = ["BTC/USDT"]
    timeframes = ["5m", "15m", "1h", "4h", "1d"]
    lock = {
        "pairlist": {"pairs": pairs},
        "scope": {
            "timerange": "20210101-20260101",
            "timeframes": timeframes,
        },
        "data": {
            "aggregate_sha256": "a" * 64,
            "file_count": 1,
            "coverage_shortfall_count": 0,
            "startup_shortfall_count": 1,
            "startup_coverage_policy": "record",
        },
    }
    seal = {
        "data_root": str(data),
        "aggregate_sha256": "a" * 64,
        "files": [{}],
        "coverage_shortfalls": [],
        "startup_shortfalls": [{}],
        "request": {
            "pairs": pairs,
            "timerange": "20210101-20260101",
            "timeframes": timeframes,
            "history_coverage_policy": "strict",
            "startup_coverage_policy": "record",
        },
    }

    _validate_release_data_seal(lock, seal, data_directory=data)

    with pytest.raises(SpecValidationError, match="selected data directory"):
        _validate_release_data_seal(
            lock,
            seal,
            data_directory=tmp_path / "other-data",
        )


@pytest.mark.parametrize("reuse_snapshot", [False, True])
def test_full_x7_warmup_can_capture_or_reuse_reference_markets(
    monkeypatch,
    tmp_path: Path,
    reuse_snapshot: bool,
) -> None:
    captured: dict[str, object] = {}

    def fake_measure(arguments, stdout_path, stderr_path, *, timeout_seconds):
        captured.update(
            arguments=arguments,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=timeout_seconds,
        )
        return {"exit_code": 0}

    monkeypatch.setattr(
        full_x7_certification,
        "measure_cli_process",
        fake_measure,
    )
    monkeypatch.setattr(
        full_x7_certification,
        "_reference_surface_sha",
        lambda _measurement: None,
    )
    snapshot = tmp_path / "reference-markets.json" if reuse_snapshot else None
    _measure_reference(
        tmp_path / "engine",
        snapshot,
        tmp_path / "reference",
        timeout_seconds=123,
        swap_cap_bytes=None,
    )

    arguments = captured["arguments"]
    assert isinstance(arguments, list)
    assert ("--markets" in arguments) is reuse_snapshot
    assert ("--no-market-capture" in arguments) is reuse_snapshot
