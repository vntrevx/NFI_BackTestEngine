"""Canonical host-independent relative path validation."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

from .errors import InputBoundaryError

_RESERVED_BASENAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CLOCK$",
        "CONIN$",
        "CONOUT$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
    }
)


def validate_portable_path_component(component: str) -> None:
    """Reject names that alias devices, streams, or normalized Win32 names."""
    basename = component.split(".", 1)[0].rstrip(" .").upper()
    if (
        not component
        or unicodedata.normalize("NFC", component) != component
        or component in {".", ".."}
        or component.endswith((".", " "))
        or any(character in '<>:"/\\|?*' for character in component)
        or "/" in component
        or "\\" in component
        or basename in _RESERVED_BASENAMES
        or any(ord(character) < 32 for character in component)
    ):
        raise InputBoundaryError(f"path has a non-portable component: {component}")


def parse_portable_relative_path(value: str) -> PurePosixPath:
    """Require one canonical relative POSIX spelling and validate every component."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise InputBoundaryError(f"path is not canonical portable relative POSIX: {value!r}")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    raw_parts = value.split("/")
    if (
        value.startswith("/")
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.root)
        or posix.as_posix() != value
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise InputBoundaryError(f"path is not canonical portable relative POSIX: {value!r}")
    for component in raw_parts:
        validate_portable_path_component(component)
    return posix


def validate_portable_filesystem_path(value: str | Path) -> Path:
    """Validate every non-anchor component and preserve caller spelling checks."""
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise InputBoundaryError(f"filesystem path is not portable: {raw!r}")
    # pathlib normalizes duplicate separators and dot components, so reject them
    # before constructing the returned path whenever the caller supplied text.
    if isinstance(value, str):
        body = raw[1:] if raw.startswith("/") else raw
        if raw.startswith("//") or any(part in {"", ".", ".."} for part in body.split("/")):
            raise InputBoundaryError(f"filesystem path is not canonically spelled: {raw!r}")
    path = Path(raw).absolute()
    anchor = path.anchor
    for component in path.parts:
        if component == anchor:
            continue
        validate_portable_path_component(component)
    return path


def validate_new_output_path(value: str | Path) -> Path:
    """Validate a destination and reject clobbering or linked parent boundaries."""
    path = validate_portable_filesystem_path(value)
    parent_descriptor = open_secure_parent(path)
    try:
        if path.exists() or path.is_symlink():
            raise InputBoundaryError(f"output destination already exists: {path}")
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
    return path


def open_secure_directory(path: Path, *, create: bool = False) -> int | None:
    """Walk an absolute directory from its filesystem anchor without following links."""
    if os.name == "nt":
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            if not current.exists():
                if not create:
                    raise InputBoundaryError(f"trusted directory does not exist: {current}")
                current.mkdir()
            metadata = current.lstat()
            reparse = getattr(metadata, "st_file_attributes", 0) & 0x00000400
            if current.is_symlink() or reparse or not current.is_dir():
                raise InputBoundaryError(
                    f"trusted directory traverses a symlink or reparse point: {current}"
                )
        return None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.name != "posix" or not nofollow:
        raise InputBoundaryError("secure directory traversal requires no-follow support")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | nofollow
    )
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise InputBoundaryError(
            f"trusted directory traverses a symlink or changed during traversal: {path}"
        ) from exc


def open_secure_parent(path: Path, *, create: bool = False) -> int | None:
    """Retain a no-follow handle for a public path's complete parent chain."""
    return open_secure_directory(path.parent, create=create)
