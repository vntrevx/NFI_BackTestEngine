from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from nfi_backtest_engine import full_x7_certification, full_x7_resume
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.config_loader import config_sha256
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError
from nfi_backtest_engine.full_x7_certification import (
    _determinism,
    _engine_complete,
    _measure_engine,
    _measure_reference,
    _validate_full_x7_timeframes,
    _validate_probe_matrix,
    _validate_release_data_seal,
    verify_installed_wheel,
)
from nfi_backtest_engine.release_contract import (
    FUTURES_RELEASE_CONTRACT,
    SPOT_RELEASE_CONTRACT,
    release_contract_for_config,
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


def test_full_x7_native_measurement_forwards_resume_to_the_research_state_machine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[str] = []

    def fake_measure(arguments, *_args, **_kwargs):
        captured.extend(arguments)
        return {
            "wall_time_seconds": 1.0,
            "peak_rss_bytes": 1,
            "exit_code": 2,
            "timed_out": False,
        }

    monkeypatch.setattr(full_x7_certification, "measure_cli_process", fake_measure)
    inputs = {
        "strategy_path": tmp_path / "strategy.py",
        "config_path": tmp_path / "config.json",
        "data_directory": tmp_path / "data",
        "engine_market_snapshot": tmp_path / "markets.json",
        "lock": {
            "strategy": {"class_name": "NostalgiaForInfinityX7"},
            "scope": {"timerange": "20210101-20260101"},
            "pairlist": {"pairs": ["BTC/USDT"]},
        },
    }

    _measure_engine(
        inputs,
        tmp_path / "run",
        profile_path=tmp_path / "profile.json",
        timeout_seconds=60,
        resume=True,
    )

    assert captured[-1] == "--resume"


def test_full_x7_loader_delegates_only_incomplete_reports_to_resume(
    tmp_path: Path,
) -> None:
    output = tmp_path / "partial"
    output.mkdir()
    write_json(output / "run.json", {"complete": False})
    write_json(
        output / "certification-measurement.json",
        {
            "wall_time_seconds": 1.0,
            "peak_rss_bytes": 1,
            "exit_code": 0,
            "timed_out": False,
        },
    )

    assert (
        full_x7_resume.load_engine_measurement(
            output,
            validator=lambda _measurement: False,
            allow_report_fallback=False,
            allow_incomplete=True,
        )
        is None
    )
    with pytest.raises(BenchmarkError, match="stage is incomplete"):
        full_x7_resume.load_engine_measurement(
            output,
            validator=lambda _measurement: False,
            allow_report_fallback=False,
        )


def test_full_x7_repeats_native_candidate_but_runs_long_oracle_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls = {"engine": 0, "reference": 0}
    surface_sha = "a" * 64
    lock = {
        "identity_sha256": "b" * 64,
        "data": {"aggregate_sha256": "c" * 64},
        "scope": {
            "timerange": "20210101-20260101",
            "pair_count": 80,
            "timeframes": ["5m", "15m", "1h", "4h", "1d"],
        },
        "strategy": {"upstream_commit": "d" * 40},
    }
    inputs = {
        "lock": lock,
        "contract": SPOT_RELEASE_CONTRACT,
        "reference_market_snapshot": tmp_path / "markets.json",
        "public": {
            "release_lock": {
                "sha256": "e" * 64,
                "identity_sha256": lock["identity_sha256"],
            },
            "mode_contract": SPOT_RELEASE_CONTRACT.contract_id,
            "reference": {
                "version": "2026.5.1",
                "image_platform_digest": "sha256:" + "9" * 64,
            },
            "strategy_sha256": "f" * 64,
            "config_sha256": "1" * 64,
            "data_aggregate_sha256": lock["data"]["aggregate_sha256"],
            "engine_market_snapshot_sha256": "2" * 64,
            "reference_market_snapshot_sha256": "3" * 64,
        },
    }

    def measurement(output: Path) -> dict[str, object]:
        return {
            "wall_time_seconds": 10.0,
            "peak_rss_bytes": 100,
            "exit_code": 0,
            "timed_out": False,
            "stdout": None,
            "stderr": None,
            "output_directory": output,
            "result_sha256": surface_sha,
            "report": {
                "result": {"trade_surface": {"sha256": surface_sha}},
            },
        }

    def fake_engine(_inputs, output, **_kwargs):
        calls["engine"] += 1
        return measurement(output)

    def fake_reference(_baseline, _markets, output, **_kwargs):
        calls["reference"] += 1
        return measurement(output)

    monkeypatch.setattr(
        full_x7_certification,
        "validate_full_x7_inputs",
        lambda **_kwargs: inputs,
    )
    monkeypatch.setattr(
        full_x7_certification,
        "build_engine",
        lambda: {"kind": "pyo3-extension"},
    )
    monkeypatch.setattr(
        full_x7_certification,
        "verify_installed_wheel",
        lambda *_args, **_kwargs: {"installed_extension_equal": True},
    )
    monkeypatch.setattr(
        full_x7_certification,
        "load_execution_profile",
        lambda _path: {"hardware_fingerprint": "hardware"},
    )
    monkeypatch.setattr(
        full_x7_certification,
        "_validate_probe_matrix",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(full_x7_certification, "_measure_engine", fake_engine)
    monkeypatch.setattr(full_x7_certification, "_measure_reference", fake_reference)
    monkeypatch.setattr(
        full_x7_certification,
        "_require_complete_baseline",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        full_x7_certification,
        "_engine_complete",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        full_x7_certification,
        "_reference_complete",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        full_x7_certification,
        "_run_probes",
        lambda *_args, **_kwargs: [
            {
                "fixture_id": f"probe-{index}",
                "probe_kind": kind,
                "manifest_sha256": "5" * 64,
                "complete": True,
                "trade_surface_equal": True,
                "full_state_equal": True,
                "coverage_met": True,
                "performance_report": {
                    "path": f"state-probes/probe-{index}.json",
                    "bytes": 0,
                    "sha256": "6" * 64,
                },
            }
            for index, kind in enumerate(
                (
                    "tag-121",
                    "protections-locks",
                    "liquidation",
                    "compound-tags",
                    "variable-leverage",
                ),
                start=1,
            )
        ],
    )
    monkeypatch.setattr(
        full_x7_certification,
        "current_resource_limits",
        lambda _profile: {"working_memory_bytes": 1_000},
    )
    monkeypatch.setattr(
        full_x7_certification,
        "inspect_hardware",
        lambda: {},
    )
    monkeypatch.setattr(
        full_x7_certification,
        "public_hardware_record",
        lambda _hardware: {},
    )
    monkeypatch.setattr(
        full_x7_certification,
        "public_engine_build_record",
        lambda _build: {},
    )
    monkeypatch.setattr(
        full_x7_certification,
        "write_evidence_bundle",
        lambda *_args, **_kwargs: {"archive": {"sha256": "4" * 64}},
    )

    report = full_x7_certification.run_full_x7_certification(
        tmp_path / "lock.json",
        tmp_path / "certificate",
        strategy_path=tmp_path / "strategy.py",
        class_name="NostalgiaForInfinityX7",
        config_path=tmp_path / "config.json",
        data_directory=tmp_path / "data",
        engine_market_snapshot=tmp_path / "engine-markets.json",
        reference_market_snapshot=tmp_path / "markets.json",
        wheel_path=tmp_path / "candidate.whl",
        execution_profile_path=tmp_path / "profile.json",
        state_probe_manifests=[],
        repetitions=3,
        timeout_seconds=60,
    )

    assert calls == {"engine": 4, "reference": 1}
    assert report["measurement"]["native_measured_repetitions"] == 3
    assert report["measurement"]["official_reference_repetitions"] == 1
    assert "reference" not in report["runs"]
    assert report["runs"]["official_reference"]["result_sha256"] == surface_sha


def test_imported_official_oracle_accepts_engine_identity_change_when_seals_match(
    tmp_path: Path,
) -> None:
    from nfi_backtest_engine.reference_runtime import (
        REFERENCE_PLATFORM,
        REFERENCE_PLATFORM_DIGEST,
    )

    config = {
        "exchange": {"pair_whitelist": ["BTC/USDT"]},
        "pairlists": [{"method": "StaticPairList", "allow_inactive": True}],
    }
    config_path = tmp_path / "config.json"
    write_json(config_path, config)
    data_sha = "9" * 64
    data_seal = tmp_path / "data-seal.json"
    write_json(
        data_seal,
        {"schema_version": "1.3.0", "aggregate_sha256": data_sha},
    )
    result_zip = tmp_path / "backtest-result.zip"
    with zipfile.ZipFile(result_zip, "w") as archive:
        archive.writestr(
            "backtest-result.meta.json",
            json.dumps(
                {
                    "NostalgiaForInfinityX7": {
                        "timeframe": "5m",
                        "backtest_start_ts": 1609459200,
                        "backtest_end_ts": 1767225600,
                    }
                }
            ),
        )

    def record(path: Path) -> dict[str, object]:
        return {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    surface_sha = "a" * 64
    strategy_sha = "c" * 64
    market_sha = "d" * 64
    baseline = {
        "result_sha256": surface_sha,
        "report": {"run_id": "b" * 64},
    }
    report = {
        "run_id": "e" * 64,
        "complete": True,
        "exact_parity": True,
        "reference": {
            "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
            "platform": REFERENCE_PLATFORM,
        },
        "inputs": {
            "strategy": {"sha256": strategy_sha},
            "config": record(config_path),
            "data_seal": record(data_seal),
            "engine_trade_surface": {"sha256": surface_sha},
            "market_snapshot": {"sha256": market_sha},
        },
        "result": record(result_zip),
        "official_trade_surface": {"sha256": surface_sha},
        "container_memory": {"verdict": "within_limit"},
    }
    measurement = {
        "exit_code": 0,
        "result_sha256": surface_sha,
        "report": report,
        "output_directory": tmp_path,
    }
    inputs = {
        "lock": {
            "strategy": {"class_name": "NostalgiaForInfinityX7"},
            "data": {"seal_version": "1.3.0"},
            "pairlist": {"pairs": ["BTC/USDT"]},
            "scope": {
                "timerange": "20210101-20260101",
                "timeframes": ["5m"],
            },
        },
        "public": {
            "strategy_sha256": strategy_sha,
            "official_reference_config_sha256": config_sha256(config),
            "data_aggregate_sha256": data_sha,
            "reference_market_snapshot_sha256": market_sha,
        },
    }

    full_x7_resume.validate_reference_oracle(
        measurement,
        baseline=baseline,
        inputs=inputs,
        validator=full_x7_certification._reference_complete,
    )

    config["exchange"]["pair_whitelist"] = ["ETH/USDT"]
    write_json(config_path, config)
    report["inputs"]["config"] = record(config_path)
    with pytest.raises(BenchmarkError, match="official config"):
        full_x7_resume.validate_reference_oracle(
            measurement,
            baseline=baseline,
            inputs=inputs,
            validator=full_x7_certification._reference_complete,
        )

    config["exchange"]["pair_whitelist"] = ["BTC/USDT"]
    write_json(config_path, config)
    report["inputs"]["config"] = record(config_path)
    write_json(
        data_seal,
        {"schema_version": "1.3.0", "aggregate_sha256": "8" * 64},
    )
    report["inputs"]["data_seal"] = record(data_seal)
    with pytest.raises(BenchmarkError, match="candle data seal"):
        full_x7_resume.validate_reference_oracle(
            measurement,
            baseline=baseline,
            inputs=inputs,
            validator=full_x7_certification._reference_complete,
        )

    write_json(
        data_seal,
        {"schema_version": "1.3.0", "aggregate_sha256": data_sha},
    )
    report["inputs"]["data_seal"] = record(data_seal)
    with zipfile.ZipFile(result_zip, "w") as archive:
        archive.writestr(
            "backtest-result.meta.json",
            json.dumps(
                {
                    "NostalgiaForInfinityX7": {
                        "timeframe": "5m",
                        "backtest_start_ts": 1640995200,
                        "backtest_end_ts": 1767225600,
                    }
                }
            ),
        )
    report["result"] = record(result_zip)
    with pytest.raises(BenchmarkError, match="official timerange"):
        full_x7_resume.validate_reference_oracle(
            measurement,
            baseline=baseline,
            inputs=inputs,
            validator=full_x7_certification._reference_complete,
        )


def test_imported_oracle_reconciles_a_completed_official_export(
    tmp_path: Path,
) -> None:
    from nfi_backtest_engine.reference_runtime import (
        REFERENCE_PLATFORM,
        REFERENCE_PLATFORM_DIGEST,
    )

    source = tmp_path / "source-oracle"
    destination = tmp_path / "imported-oracle"
    source.mkdir()
    official_surface = source / "official-trade-surface.json"
    official_surface.write_bytes(b"sealed-official-surface")
    result_zip = source / "backtest-result.zip"
    with zipfile.ZipFile(result_zip, "w") as archive:
        archive.writestr(
            "backtest-result.meta.json",
            json.dumps(
                {
                    "NostalgiaForInfinityX7": {
                        "timeframe": "5m",
                        "backtest_start_ts": 1609459200,
                        "backtest_end_ts": 1767225600,
                    }
                }
            ),
        )
    strategy = source / "strategy.py"
    strategy.write_bytes(b"sealed-strategy")
    market = source / "reference-markets.json"
    market.write_bytes(b"sealed-market-snapshot")
    config = {
        "exchange": {"pair_whitelist": ["BTC/USDT"]},
        "pairlists": [{"method": "StaticPairList", "allow_inactive": True}],
    }
    config_path = source / "config.json"
    write_json(config_path, config)
    data_sha = "9" * 64
    data_seal = tmp_path / "data-seal.json"
    write_json(
        data_seal,
        {"schema_version": "1.3.0", "aggregate_sha256": data_sha},
    )
    baseline_surface = tmp_path / "native-trade-surface.json"
    baseline_surface.write_bytes(official_surface.read_bytes())

    def record(path: Path) -> dict[str, object]:
        return {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    surface_sha = record(official_surface)["sha256"]
    run_id = "a" * 64
    strategy_sha = record(strategy)["sha256"]
    market_sha = record(market)["sha256"]
    write_json(
        source / "run.json",
        {
            "schema_version": "1.3.0",
            "run_id": run_id,
            "wall_time_seconds": 3600.0,
            "exit_code": 0,
            "timed_out": False,
            "complete": False,
            "exact_parity": False,
            "difference": {"path": "$.summary.total_trades"},
            "reference": {
                "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
                "platform": REFERENCE_PLATFORM,
            },
            "inputs": {
                "strategy": record(strategy),
                "config": record(config_path),
                "data_seal": record(data_seal),
                "engine_trade_surface": {"sha256": "d" * 64},
                "market_snapshot": record(market),
            },
            "result": record(result_zip),
            "official_trade_surface": record(official_surface),
            "reference_storage": {"complete": True},
            "container_memory": {
                "verdict": "within_limit",
                "peak_bytes": 1024,
            },
        },
    )
    baseline = {
        "result_sha256": surface_sha,
        "report": {
            "run_id": "b" * 64,
            "result": {"trade_surface": record(baseline_surface)},
        },
    }
    inputs = {
        "lock": {
            "strategy": {"class_name": "NostalgiaForInfinityX7"},
            "data": {"seal_version": "1.3.0"},
            "pairlist": {"pairs": ["BTC/USDT"]},
            "scope": {
                "timerange": "20210101-20260101",
                "timeframes": ["5m"],
            },
        },
        "public": {
            "strategy_sha256": strategy_sha,
            "official_reference_config_sha256": config_sha256(config),
            "data_aggregate_sha256": data_sha,
            "reference_market_snapshot_sha256": market_sha,
        },
    }

    source.chmod(0o555)
    try:
        imported = full_x7_resume.import_reference_oracle(
            source,
            destination,
            baseline=baseline,
            inputs=inputs,
            validator=full_x7_certification._reference_complete,
        )
    finally:
        source.chmod(0o755)

    report = imported["report"]
    assert report["complete"] is True
    assert report["exact_parity"] is True
    assert report["difference"] is None
    assert report["parity_reconciliation"]["prior_engine_surface_sha256"] == "d" * 64
    assert report["inputs"]["engine_trade_surface"]["sha256"] == surface_sha
    assert Path(report["inputs"]["data_seal"]["path"]).is_relative_to(destination)
    assert (destination / "inputs" / "data-seal.json").is_file()
    assert (destination / result_zip.name).read_bytes() == result_zip.read_bytes()


def test_oracle_import_resumes_only_an_exact_source_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_inputs = source / "inputs"
    source_inputs.mkdir(parents=True)
    (source / "run.json").write_bytes(b"sealed-report")
    (source_inputs / "strategy.py").write_bytes(b"sealed-strategy")
    source_inputs.chmod(0o555)
    source.chmod(0o555)

    exact = tmp_path / "exact"
    changed = tmp_path / "changed"
    try:
        shutil.copytree(source, exact)
        full_x7_resume._prepare_reference_oracle_copy(source, exact)
        (exact / "inputs" / "local-record.json").write_bytes(b"local")

        shutil.copytree(source, changed)
        changed.chmod(0o755)
        (changed / "extra.json").write_bytes(b"unexpected")
        with pytest.raises(BenchmarkError, match="differs from the immutable source"):
            full_x7_resume._prepare_reference_oracle_copy(source, changed)
    finally:
        source.chmod(0o755)
        source_inputs.chmod(0o755)


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
    assert _engine_complete(measurement, lock, require_cold=False) is True


def test_listing_aware_engine_gate_requires_the_locked_shortfall_count() -> None:
    lock = {
        "schema_version": "1.3.0",
        "scope": {"history_coverage_policy": "listing-aware"},
        "data": {
            "aggregate_sha256": "d" * 64,
            "coverage_shortfall_count": 7,
        },
    }
    measurement = {
        "exit_code": 0,
        "result_sha256": "a" * 64,
        "report": {
            "complete": True,
            "pipeline_evidence": {"cold": True},
            "data": {
                "history_coverage_policy": "available",
                "coverage_shortfall_count": 7,
                "aggregate_sha256": "d" * 64,
            },
            "capability": {"blockers": []},
        },
    }

    assert _engine_complete(measurement, lock) is True
    measurement["report"]["data"]["coverage_shortfall_count"] = 6
    assert _engine_complete(measurement, lock) is False


def test_full_x7_probe_matrix_cannot_be_empty() -> None:
    with pytest.raises(SpecValidationError, match="probe matrix is incomplete"):
        _validate_probe_matrix([], contract=SPOT_RELEASE_CONTRACT)


def test_real_full_x7_probe_matrix_covers_every_required_branch() -> None:
    manifests = sorted(
        path / "manifest.json"
        for path in CAPTURED.iterdir()
        if path.name.startswith("x7-") and (path / "manifest.json").is_file()
    )
    upstream_commits = {
        read_json(path)["strategy_provenance"]["upstream_commit"] for path in manifests
    }
    assert len(upstream_commits) == 1
    upstream_commit = upstream_commits.pop()

    spot_manifests = []
    for path in manifests:
        manifest = read_json(path)
        config_record = next(
            item for item in manifest["inputs"] if item["role"] == "config"
        )
        config = read_json(path.parent / config_record["path"])
        if release_contract_for_config(config).trading_mode == "spot":
            spot_manifests.append(path)

    probes = _validate_probe_matrix(
        spot_manifests,
        contract=SPOT_RELEASE_CONTRACT,
        expected_upstream_commit=upstream_commit,
    )

    assert probes
    assert len(probes) == len(spot_manifests)
    with pytest.raises(SpecValidationError, match="upstream commit differs"):
        _validate_probe_matrix(
            spot_manifests[:1],
            contract=SPOT_RELEASE_CONTRACT,
            expected_upstream_commit="0" * 40,
        )


def test_current_futures_probe_matrix_is_release_complete() -> None:
    manifests = sorted(
        path / "manifest.json"
        for path in CAPTURED.iterdir()
        if "futures" in path.name and (path / "manifest.json").is_file()
    )
    upstream_commits = {
        read_json(path)["strategy_provenance"]["upstream_commit"] for path in manifests
    }
    assert len(upstream_commits) == 1

    probes = _validate_probe_matrix(
        manifests,
        contract=FUTURES_RELEASE_CONTRACT,
        expected_upstream_commit=upstream_commits.pop(),
    )

    assert probes
    assert len(probes) == len(manifests)


def test_futures_probe_matrix_requires_observed_lifecycle_and_all_protections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_observed = {
        "callbacks": [],
        "entry_tags": [],
        "compound_tags": [],
        "protection_methods": [],
        "exit_reasons": [],
        "sides": [],
        "leverages": [],
        "lock_count": 0,
        "funded_trades": 0,
        "rejected_locked_entry": False,
    }
    definitions = [
        ("tag-121", {**base_observed, "entry_tags": ["121"]}),
        (
            "futures-lifecycle",
            {
                **base_observed,
                "sides": ["long", "short"],
                "leverages": ["3"],
                "funded_trades": 1,
            },
        ),
        (
            "liquidation",
            {**base_observed, "exit_reasons": ["liquidation"]},
        ),
        (
            "compound-tags",
            {**base_observed, "compound_tags": ["141 142"]},
        ),
        (
            "variable-leverage",
            {**base_observed, "leverages": ["2", "3"]},
        ),
        *[
            (
                "protections-locks",
                {
                    **base_observed,
                    "protection_methods": [method],
                    "lock_count": 1,
                    "rejected_locked_entry": index == 0,
                },
            )
            for index, method in enumerate(
                (
                    "CooldownPeriod",
                    "StoplossGuard",
                    "MaxDrawdown",
                    "LowProfitPairs",
                )
            )
        ],
    ]
    manifests: dict[Path, dict] = {}
    observations: dict[Path, dict] = {}
    for index, (kind, observed) in enumerate(definitions):
        root = tmp_path / f"probe-{index}"
        root.mkdir()
        write_json(
            root / "config.json",
            {
                "trading_mode": "futures",
                "margin_mode": "isolated",
                "stake_currency": "USDT",
                "exchange": {
                    "name": "binance",
                    "pair_whitelist": ["BTC/USDT:USDT"],
                },
            },
        )
        path = (root / "manifest.json").resolve()
        manifests[path] = {
            "schema_version": "3.0.0",
            "probe_kind": kind,
            "inputs": [{"role": "config", "path": "config.json"}],
            "strategy_provenance": {"upstream_commit": "a" * 40},
        }
        observations[path] = observed

    monkeypatch.setattr(
        full_x7_certification,
        "validate_fixture",
        lambda path: manifests[Path(path).resolve()],
    )
    monkeypatch.setattr(
        full_x7_certification,
        "validate_fixture_coverage",
        lambda path, _manifest: {
            "met": True,
            "observed": observations[Path(path).resolve()],
        },
    )

    manifest_paths: list[str | Path] = list(manifests)
    probes = _validate_probe_matrix(
        manifest_paths,
        contract=FUTURES_RELEASE_CONTRACT,
        expected_upstream_commit="a" * 40,
    )
    assert len(probes) == len(definitions)

    lifecycle = next(
        path
        for path, manifest in manifests.items()
        if manifest["probe_kind"] == "futures-lifecycle"
    )
    observations[lifecycle]["funded_trades"] = 0
    with pytest.raises(SpecValidationError, match="funded_trades:0<1"):
        _validate_probe_matrix(
            manifest_paths,
            contract=FUTURES_RELEASE_CONTRACT,
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
    start_ms = 1_609_459_200_000
    end_ms = 1_767_225_600_000
    files = [
        {
            "path": f"BTC_USDT-{timeframe}.feather",
            "coverage": {
                "start_timestamp_ms": start_ms,
                "end_timestamp_ms": end_ms,
            },
        }
        for timeframe in timeframes
    ]
    lock = {
        "schema_version": "1.1.0",
        "pairlist": {"pairs": pairs},
        "scope": {
            "timerange": "20210101-20260101",
            "timeframes": timeframes,
        },
        "data": {
            "aggregate_sha256": "a" * 64,
            "file_count": len(files),
            "coverage_shortfall_count": 0,
            "startup_shortfall_count": 1,
            "startup_coverage_policy": "record",
        },
    }
    seal = {
        "data_root": str(data),
        "aggregate_sha256": "a" * 64,
        "files": files,
        "coverage_shortfalls": [],
        "startup_shortfalls": [{}],
        "request": {
            "exchange": "binance",
            "trading_mode": "spot",
            "pairs": pairs,
            "timerange": "20210101-20260101",
            "timeframes": timeframes,
            "start_timestamp_ms": start_ms,
            "end_timestamp_ms": end_ms,
            "history_coverage_policy": "strict",
            "startup_coverage_policy": "record",
        },
    }

    _validate_release_data_seal(
        lock,
        seal,
        data_directory=data,
        contract=SPOT_RELEASE_CONTRACT,
    )

    with pytest.raises(SpecValidationError, match="selected data directory"):
        _validate_release_data_seal(
            lock,
            seal,
            data_directory=tmp_path / "other-data",
            contract=SPOT_RELEASE_CONTRACT,
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
        return {
            "wall_time_seconds": 1.0,
            "peak_rss_bytes": 1,
            "exit_code": 0,
            "timed_out": False,
        }

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
    storage_index = arguments.index("--storage-mode")
    assert arguments[storage_index + 1] == "spooled"
