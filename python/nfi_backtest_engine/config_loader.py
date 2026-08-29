"""Deterministic Freqtrade/NFI configuration loading and redaction."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import InputBoundaryError, SpecValidationError
from .portable_paths import (
    open_secure_directory,
    parse_portable_relative_path,
    validate_portable_filesystem_path,
)
from .windows_path_security import open_windows_contained_descriptor

CONFIG_DOCUMENT_VERSION = "1.0.0"
MAX_CONFIG_FILES = 64
MAX_CONFIG_DEPTH = 16
MAX_CONFIG_FILE_BYTES = 4 * 1024 * 1024
MAX_CONFIG_TOTAL_BYTES = 16 * 1024 * 1024
MAX_CONFIG_JSON_DEPTH = 100
_SECRET_PARTS = ("api_key", "apikey", "key", "password", "secret", "token")


def load_effective_config(source: str | Path) -> dict[str, Any]:
    """Resolve Freqtrade ``add_config_files`` with deterministic deep merging."""
    try:
        path = validate_portable_filesystem_path(source)
    except InputBoundaryError as exc:
        raise SpecValidationError(f"configuration source path is not portable: {source}") from exc
    root = path.parent
    root_fd = _open_config_root(root)
    try:
        config, inputs = _load_config_tree(
            path.name,
            root=root,
            root_fd=root_fd,
            stack=(),
            state={"files": 0, "bytes": 0},
        )
    finally:
        if root_fd is not None:
            os.close(root_fd)
    exchange = config.get("exchange")
    if not isinstance(exchange, dict) or not isinstance(exchange.get("name"), str):
        raise SpecValidationError("effective config requires exchange.name")
    return {
        "schema_version": CONFIG_DOCUMENT_VERSION,
        "root_path": str(path),
        "inputs": inputs,
        "config": config,
        "redacted_config": redact_config(config),
        "sha256": config_sha256(config),
    }


def config_sha256(config: dict[str, Any]) -> str:
    encoded = json.dumps(
        config,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def redact_config(value: Any, *, key: str | None = None) -> Any:
    """Return a JSON-safe copy with credential-shaped fields removed."""
    if key is not None and _is_secret_key(key):
        return "<redacted>" if value not in (None, "") else value
    if isinstance(value, dict):
        return {
            str(item_key): redact_config(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return deepcopy(value)


def sanitize_config(value: Any, *, key: str | None = None) -> Any:
    """Return a JSON-safe runtime copy with all credential values blanked."""
    if key is not None and _is_secret_key(key):
        return None if value is None else ""
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_config(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_config(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return deepcopy(value)


def strip_service_only_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Return a runtime copy without services unrelated to offline commands.

    Freqtrade validates API-server secret lengths even when a read-only command
    such as ``download-data`` or ``list-pairs`` never starts that server. The
    source config remains hash-bound; only the disposable offline copy drops the
    service section.
    """
    result = deepcopy(config)
    result.pop("api_server", None)
    return result


