"""Static NFI/Freqtrade strategy inventory and fail-before-run diagnostics."""

from __future__ import annotations

import ast
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import write_json
from .errors import SpecValidationError, StrategyAnalysisError
from .fixture import sha256_file

STRATEGY_IR_VERSION = "1.0.0"
HOT_CALLBACKS = {
    "adjust_trade_position",
    "confirm_trade_entry",
    "confirm_trade_exit",
    "custom_entry_price",
    "custom_exit",
    "custom_exit_price",
    "custom_stake_amount",
    "custom_stoploss",
    "leverage",
}
_DYNAMIC_CALLS = {"compile", "eval", "exec", "__import__", "globals", "locals"}
_DYNAMIC_ATTRIBUTE_CALLS = {"setattr", "delattr"}


def analyze_strategy(
    source: str | Path,
    *,
    class_name: str | None = None,
) -> dict[str, Any]:
    path = Path(source).resolve()
    if not path.is_file():
        raise StrategyAnalysisError(f"strategy source does not exist: {path}")
    try:
        text = path.read_text(encoding="utf-8")
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
        "schema_version": "1.0.0",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
        "execution_boundary": {
            "initialization": "batch-python-freeze-effective-config",
            "vector_methods": "batch-python",
            "hot_callbacks": "requires-compiled-ir",
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


def _strategy_record(node: ast.ClassDef, source_lines: list[bytes]) -> dict[str, Any]:
    methods = [
        _method_record(item, source_lines)
        for item in node.body
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    method_names = {method["name"] for method in methods}
    constants: dict[str, Any] = {}
    dynamic_constants: list[str] = []
    for item in node.body:
        if not isinstance(item, ast.Assign) or len(item.targets) != 1:
            continue
        target = item.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            constants[target.id] = _json_literal(ast.literal_eval(item.value))
        except (RecursionError, ValueError, TypeError):
            dynamic_constants.append(target.id)
    return {
        "name": node.name,
        "bases": [_qualified_name(base) or ast.unparse(base) for base in node.bases],
        "location": _location(node),
        "constants": constants,
        "dynamic_constants": sorted(dynamic_constants),
        "required_timeframes": _required_timeframes(node, constants),
        "methods": methods,
        "hot_callbacks": sorted(method_names & HOT_CALLBACKS),
        "vector_methods": sorted(
            method_names
            & {
                "populate_indicators",
                "populate_entry_trend",
                "populate_exit_trend",
            }
        ),
    }


def _method_record(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[bytes],
) -> dict[str, Any]:
    segment = _node_source_bytes(node, source_lines)
    return {
        "name": node.name,
        "location": _location(node),
        "source_sha256": hashlib.sha256(segment).hexdigest(),
        "is_async": isinstance(node, ast.AsyncFunctionDef),
    }


def _required_timeframes(node: ast.ClassDef, constants: dict[str, Any]) -> list[str]:
    values: set[str] = set()
    base = constants.get("timeframe")
    if isinstance(base, str):
        values.add(base)
    for name, value in constants.items():
        if "timeframe" in name.lower():
            values.update(_literal_timeframes(value))
    for item in ast.walk(node):
        if isinstance(item, ast.Call):
            name = _qualified_name(item.func)
            if name and name.split(".")[-1] in {"informative", "merge_informative_pair"}:
                for argument in item.args:
                    if (
                        isinstance(argument, ast.Constant)
                        and isinstance(argument.value, str)
                        and _looks_like_timeframe(argument.value)
                    ):
                        values.add(argument.value)
    return sorted(values, key=_timeframe_sort_key)


class _DiagnosticVisitor:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.diagnostics: list[dict[str, Any]] = []

    def scan(self, tree: ast.AST) -> list[dict[str, Any]]:
        for node, function_name in _iter_nodes_with_function(tree):
            if isinstance(node, ast.Import):
                self._check_import(node)
            elif isinstance(node, ast.ImportFrom):
                self._check_import_from(node)
            elif isinstance(node, ast.Call):
                self._check_call(node, function_name)
        return self.diagnostics

    def _check_import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "importlib" or alias.name.startswith("importlib."):
                self._add(node, "DYNAMIC_IMPORT", "importlib is not allowed in compiled strategies")

    def _check_import_from(self, node: ast.ImportFrom) -> None:
        if node.module == "importlib" or (node.module or "").startswith("importlib."):
            self._add(node, "DYNAMIC_IMPORT", "importlib is not allowed in compiled strategies")
        if any(alias.name == "*" for alias in node.names):
            self._add(node, "STAR_IMPORT", "star imports make strategy dependencies ambiguous")

    def _check_call(self, node: ast.Call, function_name: str | None) -> None:
        name = _qualified_name(node.func)
        leaf = name.split(".")[-1] if name else None
        if leaf in _DYNAMIC_CALLS:
            self._add(node, "DYNAMIC_EXECUTION", f"{leaf}() cannot be compiled exactly")
        elif leaf in _DYNAMIC_ATTRIBUTE_CALLS:
            self._dynamic_attribute(node, function_name, f"{leaf}()")
        elif leaf == "getattr":
            if len(node.args) < 2 or not (
                isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                self._dynamic_attribute(node, function_name, "dynamic getattr()")
        elif leaf == "shift" and node.args and _negative_number(node.args[0]):
            self._add(
                node,
                "LOOKAHEAD_NEGATIVE_SHIFT",
                "negative dataframe shift reads future candles",
            )
        elif leaf == "rolling":
            for keyword in node.keywords:
                if (
                    keyword.arg == "center"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    self._add(
                        node,
                        "LOOKAHEAD_CENTERED_WINDOW",
                        "centered rolling windows read future candles",
                    )

    def _dynamic_attribute(
        self,
        node: ast.AST,
        function_name: str | None,
        operation: str,
    ) -> None:
        compile_time_methods = {
            *HOT_CALLBACKS,
            "populate_indicators",
            "populate_entry_trend",
            "populate_exit_trend",
        }
        if function_name in compile_time_methods:
            self._add(
                node,
                "DYNAMIC_ATTRIBUTE",
                f"{operation} cannot be compiled exactly in {function_name}()",
            )
        else:
            self._add(
                node,
                "DYNAMIC_ATTRIBUTE_INIT",
                f"{operation} requires effective-config freezing during preparation",
                severity="warning",
            )

    def _add(
        self,
        node: ast.AST,
        code: str,
        message: str,
        *,
        severity: str = "error",
    ) -> None:
        self.diagnostics.append(
            {
                "severity": severity,
                "code": code,
                "message": message,
                "location": {"path": str(self.path), **_location(node)},
            }
        )


def _is_strategy_class(node: ast.ClassDef) -> bool:
    return any((_qualified_name(base) or "").split(".")[-1] == "IStrategy" for base in node.bases)


def _imports(tree: ast.Module) -> list[str]:
    result: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return sorted(result)


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _negative_number(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int | float)
        and not isinstance(node.operand.value, bool)
    )


def _location(node: ast.AST) -> dict[str, int]:
    return {
        "line": getattr(node, "lineno", 1),
        "column": getattr(node, "col_offset", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "end_column": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    }


def _document_diagnostic(path: Path, code: str, message: str) -> dict[str, Any]:
    return {
        "severity": "error",
        "code": code,
        "message": message,
        "location": {
            "path": str(path),
            "line": 1,
            "column": 0,
            "end_line": 1,
            "end_column": 0,
        },
    }


def _looks_like_timeframe(value: str) -> bool:
    return len(value) >= 2 and value[:-1].isdigit() and value[-1] in "smhdwM"


def _timeframe_sort_key(value: str) -> tuple[int, str]:
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000}
    return int(value[:-1]) * multipliers[value[-1]], value


def _literal_timeframes(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if _looks_like_timeframe(value) else set()
    if isinstance(value, dict):
        return {
            timeframe
            for key, item in value.items()
            for timeframe in (*_literal_timeframes(key), *_literal_timeframes(item))
        }
    if isinstance(value, list):
        return {
            timeframe
            for item in value
            for timeframe in _literal_timeframes(item)
        }
    return set()


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


def _json_literal(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_literal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_literal(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_literal(item) for item in value), key=repr)
    return repr(value)


def _iter_nodes_with_function(tree: ast.AST) -> Any:
    stack: list[tuple[ast.AST, str | None]] = [(tree, None)]
    while stack:
        node, function_name = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function_name = node.name
        yield node, function_name
        children = list(ast.iter_child_nodes(node))
        stack.extend((child, function_name) for child in reversed(children))
