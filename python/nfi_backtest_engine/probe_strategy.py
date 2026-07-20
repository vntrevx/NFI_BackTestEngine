"""AST-bound strategy transformations for branch-reaching official probes.

Probe fixtures occasionally need to enable a dormant upstream branch or add one
static Freqtrade protection. The transformation must identify syntax nodes, verify
their old values, and change only the selected byte spans. Line-number replacements
would silently target the wrong code after routine upstream edits.
"""

from __future__ import annotations

import ast
import math
import re
from pathlib import Path
from typing import Any

from .errors import SpecValidationError
from .fixture import sha256_file


def prepare_probe_strategy(
    source: str | Path,
    destination: str | Path,
    *,
    class_name: str,
    upstream_repository: str,
    upstream_commit: str,
    boolean_toggles: list[dict[str, Any]] | None = None,
    literal_toggles: list[dict[str, Any]] | None = None,
    protections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write one minimally changed source and return its sealed provenance."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if not source_path.is_file():
        raise SpecValidationError(f"probe strategy source does not exist: {source_path}")
    if destination_path.exists():
        raise SpecValidationError(
            f"probe strategy destination already exists: {destination_path}"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", upstream_commit):
        raise SpecValidationError("probe upstream commit must be a lowercase Git SHA")
    raw = source_path.read_bytes()
    try:
        text = raw.decode("utf-8")
        tree = ast.parse(text, filename=str(source_path))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise SpecValidationError(f"probe strategy is not valid UTF-8 Python: {exc}") from exc
    strategy_class = _one_class(tree, class_name)
    edits: list[tuple[int, int, bytes]] = []
    transformations: list[dict[str, str]] = []

    for toggle in boolean_toggles or []:
        mapping_name = _required_text(toggle, "mapping")
        key = _required_text(toggle, "key")
        expected = toggle.get("expected")
        replacement = toggle.get("replacement")
        if not isinstance(expected, bool) or not isinstance(replacement, bool):
            raise SpecValidationError(
                f"probe toggle {mapping_name}.{key} requires boolean values"
            )
        value_node = _mapping_boolean_node(
            strategy_class,
            mapping_name=mapping_name,
            key=key,
        )
        if value_node.value is not expected:
            raise SpecValidationError(
                f"probe toggle {mapping_name}.{key} expected {expected!r}, "
                f"found {value_node.value!r}"
            )
        start, end = _node_byte_span(raw, value_node)
        edits.append((start, end, str(replacement).encode("ascii")))
        transformations.append(
            {
                "kind": "source-constant-toggle",
                "description": (
                    f"AST-bound {class_name}.{mapping_name}[{key!r}] "
                    f"{expected!r} -> {replacement!r}"
                ),
            }
        )

    for toggle in literal_toggles or []:
        name = _required_text(toggle, "name")
        expected = toggle.get("expected")
        replacement = toggle.get("replacement")
        _validate_numeric_literal(
            expected,
            replacement,
            context=f"probe literal toggle {name}",
        )
        value_node, observed = _class_literal_node(
            strategy_class,
            attribute_name=name,
        )
        if type(observed) is not type(expected) or observed != expected:
            raise SpecValidationError(
                f"probe literal toggle {name} expected {expected!r}, "
                f"found {observed!r}"
            )
        start, end = _node_byte_span(raw, value_node)
        edits.append((start, end, repr(replacement).encode("ascii")))
        transformations.append(
            {
                "kind": "source-constant-toggle",
                "description": (
                    f"AST-bound {class_name}.{name} "
                    f"{expected!r} -> {replacement!r}"
                ),
            }
        )

    if protections is not None:
        _validate_protection_definitions(protections)
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "protections"
            for node in strategy_class.body
        ):
            raise SpecValidationError(
                f"{class_name} already defines protections; refuse probe injection"
            )
        first_statement = strategy_class.body[0]
        insertion = _line_start_byte_offset(raw, first_statement.lineno)
        indent = b" " * first_statement.col_offset
        body_indent = indent + b"  "
        rendered = (
            indent
            + b"@property\n"
            + indent
            + b"def protections(self):\n"
            + body_indent
            + b"return "
            + repr(protections).encode("utf-8")
            + b"\n\n"
        )
        edits.append((insertion, insertion, rendered))
        methods = ", ".join(
            str(definition["method"]) for definition in protections
        )
        transformations.append(
            {
                "kind": "source-constant-toggle",
                "description": (
                    f"AST-bound static protections property injected into "
                    f"{class_name}: {methods}"
                ),
            }
        )

    effective = _apply_non_overlapping_edits(raw, edits)
    try:
        compile(effective, str(destination_path), "exec")
    except SyntaxError as exc:
        raise SpecValidationError(
            f"probe transformation produced invalid Python: {exc}"
        ) from exc
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_bytes(effective)
    return {
        "upstream_repository": upstream_repository,
        "upstream_commit": upstream_commit,
        "base_source_sha256": sha256_file(source_path),
        "effective_source_sha256": sha256_file(destination_path),
        "transformations": transformations
        or [
            {
                "kind": "observer-only",
                "description": "No source mutation; official read-only tracer only",
            }
        ],
    }


def write_probe_config(
    source: str | Path,
    destination: str | Path,
    *,
    overrides: dict[str, Any],
    remove_paths: list[str] | None = None,
) -> dict[str, str]:
    """Apply a deterministic recursive merge to a JSON probe config."""
    from copy import deepcopy

    from .canonical import read_json, write_json

    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if destination_path.exists():
        raise SpecValidationError(
            f"probe config destination already exists: {destination_path}"
        )
    document = read_json(source_path)
    if not isinstance(document, dict):
        raise SpecValidationError("probe config source must be a JSON object")
    merged = _deep_merge(deepcopy(document), overrides)
    removed = []
    for path in remove_paths or []:
        _remove_path(merged, path)
        removed.append(path)
    write_json(destination_path, merged)
    changed_paths = [
        *sorted(_leaf_paths(overrides)),
        *(f"remove:{path}" for path in sorted(removed)),
    ]
    return {
        "kind": "config-override",
        "description": "Deterministic recursive config override: "
        + ", ".join(changed_paths),
    }


def _one_class(tree: ast.Module, class_name: str) -> ast.ClassDef:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if len(matches) != 1:
        raise SpecValidationError(
            f"probe strategy requires exactly one class {class_name!r}"
        )
    return matches[0]


def _mapping_boolean_node(
    strategy_class: ast.ClassDef,
    *,
    mapping_name: str,
    key: str,
) -> ast.Constant:
    mappings: list[ast.Dict] = []
    for node in strategy_class.body:
        mapping = _assigned_literal_mapping(node, mapping_name)
        if mapping is not None:
            mappings.append(mapping)
    if len(mappings) != 1:
        raise SpecValidationError(
            f"probe strategy requires one literal mapping {mapping_name!r}"
        )
    values = [
        value
        for key_node, value in zip(
            mappings[0].keys,
            mappings[0].values,
            strict=True,
        )
        if isinstance(key_node, ast.Constant) and key_node.value == key
    ]
    if (
        len(values) != 1
        or not isinstance(values[0], ast.Constant)
        or not isinstance(values[0].value, bool)
    ):
        raise SpecValidationError(
            f"probe strategy requires one literal boolean {mapping_name}[{key!r}]"
        )
    return values[0]


def _class_literal_node(
    strategy_class: ast.ClassDef,
    *,
    attribute_name: str,
) -> tuple[ast.expr, int | float]:
    """Return one class-level numeric literal without evaluating executable code."""
    values: list[ast.expr] = []
    for node in strategy_class.body:
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == attribute_name
                for target in node.targets
            ):
                values.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == attribute_name
            and node.value is not None
        ):
            values.append(node.value)
    if len(values) != 1:
        raise SpecValidationError(
            f"probe strategy requires one class literal {attribute_name!r}"
        )
    try:
        observed = ast.literal_eval(values[0])
    except (ValueError, TypeError) as exc:
        raise SpecValidationError(
            f"probe strategy {attribute_name!r} is not a literal number"
        ) from exc
    if (
        isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or not math.isfinite(observed)
    ):
        raise SpecValidationError(
            f"probe strategy {attribute_name!r} is not a finite numeric literal"
        )
    return values[0], observed


