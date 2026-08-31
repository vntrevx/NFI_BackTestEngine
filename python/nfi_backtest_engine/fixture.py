"""Benchmark fixture manifest validation and sealing."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from .branch_coverage import validate_fixture_coverage
from .canonical import loads_json_bytes, write_json
from .errors import InputBoundaryError, SpecValidationError
from .portable_paths import (
    open_secure_directory,
    parse_portable_relative_path,
    validate_portable_filesystem_path,
)
from .specs import validate_fixture_manifest, validate_trade_surface
from .state_trace import trace_summary_bytes
from .windows_path_security import (
    open_windows_contained_descriptor,
    windows_root_identity,
)

MAX_FIXTURE_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_FIXTURE_FILE_BYTES = 256 * 1024 * 1024


class _RetainedFixtureManifest(dict[str, Any]):
    """Validated fixture document carrying the exact bytes consumed at its boundary."""

    def __init__(
        self,
        document: dict[str, Any],
        manifest_payload: bytes,
        payloads: dict[str, bytes],
        coverage: dict[str, Any] | None,
    ) -> None:
        super().__init__(document)
        self.manifest_payload = manifest_payload
        self.payloads = payloads
        self.coverage = coverage


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixture_file_checkpoint(_checkpoint: str, _name: str) -> None:
    return


def validate_fixture(
    manifest_path: str | Path,
    *,
    verify_hashes: bool = True,
    validate_trace_semantics: bool = True,
) -> dict[str, Any]:
    """Validate schema and consume every fixture byte from contained descriptors."""
    manifest, manifest_payload, payloads, coverage = _validate_fixture_retained(
        manifest_path,
        verify_hashes=verify_hashes,
        validate_trace_semantics=validate_trace_semantics,
    )
    return _RetainedFixtureManifest(manifest, manifest_payload, payloads, coverage)


def validate_fixture_from_directory(
    directory_descriptor: int,
    manifest_path: str,
) -> dict[str, Any]:
    """Validate a relative fixture from an already retained directory descriptor."""
    manifest, manifest_payload, payloads, coverage = _validate_fixture_retained(
        manifest_path,
        directory_descriptor=directory_descriptor,
    )
    return _RetainedFixtureManifest(manifest, manifest_payload, payloads, coverage)


@contextmanager
def materialized_fixture(
    manifest_path: str | Path,
    manifest: dict[str, Any],
) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Materialize only retained fixture bytes into an owner-private consumer stage."""
    retained = manifest
    if not isinstance(retained, _RetainedFixtureManifest):
        retained = validate_fixture(manifest_path)
    if not isinstance(retained, _RetainedFixtureManifest):
        raise SpecValidationError("fixture validation did not retain immutable payloads")
    with tempfile.TemporaryDirectory(prefix="nfi-fixture-snapshot-") as temporary:
        root = Path(temporary).resolve(strict=True)
        manifest_name = validate_portable_filesystem_path(manifest_path).name
        (root / manifest_name).write_bytes(retained.manifest_payload)
        for relative, payload in retained.payloads.items():
            target = root.joinpath(*parse_portable_relative_path(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        yield root / manifest_name, retained


def _validate_fixture_retained(
    manifest_path: str | Path,
    *,
    verify_hashes: bool = True,
    validate_trace_semantics: bool = True,
    directory_descriptor: int | None = None,
) -> tuple[dict[str, Any], bytes, dict[str, bytes], dict[str, Any] | None]:
    """Return validation, exact retained bytes, and the validated coverage report."""
    if directory_descriptor is None:
        try:
            manifest_file = validate_portable_filesystem_path(manifest_path)
        except InputBoundaryError as exc:
            raise SpecValidationError("fixture manifest path is not portable") from exc
        root: Path | None = manifest_file.parent
        root_fd = _open_fixture_root(root)
        windows_identity = windows_root_identity(root) if os.name == "nt" else None
    else:
        try:
            relative_manifest = parse_portable_relative_path(os.fspath(manifest_path))
        except InputBoundaryError as exc:
            raise SpecValidationError("fixture manifest path is not portable") from exc
        if os.name != "posix" or not getattr(os, "O_NOFOLLOW", 0):
            raise SpecValidationError(
                "descriptor-relative fixture validation requires no-follow support"
            )
        manifest_file = Path(relative_manifest.name)
        root = None
        root_fd = _open_fixture_relative_root(
            directory_descriptor,
            relative_manifest.parent.parts,
        )
        windows_identity = None
    try:
        manifest_payload = _read_fixture_bytes(
            root_fd,
            root,
            manifest_file.name,
            expected_size=None,
            max_bytes=MAX_FIXTURE_MANIFEST_BYTES,
            windows_identity=windows_identity,
        )
        manifest = loads_json_bytes(manifest_payload)
        if not isinstance(manifest, dict):
            raise SpecValidationError("fixture manifest must be an object")
        validate_fixture_manifest(manifest)

        references = [*manifest["inputs"], *manifest["artifacts"].values()]
        seen: set[str] = set()
        payloads: dict[str, bytes] = {}
        for reference in references:
            relative = reference["path"]
            if relative in seen:
                raise SpecValidationError(f"duplicate fixture file reference: {relative}")
            seen.add(relative)
            payload = _read_fixture_bytes(
                root_fd,
                root,
                relative,
                expected_size=reference["bytes"],
                max_bytes=MAX_FIXTURE_FILE_BYTES,
                windows_identity=windows_identity,
            )
            if verify_hashes and hashlib.sha256(payload).hexdigest() != reference["sha256"]:
                raise SpecValidationError(f"{relative}: SHA-256 differs from sealed identity")
            payloads[relative] = payload

        surface_name = manifest["artifacts"]["trade_surface"]["path"]
        surface = loads_json_bytes(payloads[surface_name])
        validate_trade_surface(surface)
        if manifest["schema_version"] in {"2.0.0", "3.0.0"} and validate_trace_semantics:
            strategy = _one_input(manifest["inputs"], "strategy")
            config = _one_input(manifest["inputs"], "config")
            expected_input_hash = fixture_input_sha256(manifest["inputs"])
            trace_names = ["state_trace"]
            if "state_projection" in manifest["artifacts"]:
                trace_names.append("state_projection")
            for trace_name in trace_names:
                trace_relative = manifest["artifacts"][trace_name]["path"]
                trace = trace_summary_bytes(payloads[trace_relative], label=trace_relative)
                _validate_trace_binding(
                    trace,
                    trace_name=trace_name,
                    strategy_sha256=strategy["sha256"],
                    config_sha256=config["sha256"],
                    input_sha256=expected_input_hash,
                    trading_mode=manifest["freqtrade"]["trading_mode"],
                )
        coverage: dict[str, Any] | None = None
        if manifest["schema_version"] == "3.0.0" and validate_trace_semantics:
            # The descriptor-bound validation above runs first. Coverage remains a
            # semantic verifier over those same sealed identities.
            coverage = validate_fixture_coverage(
                manifest_file, manifest, retained_payloads=payloads
            )
        return manifest, manifest_payload, payloads, coverage
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _read_fixture_bytes(
    root_fd: int | None,
    root: Path | None,
    relative: str,
    *,
    expected_size: int | None,
    max_bytes: int,
    windows_identity: tuple[str, tuple[int, int, int]] | None,
) -> bytes:
    portable = _portable_fixture_name(relative)
    descriptor = _open_fixture_descriptor(
        root_fd, root, portable, windows_identity=windows_identity
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SpecValidationError(f"fixture path is not a regular file: {relative}")
        if metadata.st_size > max_bytes:
            raise SpecValidationError(f"fixture file exceeds byte limit: {relative}")
        if expected_size is not None and metadata.st_size != expected_size:
            raise SpecValidationError(f"{relative}: byte size differs from sealed identity")
        identity = (metadata.st_dev, metadata.st_ino)
        _fixture_file_checkpoint("after-open", relative)
        limit = metadata.st_size
        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) != limit:
            raise SpecValidationError(f"fixture file changed while reading: {relative}")
        _fixture_file_checkpoint("after-read", relative)
        os.lseek(descriptor, 0, os.SEEK_SET)
        second = _read_exact_descriptor(descriptor, limit)
        if second != bytes(content):
            raise SpecValidationError(f"fixture file bytes changed during validation: {relative}")
        _verify_fixture_root(root_fd, root)
        current = _open_fixture_descriptor(
            root_fd, root, portable, windows_identity=windows_identity
        )
        try:
            current_metadata = os.fstat(current)
            if (current_metadata.st_dev, current_metadata.st_ino) != identity:
                raise SpecValidationError(f"fixture path identity changed: {relative}")
        finally:
            os.close(current)
        return bytes(content)
    finally:
        os.close(descriptor)


def _read_exact_descriptor(descriptor: int, size: int) -> bytes:
    content = bytearray()
    while len(content) < size:
        chunk = os.read(descriptor, min(1024 * 1024, size - len(content)))
        if not chunk:
            break
        content.extend(chunk)
    if len(content) != size or os.read(descriptor, 1):
        raise SpecValidationError("fixture descriptor size changed during validation")
    return bytes(content)


def _portable_fixture_name(relative: str) -> str:
    try:
        return parse_portable_relative_path(relative).as_posix()
    except InputBoundaryError as exc:
        raise SpecValidationError(
            f"fixture path must be a canonical portable relative path: {relative}"
        ) from exc


def _open_fixture_root(root: Path) -> int | None:
    try:
        return open_secure_directory(root)
    except InputBoundaryError as exc:
        raise SpecValidationError("cannot open fixture root securely") from exc


def _verify_fixture_root(root_fd: int | None, root: Path | None) -> None:
    if os.name == "nt" or root_fd is None or root is None:
        return
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        current = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | nofollow,
        )
    except OSError as exc:
        raise SpecValidationError("fixture root identity changed during validation") from exc
    try:
        expected = os.fstat(root_fd)
        actual = os.fstat(current)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise SpecValidationError("fixture root identity changed during validation")
    finally:
        os.close(current)


def _open_fixture_relative_root(
    directory_descriptor: int,
    components: tuple[str, ...],
) -> int:
    current = os.dup(directory_descriptor)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        for component in components:
            next_descriptor = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = next_descriptor
        return current
    except OSError as exc:
        os.close(current)
        raise SpecValidationError(
            "fixture root traverses a symlink or changed during containment"
        ) from exc


def _open_fixture_descriptor(
    root_fd: int | None,
    root: Path | None,
    name: str,
    *,
    windows_identity: tuple[str, tuple[int, int, int]] | None,
) -> int:
    if os.name == "nt":
        if root is None:
            raise SpecValidationError("fixture root path is unavailable on Windows")
        return open_windows_contained_descriptor(
            root, name, expected_root_identity=windows_identity
        )
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.name != "posix" or not nofollow or root_fd is None:
        raise SpecValidationError("fixture containment requires no-follow descriptor support")
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
        raise SpecValidationError(
            f"fixture path traverses a symlink or changed during containment: {name}"
        ) from exc
    finally:
        os.close(current)


def _validate_trace_binding(
    trace: dict[str, Any],
    *,
    trace_name: str,
    strategy_sha256: str,
    config_sha256: str,
    input_sha256: str,
    trading_mode: str,
) -> None:
    if trace["strategy_sha256"] != strategy_sha256:
        raise SpecValidationError(
            f"{trace_name} strategy_sha256 does not match the sealed strategy input"
        )
    if trace["profile_sha256"] != config_sha256:
        raise SpecValidationError(
            f"{trace_name} profile_sha256 does not match the sealed config input"
        )
    if trace["input_sha256"] != input_sha256:
        raise SpecValidationError(
            f"{trace_name} input_sha256 does not match the sealed fixture inputs"
        )
    if trace["trading_mode"] != trading_mode:
        raise SpecValidationError(f"{trace_name} trading_mode does not match the fixture manifest")


def seal_fixture(manifest_path: str | Path) -> dict[str, Any]:
    """Refresh declared byte counts and hashes for trusted repository maintenance."""
    manifest_file = validate_portable_filesystem_path(manifest_path)
    manifest = loads_json_bytes(manifest_file.read_bytes())
    if not isinstance(manifest, dict):
        raise SpecValidationError("fixture manifest must be an object")
    validate_fixture_manifest(manifest)
    root = manifest_file.parent
    for reference in [*manifest["inputs"], *manifest["artifacts"].values()]:
        target = _safe_fixture_path(root, reference["path"])
        if not target.is_file():
            raise SpecValidationError(f"fixture file does not exist: {reference['path']}")
        reference["bytes"] = target.stat().st_size
        reference["sha256"] = sha256_file(target)
    write_json(manifest_file, manifest)
    return validate_fixture(manifest_file)


def _safe_fixture_path(root: Path, relative: str) -> Path:
    candidate = Path(*parse_portable_relative_path(relative).parts)
    lexical = root / candidate
    current = lexical
    while current != root:
        if current.is_symlink():
            raise SpecValidationError(f"fixture path traverses a symlink: {relative}")
        current = current.parent
    target = lexical.resolve()
    if not target.is_relative_to(root):
        raise SpecValidationError(f"fixture path escapes its directory: {relative}")
    return target


def fixture_input_sha256(inputs: list[dict[str, Any]]) -> str:
    """Hash the ordered behavior-affecting input identity."""
    identity = [
        {
            "role": item["role"],
            "path": item["path"],
            "sha256": item["sha256"],
            "bytes": item["bytes"],
        }
        for item in sorted(inputs, key=lambda item: (item["role"], item["path"]))
    ]
    payload = json.dumps(
        identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _one_input(inputs: list[dict[str, Any]], role: str) -> dict[str, Any]:
    matches = [item for item in inputs if item["role"] == role]
    if len(matches) != 1:
        raise SpecValidationError(f"fixture requires exactly one {role!r} input")
    return matches[0]
