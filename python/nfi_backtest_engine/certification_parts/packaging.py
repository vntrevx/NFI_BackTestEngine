"""Legacy certification bundle packaging with stable byte layout."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..archive_security import read_zip_member, validate_zip_archive
from ..canonical import write_json
from ..fixture import sha256_file
from ..portable_paths import (
    open_secure_directory,
    parse_portable_relative_path,
    validate_portable_filesystem_path,
)
from ..windows_path_security import (
    open_windows_contained_descriptor,
    windows_root_identity,
)

MAX_CERTIFICATION_SOURCE_BYTES = 256 * 1024 * 1024
MAX_CERTIFICATION_TOTAL_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class _StagedSource:
    path: Path
    name: str
    size: int
    sha256: str


def _certification_checkpoint(_checkpoint: str) -> None:
    return


def _bundle_files(root: Path) -> list[Path]:
    excluded = {
        root / "bundle-manifest.json",
        root / "bundle.json",
        root / "certification-bundle.zip",
    }
    files: list[Path] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                mode = entry.stat(follow_symlinks=False).st_mode
                if path in excluded:
                    if not stat.S_ISREG(mode):
                        raise ValueError(
                            f"owned certification path is not a regular file: {path}"
                        )
                    continue
                if stat.S_ISDIR(mode):
                    visit(path)
                elif stat.S_ISREG(mode):
                    files.append(path)
                else:
                    raise ValueError(
                        f"certification root contains a non-regular entry: {path}"
                    )

    visit(root)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _write_reproducible_zip(destination: Path, root: Path, sources: list[Path]) -> None:
    for source in sources:
        _validate_source(source, root)
    temporary = destination.with_name(f".{destination.name}.tmp")
    if (
        destination.exists()
        or destination.is_symlink()
        or temporary.exists()
        or temporary.is_symlink()
    ):
        raise ValueError("certification ZIP destination or stage already exists")
    temporary_created = False
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_STORED) as archive:
            temporary_created = True
            for source in sorted(sources, key=lambda path: path.relative_to(root).as_posix()):
                relative = source.relative_to(root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o100644 << 16
                with source.open("rb") as input_file, archive.open(info, "w") as output_file:
                    shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        _publish_no_clobber(temporary, destination)
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)


def _write_certification_publication(
    root: Path,
    sources: list[Path],
    *,
    fixture_id: str,
    release_certified: bool,
) -> dict[str, Any]:
    """Stage manifest, archive, and bundle record as one owned transaction."""
    root = validate_portable_filesystem_path(root)
    manifest_path = root / "bundle-manifest.json"
    archive_path = root / "certification-bundle.zip"
    bundle_path = root / "bundle.json"
    for source in sources:
        _validate_source(source, root)
    published = (archive_path, manifest_path, bundle_path)
    for destination in published:
        if destination.exists() or destination.is_symlink():
            raise ValueError(
                f"certification destination already exists and will not be clobbered: {destination}"
            )
    created: dict[Path, tuple[int, int]] = {}
    try:
        with tempfile.TemporaryDirectory(prefix=".nfi-certification-", dir=root.parent) as raw:
            stage = Path(raw)
            staged_manifest = stage / manifest_path.name
            staged_archive = stage / archive_path.name
            staged_bundle = stage / bundle_path.name
            snapshot_root = stage / "sources"
            snapshot_root.mkdir()
            root_fd = open_secure_directory(root)
            windows_identity = windows_root_identity(root) if os.name == "nt" else None
            staged_sources: list[_StagedSource] = []
            staged_total = 0
            try:
                for source in sources:
                    staged = _snapshot_source(
                        source,
                        root=root,
                        root_fd=root_fd,
                        windows_identity=windows_identity,
                        snapshot_root=snapshot_root,
                        max_bytes=min(
                            MAX_CERTIFICATION_SOURCE_BYTES,
                            MAX_CERTIFICATION_TOTAL_BYTES - staged_total,
                        ),
                    )
                    staged_sources.append(staged)
                    staged_total += staged.size
            finally:
                if root_fd is not None:
                    os.close(root_fd)
            _fsync_directory(snapshot_root)
            _certification_checkpoint("after-source-snapshot")
            manifest = {
                "schema_version": "1.0.0",
                "fixture_id": fixture_id,
                "files": [
                    {"path": item.name, "bytes": item.size, "sha256": item.sha256}
                    for item in staged_sources
                ],
            }
            write_json(staged_manifest, manifest)
            _fsync_file(staged_manifest)
            _certification_checkpoint("after-manifest-sync")
            _write_zip_entries(
                staged_archive,
                [
                    *[(item.path, item.name) for item in staged_sources],
                    (staged_manifest, manifest_path.name),
                ],
            )
            _fsync_file(staged_archive)
            _certification_checkpoint("after-zip-sync")
            _verify_staged_archive(staged_archive, manifest, staged_manifest)
            bundle = {
                "schema_version": "1.0.0",
                "fixture_id": fixture_id,
                "release_certified": release_certified,
                "archive": _named_artifact(staged_archive, archive_path.name),
                "manifest": _named_artifact(staged_manifest, manifest_path.name),
            }
            write_json(staged_bundle, bundle)
            _fsync_file(staged_bundle)
            _fsync_directory(stage)
            _certification_checkpoint("before-publish")
            for staged, destination in (
                (staged_archive, archive_path),
                (staged_manifest, manifest_path),
                (staged_bundle, bundle_path),
            ):
                _publish_no_clobber(staged, destination)
                metadata = destination.stat(follow_symlinks=False)
                created[destination] = (metadata.st_dev, metadata.st_ino)
                _certification_checkpoint(f"published-{destination.name}")
            _fsync_directory(root)
            _certification_checkpoint("after-directory-sync")
            return bundle
    except BaseException:
        for path, identity in created.items():
            _remove_transaction_file(path, identity)
        raise


def _snapshot_source(
    source: Path,
    *,
    root: Path,
    root_fd: int | None,
    windows_identity: tuple[str, tuple[int, int, int]] | None,
    snapshot_root: Path,
    max_bytes: int,
) -> _StagedSource:
    name = source.relative_to(root).as_posix()
    portable = parse_portable_relative_path(name)
    destination = snapshot_root.joinpath(*portable.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if root_fd is None:
        source_fd = open_windows_contained_descriptor(
            root,
            name,
            expected_root_identity=windows_identity,
        )
    else:
        source_fd = _open_certification_source(root_fd, portable)
    try:
        metadata = os.fstat(source_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"certification artifact is not regular: {source}")
        if metadata.st_size > max_bytes:
            raise ValueError("certification source exceeds byte limit")
        output_fd = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := os.read(
                source_fd,
                min(1024 * 1024, max_bytes + 1 - size),
            ):
                digest.update(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("certification source exceeds byte limit")
                view = memoryview(chunk)
                while view:
                    written = os.write(output_fd, view)
                    if written <= 0:
                        raise ValueError("certification source snapshot write failed")
                    view = view[written:]
            if size != metadata.st_size or os.fstat(source_fd).st_size != metadata.st_size:
                raise ValueError("certification source changed during snapshot")
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
        return _StagedSource(destination, name, size, digest.hexdigest())
    finally:
        os.close(source_fd)




def _open_certification_source(root_fd: int, relative: PurePosixPath) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    current = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | nofollow | cloexec,
                dir_fd=current,
            )
            os.close(current)
            current = child
        return os.open(
            relative.parts[-1], os.O_RDONLY | nofollow | cloexec, dir_fd=current
        )
    except OSError as exc:
        raise ValueError("certification source traversal changed") from exc
    finally:
        os.close(current)


def _verify_staged_archive(
    archive_path: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> None:
    expected = {
        item["path"]: (item["bytes"], item["sha256"])
        for item in manifest["files"]
    }
    expected[manifest_path.name] = (
        manifest_path.stat().st_size,
        sha256_file(manifest_path),
    )
    with zipfile.ZipFile(archive_path) as archive:
        members = validate_zip_archive(archive)
        if set(members) != set(expected):
            raise ValueError("staged certification archive member set differs")
        for name, (size, digest) in expected.items():
            payload = read_zip_member(archive, members[name])
            actual_digest = hashlib.sha256(payload).hexdigest()
            if len(payload) != size or actual_digest != digest:
                raise ValueError(
                    "staged certification archive bytes differ from manifest: "
                    f"{name!r} expected ({size}, {digest}), "
                    f"observed ({len(payload)}, {actual_digest})"
                )


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        raise ValueError("durable certification publication requires directory fsync")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_no_clobber(staged: Path, destination: Path) -> None:
    try:
        os.link(staged, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise ValueError(
            f"certification destination already exists and will not be clobbered: {destination}"
        ) from exc
    except OSError as exc:
        raise ValueError(
            f"cannot publish certification destination: {destination}"
        ) from exc


def _remove_transaction_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if (metadata.st_dev, metadata.st_ino) == identity:
        path.unlink()


def _write_zip_entries(destination: Path, entries: list[tuple[Path, str]]) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for source, relative in sorted(entries, key=lambda item: item[1]):
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            with source.open("rb") as input_file, archive.open(info, "w") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)


def _named_artifact(path: Path, name: str) -> dict[str, Any]:
    return {"path": name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _validate_source(path: Path, root: Path) -> None:
    if not path.is_relative_to(root):
        raise ValueError(f"certification artifact is outside its root: {path}")
    parse_portable_relative_path(path.relative_to(root).as_posix())
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError(f"certification artifact traverses a symlink: {path}")
        current = current.parent
    if not path.is_file():
        raise ValueError(f"certification artifact is not a regular file: {path}")


def _artifact_record(path: Path, *, relative_to: Path) -> dict[str, Any]:
    _validate_source(path, relative_to)
    return {
        "path": path.relative_to(relative_to).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
