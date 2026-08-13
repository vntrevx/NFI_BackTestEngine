from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from nfi_backtest_engine import engine_runtime
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import BenchmarkError


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
        root
        / ".venv"
        / "Lib"
        / "site-packages"
        / "nfi_backtest_engine"
        / "engine_runtime.py"
    )
    wheel_module.parent.mkdir(parents=True)
    wheel_module.write_text("", encoding="utf-8")

    monkeypatch.setattr(engine_runtime, "__file__", str(source_module))
    assert engine_runtime._project_root_or_none() == root

    monkeypatch.setattr(engine_runtime, "__file__", str(wheel_module))
    assert engine_runtime._project_root_or_none() is None


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

    native = SimpleNamespace(
        simulate_vector_file_profiled=simulate_vector_file_profiled
    )
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

    native = SimpleNamespace(
        simulate_full_vector_file_profiled=simulate_full_vector_file_profiled
    )
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
        engine_runtime.subprocess,
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
    monkeypatch.setattr(engine_runtime.platform, "system", lambda: "Darwin")

    assert engine_runtime._gnu_time_prefix(resource_path) == []
