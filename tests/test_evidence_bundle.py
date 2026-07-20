from __future__ import annotations

import zipfile
from pathlib import Path

from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.evidence_bundle import (
    public_engine_build_record,
    public_hardware_record,
    write_evidence_bundle,
)


def test_release_bundle_includes_only_explicit_public_evidence(tmp_path: Path) -> None:
    report = tmp_path / "certification.json"
    private_cache = tmp_path / "measurements" / "vector-cache.bin"
    write_json(report, {"release_certified": True})
    private_cache.parent.mkdir()
    private_cache.write_bytes(b"large-private-cache")

    bundle = write_evidence_bundle(
        tmp_path,
        evidence_id="a" * 64,
        release_certified=True,
        include_paths=[report],
    )

    manifest = read_json(tmp_path / "bundle-manifest.json")
    assert [item["path"] for item in manifest["files"]] == ["certification.json"]
    with zipfile.ZipFile(tmp_path / bundle["archive"]["path"]) as archive:
        assert archive.namelist() == [
            "bundle-manifest.json",
            "certification.json",
        ]


def test_public_environment_records_remove_machine_local_paths() -> None:
    hardware = {
        "system": "Windows",
        "affinity_cpu_count": 8,
        "affinity_cpu_ids": list(range(8)),
        "workspace_disk": {
            "path": "C:/Users/private/project",
            "total_bytes": 100,
            "free_bytes": 50,
        },
    }
    build = {
        "kind": "pyo3-extension",
        "binary_path": "C:/Users/private/site-packages/_rust.pyd",
        "binary_sha256": "a" * 64,
    }

    public_hardware = public_hardware_record(hardware)
    public_build = public_engine_build_record(build)

    assert "affinity_cpu_ids" not in public_hardware
    assert "path" not in public_hardware["workspace_disk"]
    assert "binary_path" not in public_build
