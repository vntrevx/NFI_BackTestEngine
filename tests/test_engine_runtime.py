from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from nfi_backtest_engine import engine_runtime, execution_platform
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import BenchmarkError
from nfi_backtest_engine.execution_platform import NATIVE_WINDOWS_UNSUPPORTED_MESSAGE


def _commit_mock_bundle(result: Path, profile: Path | None = None) -> None:
    bundle = engine_runtime._publication_bundle(result)
    bundle.mkdir()
    (bundle / "result.json").write_bytes(result.read_bytes())
    if profile is not None:
        (bundle / "profile.json").write_bytes(profile.read_bytes())
    write_json(bundle / "publication.json", {"schema_version": "1.0.0"})


def _rust_checkout(root: Path) -> Path:
    rust = root / "rust"
    crate = rust / "crates" / "example" / "src"
    crate.mkdir(parents=True)
    (rust / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    (rust / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (crate / "lib.rs").write_text("pub fn value() -> u8 { 1 }\n", encoding="utf-8")
    return rust


def test_project_root_ignores_a_release_venv_nested_in_a_checkout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    (root / "rust").mkdir(parents=True)
    (root / "rust" / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    source_module = root / "python" / "nfi_backtest_engine" / "engine_runtime.py"
    source_module.parent.mkdir(parents=True)
    source_module.write_text("", encoding="utf-8")
    wheel_module = (
        root / ".venv" / "Lib" / "site-packages" / "nfi_backtest_engine" / "engine_runtime.py"
    )
    wheel_module.parent.mkdir(parents=True)
    wheel_module.write_text("", encoding="utf-8")

    monkeypatch.setattr(engine_runtime, "__file__", str(source_module))
    assert engine_runtime._project_root_or_none() == root

    monkeypatch.setattr(engine_runtime, "__file__", str(wheel_module))
    assert engine_runtime._project_root_or_none() is None


def test_direct_engine_entrypoints_reject_native_windows_before_engine_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(execution_platform.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        engine_runtime,
        "_native_module",
        lambda: pytest.fail("native engine must not load"),
    )

    with pytest.raises(BenchmarkError) as build_error:
        engine_runtime.build_engine()
    assert str(build_error.value) == NATIVE_WINDOWS_UNSUPPORTED_MESSAGE

    with pytest.raises(BenchmarkError) as run_error:
        engine_runtime.run_engine(tmp_path / "missing.json", tmp_path / "result.json")
    assert str(run_error.value) == NATIVE_WINDOWS_UNSUPPORTED_MESSAGE


def test_execution_platform_accepts_linux_abi_including_wsl2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_platform.platform, "system", lambda: "Linux")

    execution_platform.require_supported_execution_platform()


def test_build_engine_uses_native_only_when_checkout_source_matches(
    monkeypatch, tmp_path: Path
) -> None:
    rust = _rust_checkout(tmp_path)
    fingerprint = engine_runtime._rust_source_fingerprint(rust)
    extension = tmp_path / "_rust.abi3.so"
    extension.write_bytes(b"fresh-native")
    native = SimpleNamespace(
        __file__=str(extension),
        source_fingerprint=lambda: fingerprint,
    )
    monkeypatch.setattr(engine_runtime, "_native_module", lambda: native)
    monkeypatch.setattr(engine_runtime, "_project_root_or_none", lambda: tmp_path)

    record = engine_runtime.build_engine()

    assert record["kind"] == "pyo3-extension"
    assert record["source_fingerprint"] == fingerprint


def test_developed_native_fingerprint_matches_checkout() -> None:
    root = engine_runtime._project_root_or_none()
    native = engine_runtime._native_module()

    assert root is not None
    assert native is not None
    assert engine_runtime._native_source_fingerprint(native) == (
        engine_runtime._rust_source_fingerprint(root / "rust")
    )


def test_build_engine_falls_back_to_fresh_cli_when_imported_native_is_stale(
    monkeypatch, tmp_path: Path
) -> None:
    rust = _rust_checkout(tmp_path)
    fingerprint = engine_runtime._rust_source_fingerprint(rust)
    extension = tmp_path / "_rust.abi3.so"
    extension.write_bytes(b"stale-native")
    native = SimpleNamespace(
        __file__=str(extension),
        source_fingerprint=lambda: "0" * 64,
    )
    binary = rust / "target" / "release" / "nfi-sim"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fresh-cli")
    marker = binary.with_suffix(".build.json")
    expected = {
        "schema_version": "1.0.0",
        "source_fingerprint": fingerprint,
        "binary_path": str(binary),
        "binary_sha256": "fixture",
        "kind": "standalone-cli",
    }
    write_json(marker, expected)
    monkeypatch.setattr(engine_runtime, "_native_module", lambda: native)
    monkeypatch.setattr(engine_runtime, "_project_root_or_none", lambda: tmp_path)
    monkeypatch.setattr(engine_runtime, "_engine_binary", lambda: binary)

    assert engine_runtime.build_engine() == expected


def test_run_engine_native_profile_keeps_result_and_reports_separate_phases(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "vectors.manifest.json"
    output = tmp_path / "result.json"
    profile = tmp_path / "engine-profile.json"
    write_json(source, {"schema_version": "fixture"})

    def simulate_vector_file_profiled(
        input_path: Path,
        output_path: Path,
        profile_path: Path,
        events_path: Path | None,
    ) -> None:
        assert input_path == source
        assert events_path is None
        write_json(output_path, {"trades": [{"id": 1}]})
        write_json(
            profile_path,
            {
                "schema_version": "1.0.0",
                "input": {"feather_decode_ns": 11},
                "simulation": {"event_loop_ns": 22},
                "serialization_ns": 33,
            },
        )
        _commit_mock_bundle(output_path, profile_path)

    native = SimpleNamespace(simulate_vector_file_profiled=simulate_vector_file_profiled)
    monkeypatch.setattr(
        engine_runtime,
        "build_engine",
        lambda: {"kind": "pyo3-extension", "binary_path": "fixture"},
    )
    monkeypatch.setattr(engine_runtime, "_native_module", lambda: native)

    report = engine_runtime.run_engine(
        source,
        output,
        vector_manifest=True,
        engine_profile_path=profile,
    )

    assert report["trade_count"] == 1
    assert report["profile"]["phases"]["input"]["feather_decode_ns"] == 11
    assert report["profile"]["phases"]["simulation"]["event_loop_ns"] == 22


def test_failed_native_attempt_never_deletes_concurrently_published_pair(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "vectors.manifest.json"
    output = tmp_path / "result.json"
    profile = tmp_path / "engine-profile.json"
    write_json(source, {"schema_version": "fixture"})

    def simulate_vector_file_profiled(
        input_path: Path,
        output_path: Path,
        profile_path: Path,
        events_path: Path | None,
    ) -> None:
        assert input_path == source
        assert events_path is None
        write_json(output_path, {"identity": "concurrent-winner", "trades": []})
        write_json(profile_path, {"identity": "concurrent-winner"})
        raise ValueError("losing writer failed after winner published")

    native = SimpleNamespace(simulate_vector_file_profiled=simulate_vector_file_profiled)
    monkeypatch.setattr(
        engine_runtime,
        "build_engine",
        lambda: {"kind": "pyo3-extension", "binary_path": "fixture"},
    )
    monkeypatch.setattr(engine_runtime, "_native_module", lambda: native)

    with pytest.raises(BenchmarkError, match="losing writer failed"):
        engine_runtime.run_engine(
            source,
            output,
            vector_manifest=True,
            engine_profile_path=profile,
        )

    assert read_json(output)["identity"] == "concurrent-winner"
    assert read_json(profile)["identity"] == "concurrent-winner"


def test_checked_in_feather_replay_has_zero_recursive_result_delta(tmp_path: Path) -> None:
    native = engine_runtime._native_module()
    assert native is not None
    fixture_source = (
        Path(__file__).parents[1]
        / "benchmarks/evidence/m22/current-head-649890f7/native/spot"
    )
    fixture = tmp_path / "fixture"
    shutil.copytree(fixture_source, fixture)
    (fixture / "vectors").mkdir()
    shutil.copy2(fixture / "vector.feather", fixture / "vectors/BTC_USDT.feather")
    output = tmp_path / "result.json"

    native.simulate_vector_file(fixture / "simulation-input.manifest.json", output)

    assert read_json(output) == read_json(fixture / "simulation-result.json")


def test_native_event_trace_commits_with_result_bundle(tmp_path: Path) -> None:
    native = engine_runtime._native_module()
    assert native is not None
    source = tmp_path / "simulation.json"
    output = tmp_path / "result.json"
    events = tmp_path / "events.jsonl"
    write_json(
        source,
        {
            "schema_version": "1.0.0",
            "config": {
                "starting_balance": 1000.0,
                "max_open_trades": 1,
                "stake_amount": 100.0,
                "fee_rate": 0.001,
                "stoploss_ratio": -0.01,
                "amount_step": 0.00001,
                "price_step": 0.01,
            },
            "pairs": [
                {
                    "pair": "AAA/USDT",
                    "candles": [
                        {
                            "timestamp_ms": 1,
                            "open": 100.0,
                            "high": 100.0,
                            "low": 100.0,
                            "close": 100.0,
                            "volume": 1.0,
                            "enter_long": {"tag": None},
                        },
                        {
                            "timestamp_ms": 2,
                            "open": 101.0,
                            "high": 101.0,
                            "low": 101.0,
                            "close": 101.0,
                            "volume": 1.0,
                        },
                    ],
                }
            ],
        },
    )

    native.simulate_file(source, output, events)

    bundle = engine_runtime._publication_bundle(output)
    assert output.read_bytes() == (bundle / "result.json").read_bytes()
    assert events.read_bytes() == (bundle / "events.jsonl").read_bytes()
    manifest = read_json(bundle / "publication.json")
    assert manifest["commit_schema"] == "nfi-artifact-publication-v1"
    assert [artifact["name"] for artifact in manifest["artifacts"]] == [
        "result.json",
        "profile.json",
        "events.jsonl",
    ]
    assert read_json(bundle / "profile.json") == {
        "schema_version": "1.0.0",
        "measurement": "unprofiled",
    }
    assert all(len(artifact["sha256"]) == 64 for artifact in manifest["artifacts"])


def test_native_exact_failure_never_truncates_preexisting_event_artifact(
    tmp_path: Path,
) -> None:
    native = engine_runtime._native_module()
    assert native is not None
    source = tmp_path / "overflow.json"
    output = tmp_path / "result.json"
    events = tmp_path / "events.jsonl"
    events.write_text("other-writer-event", encoding="utf-8")
    write_json(
        source,
        {
            "schema_version": "1.0.0",
            "config": {
                "starting_balance": 1000.0,
                "max_open_trades": 1,
                "stake_amount": 100.0,
                "fee_rate": 0.001,
                "stoploss_ratio": -0.01,
                "amount_step": 0.00001,
                "price_step": 0.01,
            },
            "pairs": [
                {
                    "pair": "MAX/USDT",
                    "candles": [
                        {
                            "timestamp_ms": 1,
                            "open": 1.0,
                            "high": 1.0,
                            "low": 1.0,
                            "close": 1.0,
                            "volume": 1.0,
                            "enter_long": {"tag": None},
                        },
                        {
                            "timestamp_ms": 2,
                            "open": float.fromhex("0x1.fffffffffffffp+1023"),
                            "high": float.fromhex("0x1.fffffffffffffp+1023"),
                            "low": 1.0,
                            "close": float.fromhex("0x1.fffffffffffffp+1023"),
                            "volume": 1.0,
                            "exit_long": {"reason": "overflow"},
                        },
                    ],
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="code=exact_arithmetic"):
        native.simulate_file(source, output, events)

    assert events.read_text(encoding="utf-8") == "other-writer-event"
    assert not output.exists()
    assert not engine_runtime._publication_bundle(output).exists()
    assert sorted(tmp_path.glob(".*.tmp")) == []


def test_native_max_wallet_overflow_is_typed_and_publishes_no_null_result(
    tmp_path: Path,
) -> None:
    native = engine_runtime._native_module()
    assert native is not None
    maximum = float.fromhex("0x1.fffffffffffffp+1023")
    source = tmp_path / "wallet-overflow.json"
    output = tmp_path / "result.json"
    events = tmp_path / "events.jsonl"
    write_json(
        source,
        {
            "schema_version": "1.0.0",
            "config": {
                "starting_balance": maximum,
                "max_open_trades": 1,
                "stake_amount": maximum / 4.0,
                "fee_rate": 0.0,
                "stoploss_ratio": -0.01,
                "amount_step": 1.0,
                "price_step": 1.0,
            },
            "pairs": [
                {
                    "pair": "MAX/USDT",
                    "candles": [
                        {
                            "timestamp_ms": 1,
                            "open": 1.0,
                            "high": 1.0,
                            "low": 1.0,
                            "close": 1.0,
                            "volume": 1.0,
                            "enter_long": {"tag": None},
                        },
                        {
                            "timestamp_ms": 2,
                            "open": 2.0,
                            "high": 2.0,
                            "low": 2.0,
                            "close": 2.0,
                            "volume": 1.0,
                            "exit_long": {"reason": "wallet-overflow"},
                        },
                    ],
                }
            ],
        },
    )

    with pytest.raises(
        ValueError,
        match="code=exact_arithmetic operation=wallet-final-balance reason=unrepresentable",
    ):
        native.simulate_file(source, output, events)

    assert not output.exists()
    assert not events.exists()
    assert not engine_runtime._publication_bundle(output).exists()
    assert sorted(tmp_path.glob(".*.tmp")) == []


def test_failed_native_attempt_never_deletes_concurrently_published_events(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "simulation.json"
    output = tmp_path / "result.json"
    events = tmp_path / "events.jsonl"
    write_json(source, {"schema_version": "fixture"})

    def simulate_file(
        input_path: Path,
        output_path: Path,
        events_path: Path | None,
    ) -> None:
        assert input_path == source
        assert output_path == output
        assert events_path == events
        events.write_text('{"identity":"concurrent-winner"}\n', encoding="utf-8")
        raise ValueError("losing writer failed after event winner published")

    native = SimpleNamespace(simulate_file=simulate_file)
    monkeypatch.setattr(
        engine_runtime,
        "build_engine",
        lambda: {"kind": "pyo3-extension", "binary_path": "fixture"},
    )
    monkeypatch.setattr(engine_runtime, "_native_module", lambda: native)

    with pytest.raises(BenchmarkError, match="event winner published"):
        engine_runtime.run_engine(source, output, events_path=events)

    assert events.read_text(encoding="utf-8") == ('{"identity":"concurrent-winner"}\n')
    assert not output.exists()


def test_run_engine_selects_direct_execution_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "simulation.json"
    output = tmp_path / "result.json"
    execution_events = tmp_path / "execution-events.jsonl"
    write_json(source, {"schema_version": "fixture"})

    def simulate_file(
        input_path: Path,
        output_path: Path,
        events_path: Path | None,
        *,
        execution_events: bool = False,
    ) -> None:
        assert input_path == source
        assert output_path == output
        assert events_path == tmp_path / "execution-events.jsonl"
        assert execution_events is True
        write_json(output_path, {"trades": []})
        assert events_path is not None
        events_path.write_text(
            '{"schema_version":"execution-boundary-event-v1"}\n',
            encoding="utf-8",
        )
        _commit_mock_bundle(output_path)

    native = SimpleNamespace(simulate_file=simulate_file)
    monkeypatch.setattr(
        engine_runtime,
        "build_engine",
        lambda: {"kind": "pyo3-extension", "binary_path": "fixture"},
    )
    monkeypatch.setattr(engine_runtime, "_native_module", lambda: native)

    report = engine_runtime.run_engine(
        source,
        output,
        execution_events_path=execution_events,
    )

    assert report["events"] is None
    assert report["execution_events"]["path"] == str(execution_events)


def test_run_engine_rejects_two_event_streams_before_build(
    tmp_path: Path,
) -> None:
    source = tmp_path / "simulation.json"
    write_json(source, {"schema_version": "fixture"})

    with pytest.raises(BenchmarkError, match="mutually exclusive"):
        engine_runtime.run_engine(
            source,
            tmp_path / "result.json",
            events_path=tmp_path / "scheduler.jsonl",
            execution_events_path=tmp_path / "execution.jsonl",
        )


def test_committed_bundle_runs_owned_recovery_before_rejecting_resume(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "vectors.manifest.json"
    output = tmp_path / "result.json"
    profile = tmp_path / "profile.json"
    write_json(source, {"schema_version": "fixture"})
    bundle = engine_runtime._publication_bundle(output)
    bundle.mkdir()
    write_json(bundle / "publication.json", {"schema_version": "1.0.0"})
    write_json(bundle / "result.json", {"trades": []})
    write_json(bundle / "profile.json", {"input": {}})
    recovered: list[tuple[Path, Path | None, Path | None]] = []
    native = SimpleNamespace(
        recover_result_publication=lambda result, phases, events: recovered.append(
            (result, phases, events)
        )
    )
    monkeypatch.setattr(engine_runtime, "_native_module", lambda: native)

    with pytest.raises(BenchmarkError, match="already exists"):
        engine_runtime.run_engine(
            source,
            output,
            vector_manifest=True,
            engine_profile_path=profile,
        )

    assert recovered == [(output, profile, None)]


def test_invalid_committed_bundle_is_a_typed_runtime_failure_without_exports(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "vectors.manifest.json"
    output = tmp_path / "result.json"
    profile = tmp_path / "profile.json"
    write_json(source, {"schema_version": "fixture"})
    bundle = engine_runtime._publication_bundle(output)
    bundle.mkdir()
    (bundle / "publication.json").write_text("{malformed", encoding="utf-8")
    native = SimpleNamespace(
        recover_result_publication=lambda *_args: (_ for _ in ()).throw(
            ValueError("invalid publication bundle")
        )
    )
    monkeypatch.setattr(engine_runtime, "_native_module", lambda: native)

    with pytest.raises(BenchmarkError, match="publication recovery failed"):
        engine_runtime.run_engine(
            source,
            output,
            vector_manifest=True,
            engine_profile_path=profile,
        )

    assert not output.exists()
    assert not profile.exists()


def test_engine_profile_requires_vector_input(tmp_path: Path) -> None:
    source = tmp_path / "simulation.json"
    write_json(source, {"schema_version": "fixture"})

    with pytest.raises(BenchmarkError, match="requires a vector input"):
        engine_runtime.run_engine(
            source,
            tmp_path / "result.json",
            engine_profile_path=tmp_path / "engine-profile.json",
        )


def test_run_engine_selects_full_native_profiled_entrypoint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "full-native.manifest.json"
    output = tmp_path / "result.json"
    profile = tmp_path / "engine-profile.json"
    write_json(source, {"schema_version": "full-native-vector-manifest-v1"})

    def simulate_full_vector_file_profiled(
        input_path: Path,
        output_path: Path,
        profile_path: Path,
        events_path: Path | None,
        pair_worker_limit: int,
    ) -> None:
        assert input_path == source
        assert events_path is None
        assert pair_worker_limit == 3
        write_json(output_path, {"trades": []})
        write_json(profile_path, {"input": {"raw_frame_count": 5}})
        _commit_mock_bundle(output_path, profile_path)

    native = SimpleNamespace(simulate_full_vector_file_profiled=simulate_full_vector_file_profiled)
    monkeypatch.setattr(
        engine_runtime,
        "build_engine",
        lambda: {"kind": "pyo3-extension", "binary_path": "fixture"},
    )
    monkeypatch.setattr(engine_runtime, "_native_module", lambda: native)

    report = engine_runtime.run_engine(
        source,
        output,
        input_kind=engine_runtime.FULL_VECTOR_INPUT,
        engine_profile_path=profile,
        pair_worker_limit=3,
    )

    assert report["input_kind"] == engine_runtime.FULL_VECTOR_INPUT
    assert report["pair_worker_limit"] == 3
    assert report["profile"]["phases"]["input"]["raw_frame_count"] == 5


def test_engine_input_kind_never_uses_filename_inference(tmp_path: Path) -> None:
    source = tmp_path / "looks-like-vectors.manifest.json"
    write_json(source, {"schema_version": "fixture"})

    with pytest.raises(BenchmarkError, match="unsupported engine input kind"):
        engine_runtime.run_engine(
            source,
            tmp_path / "result.json",
            input_kind="unknown-vector-kind",
        )
    with pytest.raises(BenchmarkError, match="conflicts"):
        engine_runtime.run_engine(
            source,
            tmp_path / "result.json",
            input_kind=engine_runtime.FULL_VECTOR_INPUT,
            vector_manifest=True,
        )
    with pytest.raises(BenchmarkError, match="requires full-vector"):
        engine_runtime.run_engine(
            source,
            tmp_path / "pair-workers.json",
            pair_worker_limit=2,
        )
    with pytest.raises(BenchmarkError, match="positive integer"):
        engine_runtime.run_engine(
            source,
            tmp_path / "zero-workers.json",
            input_kind=engine_runtime.FULL_VECTOR_INPUT,
            pair_worker_limit=0,
        )


def test_failed_cli_attempt_cleans_private_resource_capture(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "simulation.json"
    output = tmp_path / "result.json"
    binary = tmp_path / "nfi-sim"
    stale_legacy_capture = tmp_path / ".result.json.resources"
    write_json(source, {"schema_version": "fixture"})
    binary.write_bytes(b"fixture")
    stale_legacy_capture.write_bytes(b"")
    monkeypatch.setattr(
        engine_runtime,
        "build_engine",
        lambda: {"kind": "standalone-cli", "binary_path": str(binary)},
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="fixture failure",
        ),
    )

    with pytest.raises(BenchmarkError, match="fixture failure"):
        engine_runtime.run_engine(source, output)

    # A failed attempt owns and removes only its unique capture. An abandoned
    # legacy filename cannot block resume and is not silently overwritten.
    assert stale_legacy_capture.is_file()
    assert sorted(tmp_path.glob(".result.json.*.resources")) == []


def test_macos_cli_fallback_never_uses_gnu_time_options(
    monkeypatch,
    tmp_path: Path,
) -> None:
    resource_path = tmp_path / "resources.txt"
    monkeypatch.setattr(platform, "system", lambda: "Darwin")

    assert engine_runtime._gnu_time_prefix(resource_path) == []
