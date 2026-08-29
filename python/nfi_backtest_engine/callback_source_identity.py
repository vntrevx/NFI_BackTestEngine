"""Canonical identity and source locations for callback source IR."""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping

from .callback_contract import JsonObject, JsonValue


def _location(node: ast.AST, path_name: str) -> JsonObject:
    return {
        "path": path_name,
        "line": getattr(node, "lineno", 1),
        "column": getattr(node, "col_offset", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "end_column": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    }


def _append_unique[Item](values: list[Item], value: Item) -> None:
    if value not in values:
        values.append(value)


def _fingerprint(document: Mapping[str, JsonValue]) -> str:
    identity = {
        key: _without_diagnostic_locations(value)
        for key, value in document.items()
        if key not in {"fingerprint", "source"}
    }
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _without_diagnostic_locations(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {
            key: _without_diagnostic_locations(item)
            for key, item in value.items()
            if key != "location"
        }
    if isinstance(value, list):
        return [_without_diagnostic_locations(item) for item in value]
    return value
