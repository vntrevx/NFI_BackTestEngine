"""Deterministic Freqtrade/NFI configuration loading and redaction."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import SpecValidationError

CONFIG_DOCUMENT_VERSION = "1.0.0"
_SECRET_PARTS = ("api_key", "apikey", "key", "password", "secret", "token")


def load_effective_config(source: str | Path) -> dict[str, Any]:
    """Resolve Freqtrade ``add_config_files`` with deterministic deep merging."""
    path = Path(source).resolve()
    config, inputs = _load_config_tree(path, stack=())
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
    path: Path,
    *,
    stack: tuple[Path, ...],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path in stack:
        chain = " -> ".join(str(item) for item in (*stack, path))
        raise SpecValidationError(f"configuration include cycle: {chain}")
    if not path.is_file():
        raise SpecValidationError(f"configuration file does not exist: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        document = json.loads(_strip_trailing_commas(_strip_json_comments(raw)))
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
        included, included_inputs = _load_config_tree(
            (path.parent / include).resolve(),
            stack=(*stack, path),
        )
        merged = _deep_merge(merged, included)
        inputs.extend(included_inputs)
    local = {key: value for key, value in document.items() if key != "add_config_files"}
    merged = _deep_merge(merged, local)
    encoded = raw.encode()
    inputs.append(
        {
            "path": str(path),
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    )
    return merged, inputs


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
