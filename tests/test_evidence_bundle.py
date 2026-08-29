from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from nfi_backtest_engine import evidence_bundle
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.certification_parts import packaging
from nfi_backtest_engine.certification_parts.packaging import (
    _write_certification_publication,
)
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


@pytest.mark.parametrize("failure_call", [1, 2, 3])
def test_bundle_commit_failure_removes_only_current_transaction_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_call: int
) -> None:
    report = tmp_path / "certification.json"
    write_json(report, {"release_certified": True})
    real_publish = evidence_bundle._publish_no_clobber
    calls = 0

    def fail_at(staged: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("injected publication failure")
        real_publish(staged, destination)

    monkeypatch.setattr(evidence_bundle, "_publish_no_clobber", fail_at)
    with pytest.raises(OSError, match="injected"):
        write_evidence_bundle(
            tmp_path,
            evidence_id="a" * 64,
            release_certified=True,
            include_paths=[report],
        )

    assert sorted(path.name for path in tmp_path.iterdir()) == ["certification.json"]


def test_legacy_certification_archive_uses_exact_manifest_bound_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "report.json"
    source.write_bytes(b"OLD-BYTES")
    mutated = False

    def mutate(checkpoint: str) -> None:
        nonlocal mutated
        if checkpoint == "after-source-snapshot" and not mutated:
            source.write_bytes(b"NEW-BYTES")
            mutated = True

    monkeypatch.setattr(packaging, "_certification_checkpoint", mutate)
    _write_certification_publication(
        tmp_path,
        [source],
        fixture_id="fixture",
        release_certified=False,
    )

    assert mutated
    manifest = read_json(tmp_path / "bundle-manifest.json")
    with zipfile.ZipFile(tmp_path / "certification-bundle.zip") as archive:
        archived = archive.read("report.json")
    assert archived == b"OLD-BYTES"
    assert manifest["files"][0]["sha256"] == __import__("hashlib").sha256(
        archived
    ).hexdigest()


@pytest.mark.parametrize(
    "checkpoint",
    [
        "after-source-snapshot",
        "after-manifest-sync",
        "after-zip-sync",
        "before-publish",
        "published-certification-bundle.zip",
        "published-bundle-manifest.json",
        "published-bundle.json",
        "after-directory-sync",
    ],
)
def test_legacy_certification_interruption_removes_only_owned_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, checkpoint: str
) -> None:
    source = tmp_path / "report.json"
    source.write_bytes(b"SOURCE")

    def interrupt(name: str) -> None:
        if name == checkpoint:
            raise KeyboardInterrupt

    monkeypatch.setattr(packaging, "_certification_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _write_certification_publication(
            tmp_path,
            [source],
            fixture_id="fixture",
            release_certified=False,
        )

    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.json"]


def test_legacy_certification_detects_staged_source_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "report.json"
    source.write_bytes(b"SOURCE")
    real_write = packaging._write_zip_entries

    def mutate_then_write(destination: Path, entries: list[tuple[Path, str]]) -> None:
        entries[0][0].write_bytes(b"CHANGED")
        real_write(destination, entries)

    monkeypatch.setattr(packaging, "_write_zip_entries", mutate_then_write)
    with pytest.raises(ValueError, match="differ"):
        _write_certification_publication(
            tmp_path,
            [source],
            fixture_id="fixture",
            release_certified=False,
        )

    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.json"]


def test_legacy_certification_publication_never_clobbers_existing_assets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "certification.json"
    source.write_bytes(b"SOURCE")
    sentinels = {
        tmp_path / "certification-bundle.zip": b"OLD-ARCHIVE",
        tmp_path / "bundle-manifest.json": b"OLD-MANIFEST",
        tmp_path / "bundle.json": b"OLD-BUNDLE",
    }
    for path, content in sentinels.items():
        path.write_bytes(content)
    before = {
        path: (path.stat().st_mode, path.stat().st_mtime_ns, path.read_bytes())
        for path in sentinels
    }

    with pytest.raises(ValueError, match="exists|clobbered"):
        _write_certification_publication(
            tmp_path,
            [source],
            fixture_id="fixture",
            release_certified=False,
        )

    assert {
        path: (path.stat().st_mode, path.stat().st_mtime_ns, path.read_bytes())
        for path in sentinels
    } == before


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
