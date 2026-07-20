from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError
from nfi_backtest_engine.full_x7_certification import (
    _determinism,
    _engine_complete,
    _validate_full_x7_timeframes,
    _validate_probe_matrix,
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

    probes = _validate_probe_matrix(manifests)

    assert probes
    assert len(probes) == len(manifests)


def test_full_x7_release_requires_all_five_timeframes_in_stable_order() -> None:
    _validate_full_x7_timeframes(["5m", "15m", "1h", "4h", "1d"])

    with pytest.raises(SpecValidationError, match="timeframes differ"):
        _validate_full_x7_timeframes(["5m", "1h", "1d"])
