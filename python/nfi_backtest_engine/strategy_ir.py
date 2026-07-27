"""Static NFI/Freqtrade strategy inventory and fail-before-run diagnostics."""

from __future__ import annotations

import ast
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import write_json
from .errors import SpecValidationError, StrategyAnalysisError
from .fixture import sha256_file
from .strategy import (
    HOT_CALLBACKS as HOT_CALLBACKS,
)
from .strategy import (
    STRATEGY_CALLBACKS as STRATEGY_CALLBACKS,
)
from .strategy import (
    STRATEGY_IR_VERSION as STRATEGY_IR_VERSION,
)
from .strategy.diagnostics import _DiagnosticVisitor, _document_diagnostic
from .strategy.identity import _analysis_document
from .strategy.inventory import _imports, _is_strategy_class, _strategy_record

# The v1.1 regression contract freezes both these public diagnostic codes and
# their original compatibility-module location. Diagnostics are emitted from
# strategy.diagnostics; this inventory keeps the versioned source contract
# verifiable without duplicating diagnostic behavior.
_STABLE_ERROR_CODES = (
    "PYTHON_SYNTAX",
    "STRATEGY_CLASS_NOT_FOUND",
    "STRATEGY_CLASS_AMBIGUOUS",
    "DYNAMIC_IMPORT",
    "STAR_IMPORT",
    "DYNAMIC_EXECUTION",
    "LOOKAHEAD_NEGATIVE_SHIFT",
    "LOOKAHEAD_CENTERED_WINDOW",
    "DYNAMIC_ATTRIBUTE",
    "DYNAMIC_ATTRIBUTE_INIT",
)


def analyze_strategy(
    source: str | Path,
    *,
    class_name: str | None = None,
) -> dict[str, Any]:
    path = Path(source).resolve()
    if not path.is_file():
        raise StrategyAnalysisError(f"strategy source does not exist: {path}")
    try:
        # Decode bytes directly instead of using universal-newline text I/O.
        # The bundle identity is the exact file supplied by the user, so CRLF
        # sources on Windows must keep the same hash after analysis and copy.
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrategyAnalysisError(f"strategy source is not UTF-8: {path}") from exc
    try:
        tree = ast.parse(text, filename=str(path), type_comments=True)
    except SyntaxError as exc:
        diagnostic = {
            "severity": "error",
            "code": "PYTHON_SYNTAX",
            "message": exc.msg,
            "location": {
                "path": str(path),
                "line": exc.lineno or 1,
                "column": (exc.offset or 1) - 1,
                "end_line": exc.end_lineno or exc.lineno or 1,
                "end_column": (exc.end_offset or exc.offset or 1) - 1,
            },
        }
        return _analysis_document(path, text, [], [], [diagnostic])

    source_lines = [line.encode("utf-8") for line in text.splitlines(keepends=True)]
    imports = _imports(tree)
    diagnostics = _DiagnosticVisitor(path).scan(tree)
    strategies = [
        _strategy_record(node, source_lines)
        for node in tree.body
        if isinstance(node, ast.ClassDef) and _is_strategy_class(node)
    ]
    if class_name is not None:
        strategies = [strategy for strategy in strategies if strategy["name"] == class_name]
        if not strategies:
            diagnostics.append(
                _document_diagnostic(
                    path,
                    "STRATEGY_CLASS_NOT_FOUND",
                    f"strategy class {class_name!r} was not found",
                )
            )
    elif len(strategies) > 1:
        diagnostics.append(
            _document_diagnostic(
                path,
                "STRATEGY_CLASS_AMBIGUOUS",
                "multiple strategy classes found; select one explicitly",
            )
        )
    elif not strategies:
        diagnostics.append(
            _document_diagnostic(
                path,
                "STRATEGY_CLASS_NOT_FOUND",
                "no class derived from IStrategy was found",
            )
        )
    diagnostics.sort(
        key=lambda item: (
            item["location"]["line"],
            item["location"]["column"],
            item["code"],
        )
    )
    return _analysis_document(path, text, imports, strategies, diagnostics)


def prepare_strategy(
    source: str | Path,
    destination: str | Path,
    *,
    class_name: str | None = None,
) -> dict[str, Any]:
    """Copy a static-safe source and its immutable analysis into a fresh bundle."""
    analysis = analyze_strategy(source, class_name=class_name)
    errors = [item for item in analysis["diagnostics"] if item["severity"] == "error"]
    if errors:
        first = errors[0]
        location = first["location"]
        raise StrategyAnalysisError(
            f"{location['path']}:{location['line']}:{location['column']}: "
            f"{first['code']}: {first['message']}"
        )
    root = Path(destination).resolve()
    if root.exists() and any(root.iterdir()):
        raise StrategyAnalysisError(f"strategy bundle destination must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    strategy_path = root / "strategy.py"
    shutil.copyfile(Path(source).resolve(), strategy_path)
    write_json(root / "strategy-ir.json", analysis)
    manifest = {
        "schema_version": "1.1.0",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "strategy": {
            "path": "strategy.py",
            "bytes": strategy_path.stat().st_size,
            "sha256": sha256_file(strategy_path),
        },
        "ir": {
            "path": "strategy-ir.json",
            "bytes": (root / "strategy-ir.json").stat().st_size,
            "sha256": sha256_file(root / "strategy-ir.json"),
        },
        "selected_class": analysis["strategies"][0]["name"],
        "hot_callbacks": analysis["strategies"][0]["hot_callbacks"],
        "strategy_callbacks": analysis["strategies"][0]["strategy_callbacks"],
        "execution_boundary": {
            "initialization": "batch-python-freeze-effective-config",
            "vector_methods": "batch-python",
            "strategy_callbacks": "requires-compiled-ir",
            "python_per_candle": False,
        },
    }
    write_json(root / "manifest.json", manifest)
    return manifest


def validate_strategy_bundle(source: str | Path) -> dict[str, Any]:
    root = Path(source).resolve()
    manifest = __import__("json").loads((root / "manifest.json").read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "created_at",
        "strategy",
        "ir",
        "selected_class",
        "hot_callbacks",
        "strategy_callbacks",
        "execution_boundary",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise SpecValidationError("strategy bundle manifest fields differ from v1")
    for key in ("strategy", "ir"):
        record = manifest[key]
        target = (root / record["path"]).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise SpecValidationError(f"strategy bundle {key} path is invalid")
        if target.stat().st_size != record["bytes"] or sha256_file(target) != record["sha256"]:
            raise SpecValidationError(f"strategy bundle {key} bytes changed")
    analysis = analyze_strategy(
        root / manifest["strategy"]["path"],
        class_name=manifest["selected_class"],
    )
    if analysis["source"]["sha256"] != manifest["strategy"]["sha256"]:
        raise SpecValidationError("strategy bundle analysis source hash differs")
    return manifest