def _validate_numeric_literal(
    expected: Any,
    replacement: Any,
    *,
    context: str,
) -> None:
    """Keep probe constant edits explicit, finite, and representation-compatible."""
    if (
        isinstance(expected, bool)
        or isinstance(replacement, bool)
        or not isinstance(expected, (int, float))
        or not isinstance(replacement, (int, float))
        or not math.isfinite(expected)
        or not math.isfinite(replacement)
        or type(expected) is not type(replacement)
        or expected == replacement
    ):
        raise SpecValidationError(
            f"{context} requires distinct finite numbers of the same type"
        )


def _assigned_literal_mapping(
    node: ast.stmt,
    mapping_name: str,
) -> ast.Dict | None:
    if isinstance(node, ast.Assign):
        selected = any(
            isinstance(target, ast.Name) and target.id == mapping_name
            for target in node.targets
        )
        return node.value if selected and isinstance(node.value, ast.Dict) else None
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == mapping_name
        and isinstance(node.value, ast.Dict)
    ):
        return node.value
    return None


def _node_byte_span(raw: bytes, node: ast.expr) -> tuple[int, int]:
    if (
        node.end_lineno is None
        or node.end_col_offset is None
    ):
        raise SpecValidationError("Python AST did not expose a complete source span")
    lines = raw.splitlines(keepends=True)
    start = sum(len(line) for line in lines[: node.lineno - 1]) + node.col_offset
    end = (
        sum(len(line) for line in lines[: node.end_lineno - 1])
        + node.end_col_offset
    )
    return start, end


