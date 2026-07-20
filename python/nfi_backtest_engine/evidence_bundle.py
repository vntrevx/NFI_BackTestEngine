"""Reproducible, content-addressed evidence bundle creation."""

from __future__ import annotations

import shutil
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .canonical import write_json
from .fixture import sha256_file


def public_hardware_record(hardware: dict[str, Any]) -> dict[str, Any]:
    """Remove machine-local paths while preserving benchmark-relevant facts."""
    disk = hardware.get("workspace_disk")
    public_disk = (
        {
            "total_bytes": disk.get("total_bytes"),
            "free_bytes": disk.get("free_bytes"),
        }
        if isinstance(disk, dict)
        else None
    )
    return {
        key: value
        for key, value in hardware.items()
        if key not in {"workspace_disk", "affinity_cpu_ids"}
    } | {
        "affinity_cpu_count": hardware.get("affinity_cpu_count"),
        "workspace_disk": public_disk,
    }


def public_engine_build_record(build: dict[str, Any]) -> dict[str, Any]:
    """Keep native build identity without publishing an installation path."""
    return {
        key: value
        for key, value in build.items()
        if key != "binary_path"
    }


def artifact_record(path: str | Path, *, relative_to: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    root = Path(relative_to).resolve()
    return {
        "path": source.relative_to(root).as_posix(),
        "bytes": source.stat().st_size,
        "sha256": sha256_file(source),
    }


def write_evidence_bundle(
    root: str | Path,
    *,
    evidence_id: str,
    release_certified: bool,
    archive_name: str = "certification-bundle.zip",
    include_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """Package selected evidence using stable paths and timestamps.

    Callers producing large backtests should pass ``include_paths``. This keeps
    vector caches and databases available for local diagnosis without accidentally
    turning a public release asset into a multi-gigabyte archive.
    """
    directory = Path(root).resolve()
    manifest_path = directory / "bundle-manifest.json"
    bundle_path = directory / "bundle.json"
    archive_path = directory / archive_name
    excluded = {manifest_path, bundle_path, archive_path}
    if include_paths is None:
        candidates = (
            path
            for path in directory.rglob("*")
            if path.is_file() and path not in excluded
        )
    else:
        candidates = (Path(path).resolve() for path in include_paths)
    resolved = set(candidates)
    for path in resolved:
        if not path.is_relative_to(directory):
            raise ValueError(f"evidence file is outside the bundle root: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"evidence file does not exist: {path}")
    included = sorted(
        resolved,
        key=lambda path: path.relative_to(directory).as_posix(),
    )
    manifest = {
        "schema_version": "1.0.0",
        "evidence_id": evidence_id,
        "files": [
            artifact_record(path, relative_to=directory)
            for path in included
        ],
    }
    write_json(manifest_path, manifest)
    included.append(manifest_path)
    _write_reproducible_zip(archive_path, directory, included)
    bundle = {
        "schema_version": "1.0.0",
        "evidence_id": evidence_id,
        "release_certified": release_certified,
        "archive": artifact_record(archive_path, relative_to=directory),
        "manifest": artifact_record(manifest_path, relative_to=directory),
    }
    write_json(bundle_path, bundle)
    return bundle


def _write_reproducible_zip(
    destination: Path,
    root: Path,
    sources: list[Path],
) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for source in sorted(sources, key=lambda path: path.relative_to(root).as_posix()):
            relative = source.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            with source.open("rb") as input_file, archive.open(info, "w") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
