"""Shared resource and path limits for untrusted ZIP archives."""

from __future__ import annotations

import os
import stat
import unicodedata
import zipfile
from pathlib import Path

from .errors import InputBoundaryError
from .portable_paths import (
    parse_portable_relative_path,
    validate_portable_filesystem_path,
)

MAX_ARCHIVE_MEMBERS = 1024
MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_DECOMPRESSION_RATIO = 200


def validate_zip_archive(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Validate the complete central directory before any member is decompressed."""
    if isinstance(archive.filename, (str, os.PathLike)):
        validate_portable_filesystem_path(archive.filename)
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise InputBoundaryError(f"too many archive members (limit {MAX_ARCHIVE_MEMBERS})")
    records: dict[str, zipfile.ZipInfo] = {}
    normalized_names: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        try:
            parse_portable_relative_path(name)
        except InputBoundaryError as exc:
            raise InputBoundaryError(f"unsafe archive member path: {name!r}") from exc
        normalized = unicodedata.normalize("NFC", name).casefold()
        if name in records or normalized in normalized_names:
            raise InputBoundaryError(f"duplicate archive member alias: {name!r}")
        normalized_names.add(normalized)
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if info.is_dir() or file_type not in {0, stat.S_IFREG}:
            raise InputBoundaryError(f"non-regular archive member: {name!r}")
        if info.flag_bits & 0x1:
            raise InputBoundaryError(
                f"encrypted archive member is not supported: {name!r}"
            )
        if info.file_size < 0 or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise InputBoundaryError(
                f"archive member exceeds uncompressed size limit: {name!r}"
            )
        total += info.file_size
        if total > MAX_ARCHIVE_TOTAL_BYTES:
            raise InputBoundaryError("archive aggregate uncompressed size exceeds limit")
        if info.file_size and (
            info.compress_size == 0
            or info.file_size > max(info.compress_size, 1) * MAX_DECOMPRESSION_RATIO
        ):
            raise InputBoundaryError(
                f"archive member exceeds decompression ratio: {name!r}"
            )
        records[name] = info
    return records


def create_deterministic_zip(source_directory: str | Path, output: str | Path) -> None:
    """Archive a bounded regular-file tree with stable metadata and member order."""
    source = validate_portable_filesystem_path(source_directory)
    destination = validate_portable_filesystem_path(output)
    if not source.is_dir() or source.is_symlink():
        raise InputBoundaryError("archive source must be a regular directory")
    if destination.exists() or destination.is_symlink():
        raise InputBoundaryError("archive output must not already exist")
    files: list[tuple[str, Path]] = []
    total = 0
    for path in source.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise InputBoundaryError(f"archive source contains a non-regular path: {path}")
        if path.is_file():
            name = path.relative_to(source).as_posix()
            parse_portable_relative_path(name)
            size = path.stat().st_size
            if size > MAX_ARCHIVE_MEMBER_BYTES:
                raise InputBoundaryError(f"archive source member exceeds size limit: {name!r}")
            total += size
            if total > MAX_ARCHIVE_TOTAL_BYTES:
                raise InputBoundaryError("archive source exceeds aggregate size limit")
            files.append((name, path))
    if len(files) > MAX_ARCHIVE_MEMBERS:
        raise InputBoundaryError(f"too many archive source members (limit {MAX_ARCHIVE_MEMBERS})")
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, path in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes())


def extract_validated_zip(
    archive_path: str | Path,
    destination_directory: str | Path,
) -> None:
    """Extract a bounded portable regular-file archive into one new directory."""
    source = validate_portable_filesystem_path(archive_path)
    destination = validate_portable_filesystem_path(destination_directory)
    if destination.exists() or destination.is_symlink():
        raise InputBoundaryError("archive extraction destination must not already exist")
    with zipfile.ZipFile(source, "r") as archive:
        records = validate_zip_archive(archive)
        destination.mkdir(parents=True)
        for name, info in sorted(records.items()):
            target = destination.joinpath(*parse_portable_relative_path(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(read_zip_member(archive, info))


def read_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Read one prevalidated member while enforcing its declared size."""
    content = bytearray()
    with archive.open(info, "r") as source:
        while chunk := source.read(min(1024 * 1024, info.file_size + 1 - len(content))):
            content.extend(chunk)
            if len(content) > info.file_size or len(content) > MAX_ARCHIVE_MEMBER_BYTES:
                raise InputBoundaryError(
                    f"archive member expanded beyond declared size: {info.filename!r}"
                )
    if len(content) != info.file_size:
        raise InputBoundaryError(
            f"archive member size differs from central directory: {info.filename!r}"
        )
    return bytes(content)
