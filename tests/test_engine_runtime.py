from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nfi_backtest_engine import engine_runtime
from nfi_backtest_engine.canonical import write_json


def _rust_checkout(root: Path) -> Path:
    rust = root / "rust"
    crate = rust / "crates" / "example" / "src"
    crate.mkdir(parents=True)
    (rust / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    (rust / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (crate / "lib.rs").write_text("pub fn value() -> u8 { 1 }\n", encoding="utf-8")
    return rust


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