def _line_start_byte_offset(raw: bytes, line_number: int) -> int:
    return sum(len(line) for line in raw.splitlines(keepends=True)[: line_number - 1])


def _apply_non_overlapping_edits(
    raw: bytes,
    edits: list[tuple[int, int, bytes]],
) -> bytes:
    ordered = sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True)
    previous_start = len(raw) + 1
    result = raw
    for start, end, replacement in ordered:
        if start < 0 or end < start or end > len(raw) or end > previous_start:
            raise SpecValidationError("probe strategy transformations overlap")
        result = result[:start] + replacement + result[end:]
        previous_start = start
    return result


def _validate_protection_definitions(definitions: list[dict[str, Any]]) -> None:
    if len(definitions) != 1:
        raise SpecValidationError(
            "branch-attributed protection probes require exactly one method"
        )
    method = definitions[0].get("method") if isinstance(definitions[0], dict) else None
    if method not in {
        "CooldownPeriod",
        "StoplossGuard",
        "MaxDrawdown",
        "LowProfitPairs",
    }:
        raise SpecValidationError(f"unsupported probe protection method: {method!r}")


def _required_text(document: dict[str, Any], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise SpecValidationError(f"probe transformation {field} must be non-empty")
    return value


def _deep_merge(target: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    for key in sorted(overrides):
        value = overrides[key]
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            target[key] = _deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def _leaf_paths(document: dict[str, Any], prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key in sorted(document):
        path = f"{prefix}.{key}" if prefix else key
        value = document[key]
        if isinstance(value, dict):
            paths.extend(_leaf_paths(value, path))
        else:
            paths.append(path)
    return paths


def _remove_path(document: dict[str, Any], dotted_path: str) -> None:
    parts = dotted_path.split(".")
    if (
        not dotted_path
        or any(not part for part in parts)
    ):
        raise SpecValidationError(f"invalid probe config removal path: {dotted_path!r}")
    parent: dict[str, Any] = document
    for part in parts[:-1]:
        value = parent.get(part)
        if not isinstance(value, dict):
            raise SpecValidationError(
                f"probe config removal path does not exist: {dotted_path}"
            )
        parent = value
    if parts[-1] not in parent:
        raise SpecValidationError(
            f"probe config removal path does not exist: {dotted_path}"
        )
    del parent[parts[-1]]