def freeze_pairlist(
    effective_config: dict[str, Any],
    *,
    resolved_pairs: list[str] | None = None,
) -> dict[str, Any]:
    """Freeze one stable, ordered pairlist for engine and reference runs."""
    exchange = effective_config.get("exchange")
    if not isinstance(exchange, dict):
        raise SpecValidationError("effective config exchange must be an object")
    configured = exchange.get("pair_whitelist")
    pairs = resolved_pairs if resolved_pairs is not None else configured
    if not isinstance(pairs, list) or not pairs:
        raise SpecValidationError(
            "pairlist cannot be resolved from exchange.pair_whitelist; "
            "resolve dynamic pairlists before freezing"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for index, pair in enumerate(pairs):
        if not isinstance(pair, str) or "/" not in pair or pair.strip() != pair:
            raise SpecValidationError(f"pairlist item {index} is not a canonical CCXT pair")
        if pair in seen:
            raise SpecValidationError(f"pairlist contains duplicate pair: {pair}")
        seen.add(pair)
        normalized.append(pair)
    identity = {
        "exchange": exchange.get("name"),
        "trading_mode": effective_config.get("trading_mode", "spot"),
        "margin_mode": effective_config.get("margin_mode", ""),
        "pairs": normalized,
    }
    return {
        "schema_version": "1.0.0",
        **identity,
        "sha256": config_sha256(identity),
    }


def _load_config_tree(
    name: str,
    *,
    root: Path,
    root_fd: int | None,
    stack: tuple[str, ...],
    state: dict[str, int],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = root / name
    if len(stack) >= MAX_CONFIG_DEPTH:
        raise SpecValidationError(f"configuration include depth exceeds {MAX_CONFIG_DEPTH}")
    if name in stack:
        chain = " -> ".join((*stack, name))
        raise SpecValidationError(f"configuration include cycle: {chain}")
    descriptor = _open_config_descriptor(root_fd, root, name)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SpecValidationError(f"configuration path is not a regular file: {path}")
        if metadata.st_size > MAX_CONFIG_FILE_BYTES:
            raise SpecValidationError(f"configuration file exceeds byte limit: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            encoded = handle.read(MAX_CONFIG_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(encoded) > MAX_CONFIG_FILE_BYTES:
        raise SpecValidationError(f"configuration file exceeds byte limit: {path}")
    state["files"] += 1
    state["bytes"] += len(encoded)
    if state["files"] > MAX_CONFIG_FILES:
        raise SpecValidationError("configuration include count exceeds limit")
    if state["bytes"] > MAX_CONFIG_TOTAL_BYTES:
        raise SpecValidationError("configuration aggregate bytes exceed limit")
    _validate_config_json_depth(encoded)
    try:
        raw = encoded.decode("utf-8")
        document = json.loads(_strip_trailing_commas(_strip_json_comments(raw)))
    except UnicodeDecodeError as exc:
        raise SpecValidationError(f"configuration is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SpecValidationError(
            f"invalid JSON configuration {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(document, dict):
        raise SpecValidationError(f"configuration root must be an object: {path}")
    merged: dict[str, Any] = {}
    inputs: list[dict[str, Any]] = []
    includes = document.get("add_config_files", [])
    if not isinstance(includes, list) or not all(isinstance(item, str) for item in includes):
        raise SpecValidationError(f"add_config_files must be a list of paths: {path}")
    for include in includes:
        relative = _canonical_config_include(include)
        candidate = (PurePosixPath(name).parent / relative).as_posix()
        included, included_inputs = _load_config_tree(
            candidate,
            root=root,
            root_fd=root_fd,
            stack=(*stack, name),
            state=state,
        )
        merged = _deep_merge(merged, included)
        inputs.extend(included_inputs)
    local = {key: value for key, value in document.items() if key != "add_config_files"}
    merged = _deep_merge(merged, local)
    inputs.append(
        {
            "path": str(path),
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    )
    return merged, inputs


def _open_config_root(root: Path) -> int | None:
    try:
        return open_secure_directory(root)
    except InputBoundaryError as exc:
        raise SpecValidationError(
            f"cannot open trusted configuration root with no-follow containment: {root}"
        ) from exc


def _open_config_descriptor(root_fd: int | None, root: Path, name: str) -> int:
    if os.name == "nt":
        return open_windows_contained_descriptor(root, name)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.name != "posix" or not nofollow or root_fd is None:
        raise InputBoundaryError("configuration loading requires kernel no-follow containment")
    current = os.dup(root_fd)
    try:
        parts = PurePosixPath(name).parts
        for component in parts[:-1]:
            _config_open_checkpoint(name, component)
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | nofollow,
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        _config_open_checkpoint(name, parts[-1])
        return os.open(
            parts[-1],
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
            dir_fd=current,
        )
    except OSError as exc:
        raise InputBoundaryError(
            f"configuration path traverses a symlink or changed during containment: {name}"
        ) from exc
    finally:
        os.close(current)


def _canonical_config_include(include: str) -> PurePosixPath:
    try:
        return parse_portable_relative_path(include)
    except InputBoundaryError as exc:
        raise SpecValidationError(
            f"configuration include path must be a canonical portable child: {include}"
        ) from exc


def _validate_config_json_depth(payload: bytes) -> None:
    depth = 0
    index = 0
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False
    while index < len(payload):
        byte = payload[index]
        following = payload[index + 1] if index + 1 < len(payload) else None
        if line_comment:
            line_comment = byte not in {0x0A, 0x0D}
        elif block_comment:
            if byte == 0x2A and following == 0x2F:
                block_comment = False
                index += 1
        elif in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte == 0x2F and following == 0x2F:
            line_comment = True
            index += 1
        elif byte == 0x2F and following == 0x2A:
            block_comment = True
            index += 1
        elif byte in {0x5B, 0x7B}:
            depth += 1
            if depth > MAX_CONFIG_JSON_DEPTH:
                raise InputBoundaryError(
                    f"configuration JSON nesting limit exceeded ({MAX_CONFIG_JSON_DEPTH})"
                )
        elif byte in {0x5D, 0x7D}:
            depth -= 1
        index += 1


def _config_open_checkpoint(_name: str, _component: str) -> None:
    return


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _strip_json_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
        elif char == "/" and next_char == "/":
            while index < len(text) and text[index] not in "\r\n":
                index += 1
        elif char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                output.append("\n" if text[index] == "\n" else " ")
                index += 1
            if index + 1 >= len(text):
                raise SpecValidationError("unterminated block comment in configuration")
            index += 2
        else:
            output.append(char)
            index += 1
    return "".join(output)


def _strip_trailing_commas(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SECRET_PARTS or any(
        normalized.endswith(f"_{part}") for part in _SECRET_PARTS
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    missing = object()
    enum_value = getattr(value, "value", missing)
    if enum_value is None or isinstance(enum_value, str | int | float | bool):
        return enum_value
    raise TypeError(f"configuration value is not JSON serializable: {type(value).__name__}")
