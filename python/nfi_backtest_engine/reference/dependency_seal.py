"""Standalone bounded wheel inventory validation and immutable staging."""

from __future__ import annotations

import hashlib
import shutil
import stat
import sys
import unicodedata
import zipfile
from collections.abc import Callable
from pathlib import Path

from nfi_backtest_engine.portable_paths import parse_portable_relative_path

MAX_MEMBERS = 4096
MAX_MEMBER_BYTES = 64 * 1024**2
MAX_INVENTORY_BYTES = 256 * 1024**2
MAX_COMPRESSION_RATIO = 200


def validate_inventory(root: Path, wheels: tuple[tuple[str, str], ...]) -> None:
    expected: dict[str, tuple[int, str]] = {}
    wheel_paths: set[str] = set()
    normalized_members: set[str] = set()
    try:
        for wheel_name, wheel_sha256 in wheels:
            relative_wheel = f".wheels/{wheel_name}"
            wheel = root / relative_wheel
            if wheel.is_symlink() or not wheel.is_file() or _sha256_file(wheel) != wheel_sha256:
                raise ValueError("pinned wheel missing or changed")
            wheel_paths.add(relative_wheel)
            with zipfile.ZipFile(wheel) as archive:
                members = archive.infolist()
                validate_archive_bounds(members)
                for member in members:
                    relative = safe_member(member)
                    if relative is None:
                        continue
                    normalized = unicodedata.normalize("NFC", relative).casefold()
                    if relative in expected or normalized in normalized_members:
                        raise ValueError("duplicate wheel inventory member alias")
                    normalized_members.add(normalized)
                    with archive.open(member) as source:
                        digest, size = _sha256_stream(source)
                    expected[relative] = (size, digest)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("invalid wheel inventory") from exc

    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual != set(expected) | wheel_paths:
        raise ValueError("incomplete wheel inventory")
    for relative, (size, digest) in expected.items():
        target = root / relative
        if target.is_symlink() or not target.is_file() or target.stat().st_size != size:
            raise ValueError("wheel inventory member changed")
        if _sha256_file(target) != digest:
            raise ValueError("wheel inventory member changed")


def copy_and_validate(
    source: Path,
    target: Path,
    wheels: tuple[tuple[str, str], ...],
    *,
    after_copy: Callable[[], None] | None = None,
) -> None:
    """Copy into private scratch, then validate only the immutable copy."""
    if target.exists():
        raise ValueError("dependency staging target already exists")
    shutil.copytree(source, target, symlinks=True)
    if after_copy is not None:
        after_copy()
    validate_inventory(target, wheels)


def validate_archive_bounds(members: list[zipfile.ZipInfo]) -> None:
    if len(members) > MAX_MEMBERS:
        raise ValueError("wheel inventory has too many members")
    total = 0
    for member in members:
        if member.file_size > MAX_MEMBER_BYTES:
            raise ValueError("wheel inventory member exceeds the size limit")
        total += member.file_size
        if total > MAX_INVENTORY_BYTES:
            raise ValueError("wheel inventory exceeds the aggregate size limit")
        if (
            member.file_size > 1024**2
            and member.file_size > max(member.compress_size, 1) * MAX_COMPRESSION_RATIO
        ):
            raise ValueError("wheel inventory member has an unsafe compression ratio")


def safe_member(member: zipfile.ZipInfo) -> str | None:
    raw = member.filename[:-1] if member.is_dir() else member.filename
    try:
        path = parse_portable_relative_path(raw)
    except ValueError as exc:
        raise ValueError("unsafe wheel inventory path") from exc
    if stat.S_ISLNK(member.external_attr >> 16):
        raise ValueError("wheel inventory contains a symbolic link")
    return None if member.is_dir() else path.as_posix()


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_stream(handle)[0]


def _sha256_stream(handle) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4 or len(args[2:]) % 2:
        raise SystemExit("usage: dependency_seal.py SOURCE TARGET WHEEL SHA256 [WHEEL SHA256 ...]")
    wheels = tuple(zip(args[2::2], args[3::2], strict=True))
    copy_and_validate(Path(args[0]), Path(args[1]), wheels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
