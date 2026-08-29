"""Reproducible, content-addressed evidence bundle creation."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .canonical import write_json
from .errors import InputBoundaryError
from .fixture import sha256_file
from .portable_paths import (
    parse_portable_relative_path,
    validate_portable_filesystem_path,
)
from .windows_path_security import open_windows_contained_descriptor


@dataclass(slots=True)
class _PreparedFile:
    name: str
    handle: BinaryIO
    size: int
    sha256: str
    device: int
    inode: int


def public_hardware_record(hardware: dict[str, Any]) -> dict[str, Any]:
    """Remove machine-local paths while preserving benchmark-relevant facts."""
    disk = hardware.get("workspace_disk")
    public_disk = (
        {"total_bytes": disk.get("total_bytes"), "free_bytes": disk.get("free_bytes")}
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
    return {key: value for key, value in build.items() if key != "binary_path"}


def artifact_record(path: str | Path, *, relative_to: str | Path) -> dict[str, Any]:
    root = _trusted_root(relative_to)
    root_fd = _open_root(root)
    prepared: _PreparedFile | None = None
    try:
        name = _canonical_bundle_name(path, root)
        prepared = _prepare_file(root_fd, root, name)
        _verify_prepared_path(root_fd, root, prepared)
        return _prepared_record(prepared)
    finally:
        if prepared is not None:
            prepared.handle.close()
        if root_fd is not None:
            os.close(root_fd)


def write_evidence_bundle(
    root: str | Path,
    *,
    evidence_id: str,
    release_certified: bool,
    archive_name: str = "certification-bundle.zip",
    include_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """Preflight every input, then transactionally publish one evidence bundle."""
    directory = _trusted_root(root)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        archive_relative = parse_portable_relative_path(archive_name)
    except InputBoundaryError as exc:
        raise InputBoundaryError(
            "bundle archive name must be one canonical portable filename"
        ) from exc
    if len(archive_relative.parts) != 1:
        raise InputBoundaryError("bundle archive name must be one canonical filename")
    manifest_path = directory / "bundle-manifest.json"
    bundle_path = directory / "bundle.json"
    archive_path = directory / archive_name
    published = (archive_path, manifest_path, bundle_path)
    owned = set(published)
    for destination in published:
        if destination.exists() or destination.is_symlink():
            raise InputBoundaryError(
                f"bundle destination already exists and will not be clobbered: {destination}"
            )
    created: dict[Path, tuple[int, int]] = {}
    prepared: list[_PreparedFile] = []
    root_fd: int | None = None
    try:
        regular_entries = _validated_regular_entries(directory, excluded=owned)
        selected: Iterable[str | Path] = (
            regular_entries if include_paths is None else include_paths
        )
        names: set[str] = set()
        normalized_names: set[str] = set()
        canonical_names: list[str] = []
        reserved = {archive_name, "bundle-manifest.json", "bundle.json"}
        for source in selected:
            name = _canonical_bundle_name(source, directory)
            normalized = unicodedata.normalize("NFC", name).casefold()
            if name in reserved:
                raise InputBoundaryError(f"bundle member uses a reserved name: {name}")
            if name in names or normalized in normalized_names:
                raise InputBoundaryError(
                    f"duplicate or case-colliding bundle member name: {name}"
                )
            names.add(name)
            normalized_names.add(normalized)
            canonical_names.append(name)
        root_fd = _open_root(directory)
        for name in canonical_names:
            prepared.append(_prepare_file(root_fd, directory, name))
        for item in prepared:
            _verify_prepared_path(root_fd, directory, item)
        prepared.sort(key=lambda item: item.name)
        manifest = {
            "schema_version": "1.0.0",
            "evidence_id": evidence_id,
            "files": [_prepared_record(item) for item in prepared],
        }

        with tempfile.TemporaryDirectory(
            prefix=".nfi-bundle-", dir=directory.parent
        ) as raw_stage:
            stage = Path(raw_stage)
            staged_manifest = stage / "bundle-manifest.json"
            staged_archive = stage / archive_name
            staged_bundle = stage / "bundle.json"
            write_json(staged_manifest, manifest)
            _write_prepared_zip(staged_archive, prepared, staged_manifest)
            bundle = {
                "schema_version": "1.0.0",
                "evidence_id": evidence_id,
                "release_certified": release_certified,
                "archive": _artifact_record_named(staged_archive, archive_name),
                "manifest": _artifact_record_named(
                    staged_manifest, "bundle-manifest.json"
                ),
            }
            write_json(staged_bundle, bundle)
            for staged, destination in (
                (staged_archive, archive_path),
                (staged_manifest, manifest_path),
                (staged_bundle, bundle_path),
            ):
                _publish_no_clobber(staged, destination)
                metadata = destination.stat(follow_symlinks=False)
                created[destination] = (metadata.st_dev, metadata.st_ino)
            return bundle
    except BaseException:
        for path, identity in created.items():
            _remove_transaction_file(path, identity)
        raise
    finally:
        for item in prepared:
            item.handle.close()
        if root_fd is not None:
            os.close(root_fd)


def _trusted_root(root: str | Path) -> Path:
    lexical = validate_portable_filesystem_path(root)
    if lexical.is_symlink():
        raise InputBoundaryError(f"evidence root must not be a symlink: {lexical}")
    return lexical.resolve()


def _canonical_bundle_name(source: str | Path, root: Path) -> str:
    raw = os.fspath(source)
    lexical = Path(raw)
    if lexical.is_absolute():
        raw_parts = raw.split("/")
        if "\\" in raw or any(part in {"", ".", ".."} for part in raw_parts[1:]):
            raise InputBoundaryError(
                f"evidence path is not a canonical absolute spelling: {raw}"
            )
        try:
            relative = lexical.relative_to(root)
        except ValueError as exc:
            raise InputBoundaryError(f"evidence file is outside the bundle root: {raw}") from exc
        name = relative.as_posix()
    else:
        name = raw
    try:
        return parse_portable_relative_path(name).as_posix()
    except InputBoundaryError as exc:
        raise InputBoundaryError(
            f"evidence path is not a canonical portable relative POSIX name: {raw}"
        ) from exc


def _open_root(root: Path) -> int | None:
    if os.name == "nt":
        return None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.name != "posix" or not nofollow:
        raise InputBoundaryError(
            "explicit evidence paths require kernel no-follow containment"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | nofollow
    )
    try:
        return os.open(root, flags)
    except OSError as exc:
        raise InputBoundaryError(f"cannot open trusted evidence root: {root}") from exc


def _open_relative_descriptor(root_fd: int | None, root: Path, name: str) -> int:
    if os.name == "nt":
        return open_windows_contained_descriptor(root, name)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.name != "posix" or not nofollow or root_fd is None:
        raise InputBoundaryError(
            "explicit evidence paths require kernel no-follow containment"
        )
    parts = PurePosixPath(name).parts
    current = os.dup(root_fd)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec,
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        return os.open(parts[-1], os.O_RDONLY | nofollow | cloexec, dir_fd=current)
    except OSError as exc:
        raise InputBoundaryError(
            f"evidence path traverses a symlink or changed during validation: {name}"
        ) from exc
    finally:
        os.close(current)


def _prepare_file(root_fd: int | None, root: Path, name: str) -> _PreparedFile:
    descriptor = _open_relative_descriptor(root_fd, root, name)
    handle = os.fdopen(descriptor, "rb", closefd=True)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise InputBoundaryError(f"evidence path is not a regular file: {name}")
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
        if size != metadata.st_size:
            raise InputBoundaryError(f"evidence file changed while reading: {name}")
        handle.seek(0)
        return _PreparedFile(
            name=name,
            handle=handle,
            size=size,
            sha256=digest.hexdigest(),
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
    except BaseException:
        handle.close()
        raise


def _verify_prepared_path(
    root_fd: int | None, root: Path, item: _PreparedFile
) -> None:
    descriptor = _open_relative_descriptor(root_fd, root, item.name)
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_dev != item.device
        or metadata.st_ino != item.inode
        or metadata.st_size != item.size
    ):
        raise InputBoundaryError(f"evidence file changed after validation: {item.name}")


def _prepared_record(item: _PreparedFile) -> dict[str, Any]:
    return {"path": item.name, "bytes": item.size, "sha256": item.sha256}


def _validated_regular_entries(root: Path, *, excluded: set[Path]) -> set[Path]:
    files: set[Path] = set()

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                mode = entry.stat(follow_symlinks=False).st_mode
                if path in excluded:
                    if not stat.S_ISREG(mode):
                        raise InputBoundaryError(
                            f"owned bundle path is not a regular file: {path}"
                        )
                    continue
                if stat.S_ISDIR(mode):
                    visit(path)
                elif stat.S_ISREG(mode):
                    files.add(path)
                elif stat.S_ISLNK(mode):
                    raise InputBoundaryError(f"evidence root contains a symlink: {path}")
                else:
                    raise InputBoundaryError(
                        f"evidence root contains a non-regular entry: {path}"
                    )

    visit(root)
    return files


def _publish_no_clobber(staged: Path, destination: Path) -> None:
    try:
        os.link(staged, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise InputBoundaryError(
            f"bundle destination already exists and will not be clobbered: {destination}"
        ) from exc
    except OSError as exc:
        raise InputBoundaryError(f"cannot publish bundle destination: {destination}") from exc


def _remove_transaction_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if (metadata.st_dev, metadata.st_ino) == identity:
        path.unlink()


def _artifact_record_named(path: Path, name: str) -> dict[str, Any]:
    return {"path": name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _write_prepared_zip(
    destination: Path,
    prepared: list[_PreparedFile],
    staged_manifest: Path,
) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        entries: list[tuple[str, BinaryIO]] = [
            (item.name, item.handle) for item in prepared
        ]
        with staged_manifest.open("rb") as manifest_handle:
            entries.append(("bundle-manifest.json", manifest_handle))
            for relative, handle in sorted(entries, key=lambda item: item[0]):
                handle.seek(0)
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o100644 << 16
                with archive.open(info, "w") as output_file:
                    shutil.copyfileobj(handle, output_file, length=1024 * 1024)


def _validate_bundle_source(path: Path, root: Path) -> None:
    """Compatibility validation used by legacy callers."""
    root_fd = _open_root(root)
    prepared: _PreparedFile | None = None
    try:
        prepared = _prepare_file(root_fd, root, _canonical_bundle_name(path, root))
        _verify_prepared_path(root_fd, root, prepared)
    finally:
        if prepared is not None:
            prepared.handle.close()
        if root_fd is not None:
            os.close(root_fd)


def _write_reproducible_zip(
    destination: Path,
    root: Path,
    sources: list[Path],
) -> None:
    """Compatibility wrapper for callers that already own manifest publication."""
    root_fd = _open_root(root)
    prepared: list[_PreparedFile] = []
    temporary = destination.with_name(f".{destination.name}.tmp")
    if (
        destination.exists()
        or destination.is_symlink()
        or temporary.exists()
        or temporary.is_symlink()
    ):
        raise InputBoundaryError("bundle ZIP destination or stage already exists")
    temporary_created = False
    try:
        for source in sources:
            prepared.append(
                _prepare_file(root_fd, root, _canonical_bundle_name(source, root))
            )
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_STORED) as archive:
            temporary_created = True
            for item in sorted(prepared, key=lambda value: value.name):
                item.handle.seek(0)
                info = zipfile.ZipInfo(item.name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o100644 << 16
                with archive.open(info, "w") as output_file:
                    shutil.copyfileobj(item.handle, output_file, length=1024 * 1024)
        _publish_no_clobber(temporary, destination)
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)
        for item in prepared:
            item.handle.close()
        if root_fd is not None:
            os.close(root_fd)
