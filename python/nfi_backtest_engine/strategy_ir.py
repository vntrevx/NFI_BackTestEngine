"""Static NFI/Freqtrade strategy inventory and fail-before-run diagnostics."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard

from .canonical import write_json
from .errors import SpecValidationError, StrategyAnalysisError
from .fixture import sha256_file

STRATEGY_IR_VERSION = "1.6.0"
HOT_CALLBACKS = {
    "adjust_trade_position",
    "bot_loop_start",
    "confirm_trade_entry",
    "confirm_trade_exit",
    "custom_roi",
    "custom_entry_price",
    "custom_exit",
    "custom_exit_price",
    "custom_stake_amount",
    "custom_stoploss",
}
STRATEGY_CALLBACKS = HOT_CALLBACKS | {
    "adjust_entry_price",
    "adjust_exit_price",
    "adjust_order_price",
    "bot_start",
    "check_entry_timeout",
    "check_exit_timeout",
    "leverage",
    "order_filled",
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
        assignment = _class_constant_assignment(item)
        if assignment is None:
            continue
        target, expression = assignment
        try:
            value = _safe_static_value(expression, constants)
        except (ArithmeticError, OverflowError, RecursionError, TypeError, ValueError):
            value = _STATIC_UNKNOWN
        if value is _STATIC_UNKNOWN:
            dynamic_constants.append(target.id)
        else:
            constants[target.id] = _json_literal(value)
    record = {
        "name": node.name,
        "bases": [_qualified_name(base) or ast.unparse(base) for base in node.bases],
        "location": _location(node),
        "constants": constants,
        "dynamic_constants": sorted(dynamic_constants),
        "literal_condition_indices": _literal_condition_indices(node),
        "required_timeframes": _required_timeframes(node, constants),
        "methods": methods,
        "hot_callbacks": sorted(method_names & HOT_CALLBACKS),
        "strategy_callbacks": sorted(method_names & STRATEGY_CALLBACKS),
        "vector_methods": sorted(
            method_names
            & {
                "populate_indicators",
                "populate_entry_trend",
                "populate_exit_trend",
            }
        ),
    }
    fingerprint_identity = {
        "name": record["name"],
        "bases": record["bases"],
        "constants": record["constants"],
        "dynamic_constants": record["dynamic_constants"],
        "literal_condition_indices": record["literal_condition_indices"],
        "methods": [
            {
                "name": method["name"],
                "source_sha256": method["source_sha256"],
            }
            for method in methods
        ],
    }
    record["capability_fingerprint"] = hashlib.sha256(
        json.dumps(
            fingerprint_identity,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return record


def _class_constant_assignment(item: ast.stmt) -> tuple[ast.Name, ast.expr] | None:
    """Return one simple class assignment without executing annotations.

    Modern Freqtrade strategies commonly spell configuration as
    ``startup_candle_count: int = 800``. The annotation carries no runtime
    value for this inventory; only the literal right-hand side participates in
    the same bounded evaluator used for unannotated assignments.
    """
    if isinstance(item, ast.Assign):
        if len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
            return item.targets[0], item.value
        return None
    if (
        isinstance(item, ast.AnnAssign)
        and isinstance(item.target, ast.Name)
        and item.value is not None
    ):
        return item.target, item.value
    return None


def _literal_condition_indices(node: ast.ClassDef) -> dict[str, dict[str, list[int]]]:
    """Inventory source branches selected through a literal condition index.

    NFI's large vector methods iterate enabled signal parameters, derive an
    integer such as ``long_entry_condition_index``, and then dispatch through
    independent ``if index == 120`` branches. Mode-tag constants alone do not
    prove that a strategy can emit a tag. Recording the literal branches keeps
    that reachability boundary visible without executing trusted strategy code.
    """
    result: dict[str, dict[str, list[int]]] = {}
    for method in node.body:
        if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        indices: dict[str, set[int]] = {}
        for item in ast.walk(method):
            if not isinstance(item, ast.Compare) or len(item.ops) != 1:
                continue
            if not isinstance(item.ops[0], ast.Eq) or len(item.comparators) != 1:
                continue
            pair = _literal_index_comparison(item.left, item.comparators[0])
            if pair is None:
                pair = _literal_index_comparison(item.comparators[0], item.left)
            if pair is not None:
                name, value = pair
                indices.setdefault(name, set()).add(value)
        if indices:
            result[method.name] = {name: sorted(values) for name, values in sorted(indices.items())}
    return result


def _literal_index_comparison(name_node: ast.AST, value_node: ast.AST) -> tuple[str, int] | None:
    if not isinstance(name_node, ast.Name) or not name_node.id.endswith("_condition_index"):
        return None
    if not isinstance(value_node, ast.Constant) or not _is_static_int(value_node.value):
        return None
    return name_node.id, value_node.value


def _method_record(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[bytes],
) -> dict[str, Any]:
    segment = _node_source_bytes(node, source_lines)
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
        "source_sha256": hashlib.sha256(segment).hexdigest(),
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
                isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
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
            *STRATEGY_CALLBACKS,
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
        return {timeframe for item in value for timeframe in _literal_timeframes(item)}
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


def _safe_static_value(node: ast.AST, constants: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id, _STATIC_UNKNOWN)
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        values = [_safe_static_value(item, constants) for item in node.elts]
        if any(value is _STATIC_UNKNOWN for value in values):
            return _STATIC_UNKNOWN
        if isinstance(node, ast.List):
            return values
        if isinstance(node, ast.Tuple):
            return tuple(values)
        return set(values)
    if isinstance(node, ast.Dict):
        if any(item is None for item in node.keys):
            return _STATIC_UNKNOWN
        keys = [_safe_static_value(item, constants) for item in node.keys if item is not None]
        values = [_safe_static_value(item, constants) for item in node.values]
        if any(value is _STATIC_UNKNOWN for value in (*keys, *values)):
            return _STATIC_UNKNOWN
        return dict(zip(keys, values, strict=True))
    if isinstance(node, ast.UnaryOp):
        value = _safe_static_value(node.operand, constants)
        if value is _STATIC_UNKNOWN:
            return _STATIC_UNKNOWN
        if isinstance(node.op, ast.USub) and _is_static_number(value):
            return -value
        if isinstance(node.op, ast.UAdd) and _is_static_number(value):
            return +value
        if isinstance(node.op, ast.Not):
            return not value
        return _STATIC_UNKNOWN
    if isinstance(node, ast.BinOp):
        left = _safe_static_value(node.left, constants)
        right = _safe_static_value(node.right, constants)
        if left is _STATIC_UNKNOWN or right is _STATIC_UNKNOWN:
            return _STATIC_UNKNOWN
        if isinstance(node.op, ast.Add):
            if _is_static_number(left) and _is_static_number(right):
                return left + right
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            if isinstance(left, list) and isinstance(right, list):
                return [*left, *right]
            if isinstance(left, tuple) and isinstance(right, tuple):
                return (*left, *right)
        if isinstance(node.op, ast.Sub) and _is_static_number(left) and _is_static_number(right):
            return left - right
        if isinstance(node.op, ast.Mult):
            if _is_static_number(left) and _is_static_number(right):
                return left * right
            if (
                isinstance(left, str)
                and isinstance(right, int)
                and not isinstance(right, bool)
                and 0 <= right <= 10_000
            ):
                return left * right
            if (
                isinstance(left, list)
                and isinstance(right, int)
                and not isinstance(right, bool)
                and 0 <= right <= 10_000
            ):
                return left * right
            if (
                isinstance(left, tuple)
                and isinstance(right, int)
                and not isinstance(right, bool)
                and 0 <= right <= 10_000
            ):
                return left * right
            if (
                isinstance(left, int)
                and not isinstance(left, bool)
                and 0 <= left <= 10_000
                and isinstance(right, str)
            ):
                return right * left
            if (
                isinstance(left, int)
                and not isinstance(left, bool)
                and 0 <= left <= 10_000
                and isinstance(right, list)
            ):
                return right * left
            if (
                isinstance(left, int)
                and not isinstance(left, bool)
                and 0 <= left <= 10_000
                and isinstance(right, tuple)
            ):
                return right * left
        if (
            isinstance(node.op, ast.Div)
            and isinstance(left, int | float)
            and not isinstance(left, bool)
            and isinstance(right, int | float)
            and not isinstance(right, bool)
        ):
            return left / right
        return _STATIC_UNKNOWN
    if isinstance(node, ast.IfExp):
        condition = _safe_static_value(node.test, constants)
        if not isinstance(condition, bool):
            return _STATIC_UNKNOWN
        return _safe_static_value(node.body if condition else node.orelse, constants)
    return _STATIC_UNKNOWN


def _is_static_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_static_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000


_STATIC_UNKNOWN = object()


def _iter_nodes_with_function(tree: ast.AST) -> Any:
    stack: list[tuple[ast.AST, str | None]] = [(tree, None)]
    while stack:
        node, function_name = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function_name = node.name
        yield node, function_name
        children = list(ast.iter_child_nodes(node))
        stack.extend((child, function_name) for child in reversed(children))
