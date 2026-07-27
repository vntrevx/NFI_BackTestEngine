"""Hash-bound source, method, and analysis identities."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

from . import STRATEGY_IR_VERSION


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _analysis_document(
    path: Path,
    text: str,
    imports: list[str],
    strategies: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": STRATEGY_IR_VERSION,
        "source": {
            "path": str(path),
            "bytes": len(text.encode("utf-8")),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        },
        "imports": imports,
        "strategies": strategies,
        "diagnostics": diagnostics,
        "static_safe": not any(item["severity"] == "error" for item in diagnostics),
    }


def _method_record(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[bytes],
) -> dict[str, Any]:
    segment = _node_source_bytes(node, source_lines)
    # Method identities protect handwritten lowering against behavior changes,
    # but a Windows checkout may convert LF to CRLF without changing Python
    # semantics. Keep the whole-file bundle hash byte exact while making this
    # narrower callback identity independent of the checkout platform.
    normalized_segment = segment.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    calls = sorted(
        {
            name
            for item in ast.walk(node)
            if isinstance(item, ast.Call)
            if (name := _qualified_name(item.func)) is not None
        }
    )
    return {
        "name": node.name,
        "location": _location(node),
        "source_sha256": hashlib.sha256(normalized_segment).hexdigest(),
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "parameters": [argument.arg for argument in node.args.args],
        "node_count": sum(1 for _ in ast.walk(node)),
        "calls": calls,
        "control_flow": {
            "branches": sum(isinstance(item, ast.If | ast.IfExp) for item in ast.walk(node)),
            "loops": sum(isinstance(item, ast.For | ast.While) for item in ast.walk(node)),
            "comprehensions": sum(
                isinstance(item, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp)
                for item in ast.walk(node)
            ),
        },
    }

def _location(node: ast.AST) -> dict[str, int]:
    return {
        "line": getattr(node, "lineno", 1),
        "column": getattr(node, "col_offset", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "end_column": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    }

def _node_source_bytes(node: ast.AST, source_lines: list[bytes]) -> bytes:
    start_line = getattr(node, "lineno", 1) - 1
    end_line = getattr(node, "end_lineno", getattr(node, "lineno", 1)) - 1
    start_column = getattr(node, "col_offset", 0)
    end_column = getattr(node, "end_col_offset", len(source_lines[end_line]))
    if start_line == end_line:
        return source_lines[start_line][start_column:end_column]
    chunks = [source_lines[start_line][start_column:]]
    chunks.extend(source_lines[start_line + 1 : end_line])
    chunks.append(source_lines[end_line][:end_column])
    return b"".join(chunks)
