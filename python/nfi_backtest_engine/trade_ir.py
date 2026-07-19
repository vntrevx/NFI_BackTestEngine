"""Compact, fail-closed IR for stateful trade callback dependencies.

Trade-management callbacks are much larger than entry confirmation callbacks.
This module separates their pure scalar decision functions from methods that
touch wallets, orders, dataframe providers, or mutable strategy state.  Only
the former are lowered here; every unsupported statement or expression is
reported with a source location instead of being approximated.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import StrategyAnalysisError

TRADE_IR_VERSION = "1.4.0"
SCALAR_PROGRAM_VERSION = "1.2.0"
_TRADE_ROOTS = ("adjust_trade_position", "custom_exit")


class _UnsupportedTradeIr(Exception):
    def __init__(self, node: ast.AST, message: str) -> None:
        super().__init__(message)
        self.node = node
        self.message = message


@dataclass
class _ExpressionArena:
    records: list[list[Any]]
    _indexes: dict[str, int]

    @classmethod
    def empty(cls) -> _ExpressionArena:
        return cls([], {})

    def add(self, record: list[Any]) -> int:
        key = json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        known = self._indexes.get(key)
        if known is not None:
            return known
        index = len(self.records)
        self.records.append(record)
        self._indexes[key] = index
        return index


class _ScalarCompiler:
    def __init__(
        self,
        node: ast.FunctionDef,
        *,
        constants: dict[str, Any],
        available_methods: set[str],
        ephemeral_writes: set[str] | None = None,
    ) -> None:
        self.node = node
        self.constants = constants
        self.available_methods = available_methods
        self.ephemeral_writes = ephemeral_writes or set()
        self.method_aliases = _method_aliases(node, available_methods)
        self.called_methods: dict[str, ast.Call] = {}
        self.arena = _ExpressionArena.empty()
        self.parameters = {
            argument.arg
            for argument in (
                list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
            )
            if argument.arg != "self"
        }
        if node.args.vararg is not None:
            self.parameters.add(node.args.vararg.arg)
        if node.args.kwarg is not None:
            self.parameters.add(node.args.kwarg.arg)
        self.locals = set(self.parameters)
        for item in ast.walk(node):
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                self.locals.add(item.id)

    def compile(self) -> dict[str, Any]:
        statements = self._statements(self.node.body)
        return {
            "schema_version": SCALAR_PROGRAM_VERSION,
            "opcode": "scalar-decision-program-v1",
            "parameters": [
                argument.arg
                for argument in (
                    list(self.node.args.posonlyargs)
                    + list(self.node.args.args)
                    + list(self.node.args.kwonlyargs)
                )
                if argument.arg != "self"
            ],
            "expressions": self.arena.records,
            "statements": statements,
        }

    def _statements(self, nodes: list[ast.stmt]) -> list[list[Any]]:
        return [self._statement(node) for node in nodes]

    def _statement(self, node: ast.stmt) -> list[Any]:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            return self._assignment(node.targets[0], node.value, node)
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            return self._assignment(node.target, node.value, node)
        if isinstance(node, ast.If):
            return self._if_statement(node)
        if isinstance(node, ast.Return):
            return ["return", self._expression(node.value)]
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return ["pass"]
        if isinstance(node, ast.Pass):
            return ["pass"]
        raise _UnsupportedTradeIr(
            node,
            f"statement {type(node).__name__} is not scalar-pure",
        )

    def _if_statement(self, node: ast.If) -> list[Any]:
        """Flatten Python's nested-AST representation of an ``elif`` chain.

        CPython stores every ``elif`` as the sole ``else`` child of the
        preceding ``if``. Large NFI decision tables therefore exceeded JSON's
        safe nesting limit even though control flow was only one level deep.
        The flat record preserves first-match semantics and keeps serialized
        IR depth independent of the number of thresholds.
        """
        branches: list[list[Any]] = []
        current = node
        while True:
            branches.append(
                [
                    self._expression(current.test),
                    self._statements(current.body),
                ]
            )
            if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
                current = current.orelse[0]
                continue
            otherwise = self._statements(current.orelse)
            break
        if len(branches) == 1:
            return ["if", branches[0][0], branches[0][1], otherwise]
        return ["if-chain", branches, otherwise]

    def _assignment(
        self,
        target: ast.expr,
        value: ast.expr,
        source: ast.AST,
    ) -> list[Any]:
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "self"
            and value.attr in self.available_methods
        ):
            return ["pass"]
        expression = self._expression(value)
        if isinstance(target, ast.Name):
            return ["set", target.id, expression]
        if isinstance(target, ast.Tuple | ast.List):
            names: list[str] = []
            for item in target.elts:
                if not isinstance(item, ast.Name):
                    raise _UnsupportedTradeIr(source, "assignment target is not a local name")
                names.append(item.id)
            return ["unpack", names, expression]
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr in self.ephemeral_writes
        ):
            return ["ephemeral-set", target.attr, expression]
        raise _UnsupportedTradeIr(source, "assignment target is not a local name")

    def _expression(self, node: ast.expr | None) -> int:
        if node is None:
            return self.arena.add(["literal", None])
        if isinstance(node, ast.Constant):
            if isinstance(node.value, complex | bytes):
                raise _UnsupportedTradeIr(node, "literal is not JSON scalar data")
            return self.arena.add(["literal", node.value])
        if isinstance(node, ast.Name):
            if node.id in self.locals:
                return self.arena.add(["variable", node.id])
            raise _UnsupportedTradeIr(node, f"global name {node.id!r} is not frozen")
        if isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in self.constants
            ):
                return self.arena.add(["literal", self.constants[node.attr]])
            return self.arena.add(["attribute", self._expression(node.value), node.attr])
        if isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Slice):
                raise _UnsupportedTradeIr(node, "slice expressions are not scalar-pure")
            return self.arena.add(
                ["index", self._expression(node.value), self._expression(node.slice)]
            )
        if isinstance(node, ast.UnaryOp):
            unary_operations: dict[type[Any], str] = {
                ast.Not: "not",
                ast.USub: "negative",
                ast.UAdd: "positive",
            }
            operation = unary_operations.get(type(node.op))
            if operation is None:
                raise _UnsupportedTradeIr(node, "unary operator is not supported")
            return self.arena.add([operation, self._expression(node.operand)])
        if isinstance(node, ast.BinOp):
            binary_operations: dict[type[Any], str] = {
                ast.Add: "add",
                ast.Sub: "subtract",
                ast.Mult: "multiply",
                ast.Div: "divide",
                ast.FloorDiv: "floor-divide",
                ast.Mod: "modulo",
                ast.Pow: "power",
            }
            operation = binary_operations.get(type(node.op))
            if operation is None:
                raise _UnsupportedTradeIr(node, "binary operator is not supported")
            return self.arena.add(
                [operation, self._expression(node.left), self._expression(node.right)]
            )
        if isinstance(node, ast.BoolOp):
            operation = "and" if isinstance(node.op, ast.And) else "or"
            return self.arena.add([operation, [self._expression(value) for value in node.values]])
        if isinstance(node, ast.Compare):
            operations = []
            for operation, comparator in zip(node.ops, node.comparators, strict=True):
                if isinstance(operation, ast.Is | ast.IsNot) and not (
                    isinstance(comparator, ast.Constant)
                    and (comparator.value is None or isinstance(comparator.value, bool))
                ):
                    raise _UnsupportedTradeIr(
                        node,
                        "identity comparison is only exact for None and booleans",
                    )
                comparison_operations: dict[type[Any], str] = {
                    ast.Eq: "equal",
                    ast.NotEq: "not-equal",
                    ast.Lt: "less",
                    ast.LtE: "less-equal",
                    ast.Gt: "greater",
                    ast.GtE: "greater-equal",
                    ast.In: "in",
                    ast.NotIn: "not-in",
                    ast.Is: "is",
                    ast.IsNot: "is-not",
                }
                opcode = comparison_operations.get(type(operation))
                if opcode is None:
                    raise _UnsupportedTradeIr(node, "comparison operator is not supported")
                operations.append([opcode, self._expression(comparator)])
            return self.arena.add(["compare", self._expression(node.left), operations])
        if isinstance(node, ast.IfExp):
            return self.arena.add(
                [
                    "if-expression",
                    self._expression(node.test),
                    self._expression(node.body),
                    self._expression(node.orelse),
                ]
            )
        if isinstance(node, ast.Tuple):
            return self.arena.add(["tuple", [self._expression(item) for item in node.elts]])
        if isinstance(node, ast.List):
            return self.arena.add(["list", [self._expression(item) for item in node.elts]])
        if isinstance(node, ast.Set):
            return self.arena.add(["set-literal", [self._expression(item) for item in node.elts]])
        if isinstance(node, ast.Dict):
            if any(key is None for key in node.keys):
                raise _UnsupportedTradeIr(node, "dictionary unpacking is not supported")
            return self.arena.add(
                [
                    "dict",
                    [
                        [self._expression(key), self._expression(value)]
                        for key, value in zip(node.keys, node.values, strict=True)
                        if key is not None
                    ],
                ]
            )
        if isinstance(node, ast.JoinedStr):
            parts: list[list[Any]] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(["text", value.value])
                elif (
                    isinstance(value, ast.FormattedValue)
                    and value.conversion == -1
                    and value.format_spec is None
                ):
                    parts.append(["value", self._expression(value.value)])
                else:
                    raise _UnsupportedTradeIr(node, "formatted string spec is not supported")
            return self.arena.add(["format", parts])
        if isinstance(node, ast.Call):
            return self._call(node)
        raise _UnsupportedTradeIr(
            node,
            f"expression {type(node).__name__} is not scalar-pure",
        )

    def _call(self, node: ast.Call) -> int:
        method_name = _called_method_name(
            node.func,
            self.method_aliases,
            self.available_methods,
        )
        if method_name is not None:
            if node.keywords or any(isinstance(argument, ast.Starred) for argument in node.args):
                raise _UnsupportedTradeIr(
                    node,
                    "compiled method calls require positional scalar arguments",
                )
            self.called_methods.setdefault(method_name, node)
            return self.arena.add(
                [
                    "call-program",
                    method_name,
                    [self._expression(argument) for argument in node.args],
                ]
            )
        if node.keywords:
            raise _UnsupportedTradeIr(node, "keyword call arguments are not scalar-pure")
        if isinstance(node.func, ast.Name) and node.func.id == "isinstance":
            if len(node.args) != 2:
                raise _UnsupportedTradeIr(node, "isinstance arity differs")
            type_name = _type_name(node.args[1])
            if type_name not in {"bool", "float", "int", "np.float64", "str"}:
                raise _UnsupportedTradeIr(node, "isinstance type is not frozen")
            return self.arena.add(["is-instance", self._expression(node.args[0]), type_name])
        if isinstance(node.func, ast.Name) and node.func.id == "len" and len(node.args) == 1:
            return self.arena.add(["length", self._expression(node.args[0])])
        raise _UnsupportedTradeIr(node, f"call {_call_name(node.func)!r} is not scalar-pure")


def build_trade_dependency_ir(
    analysis: dict[str, Any],
    *,
    roots: tuple[str, ...] = _TRADE_ROOTS,
) -> dict[str, Any]:
    """Compile pure decision dependencies and inventory all stateful blockers."""
    strategies = analysis.get("strategies")
    source = analysis.get("source")
    if not isinstance(strategies, list) or len(strategies) != 1:
        raise StrategyAnalysisError("trade dependency IR requires one selected strategy")
    if not isinstance(source, dict):
        raise StrategyAnalysisError("trade dependency IR requires a hash-bound source")
    source_path = source.get("path")
    source_sha256 = source.get("sha256")
    if not isinstance(source_path, str) or not isinstance(source_sha256, str):
        raise StrategyAnalysisError("trade dependency source identity is invalid")
    path = Path(source_path).resolve()
    try:
        # The analysis identity covers the exact bytes supplied by the user.
        # Reading through text mode would normalize CRLF on Windows and make
        # an unchanged strategy look different during the compilation pass.
        source_bytes = path.read_bytes()
        text = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StrategyAnalysisError(
            f"trade dependency source cannot be read: {path}"
        ) from exc
    if hashlib.sha256(source_bytes).hexdigest() != source_sha256:
        raise StrategyAnalysisError("trade dependency source hash differs from analysis")
    tree = ast.parse(text, filename=str(path), type_comments=True)
    strategy_name = strategies[0].get("name")
    strategy = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy_name
        ),
        None,
    )
    if strategy is None:
        raise StrategyAnalysisError("trade dependency strategy class disappeared")
    methods = {node.name: node for node in strategy.body if isinstance(node, ast.FunctionDef)}
    constants = strategies[0].get("constants")
    if not isinstance(constants, dict):
        raise StrategyAnalysisError("trade dependency constants are invalid")

    root_records: dict[str, Any] = {}
    compiled: dict[str, Any] = {}
    failures: dict[str, Any] = {}
    ephemeral_writes = {
        field
        for field, writers in {
            "_grind_entry_tag": {
                "long_grind_entry_v3",
                "short_grind_entry_v3",
            }
        }.items()
        if _observability_only_field(strategy, field, writers)
    }
    for root in roots:
        if root not in methods:
            continue
        closure = _method_closure(root, methods)
        root_records[root] = {
            "methods": sorted(closure),
            "method_count": len(closure),
            "node_count": sum(sum(1 for _ in ast.walk(methods[name])) for name in closure),
        }
        for name in sorted(closure):
            if name in compiled or name in failures:
                continue
            node = methods[name]
            try:
                method_ephemeral_writes = {
                    field
                    for field in ephemeral_writes
                    if name in {"long_grind_entry_v3", "short_grind_entry_v3"}
                }
                program = _ScalarCompiler(
                    node,
                    constants=constants,
                    available_methods=set(methods),
                    ephemeral_writes=method_ephemeral_writes,
                )
                compiled_program = program.compile()
            except _UnsupportedTradeIr as exc:
                failures[name] = {
                    "line": getattr(exc.node, "lineno", node.lineno),
                    "column": getattr(exc.node, "col_offset", node.col_offset),
                    "node": type(exc.node).__name__,
                    "message": exc.message,
                }
            else:
                compiled[name] = {
                    "line": node.lineno,
                    "end_line": node.end_lineno,
                    "node_count": sum(1 for _ in ast.walk(node)),
                    "input_contract": _scalar_input_contract(node),
                    "elided_observability_writes": sorted(method_ephemeral_writes),
                    "called_methods": sorted(program.called_methods),
                    "call_locations": {
                        called_name: {
                            "line": called_node.lineno,
                            "column": called_node.col_offset,
                        }
                        for called_name, called_node in sorted(program.called_methods.items())
                    },
                    "program": compiled_program,
                }
    changed = True
    while changed:
        changed = False
        for name, record in list(compiled.items()):
            missing = next(
                (
                    dependency
                    for dependency in record["called_methods"]
                    if dependency not in compiled
                ),
                None,
            )
            if missing is None:
                continue
            location = record["call_locations"][missing]
            failures[name] = {
                "line": location["line"],
                "column": location["column"],
                "node": "Call",
                "message": f"called method {missing!r} is not scalar-pure",
            }
            del compiled[name]
            changed = True
    for record in compiled.values():
        record.pop("call_locations", None)
    identity = {
        "schema_version": TRADE_IR_VERSION,
        "source_sha256": source_sha256,
        "roots": root_records,
        "compiled_scalar_methods": compiled,
        "stateful_methods": failures,
    }
    return {
        **identity,
        "fingerprint": hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }


def summarize_trade_dependency_ir(report: dict[str, Any]) -> dict[str, Any]:
    """Return a small artifact-safe summary while retaining the full fingerprint."""
    compiled = report.get("compiled_scalar_methods")
    if not isinstance(compiled, dict):
        raise StrategyAnalysisError("trade dependency IR compiled methods are invalid")
    method_summaries: dict[str, Any] = {}
    for name, record in compiled.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise StrategyAnalysisError("trade dependency IR method record is invalid")
        program = record.get("program")
        if not isinstance(program, dict):
            raise StrategyAnalysisError("trade dependency IR scalar program is invalid")
        expressions = program.get("expressions")
        statements = program.get("statements")
        if not isinstance(expressions, list) or not isinstance(statements, list):
            raise StrategyAnalysisError("trade dependency IR scalar arena is invalid")
        encoded = json.dumps(
            program,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        method_summaries[name] = {
            key: record[key]
            for key in (
                "line",
                "end_line",
                "node_count",
                "input_contract",
                "elided_observability_writes",
                "called_methods",
            )
        } | {
            "expression_count": len(expressions),
            "statement_count": len(statements),
            "program_sha256": hashlib.sha256(encoded).hexdigest(),
        }
    return {
        "schema_version": report["schema_version"],
        "source_sha256": report["source_sha256"],
        "roots": report["roots"],
        "compiled_scalar_methods": method_summaries,
        "stateful_methods": report["stateful_methods"],
        "fingerprint": report["fingerprint"],
    }


def _method_closure(
    root: str,
    methods: dict[str, ast.FunctionDef],
) -> set[str]:
    found: set[str] = set()
    pending = [root]
    while pending:
        name = pending.pop()
        if name in found or name not in methods:
            continue
        found.add(name)
        pending.extend(sorted(_method_calls(methods[name], methods) - found))
    return found


def _observability_only_field(
    strategy: ast.ClassDef,
    field: str,
    writers: set[str],
) -> bool:
    parents = {
        child: parent for parent in ast.walk(strategy) for child in ast.iter_child_nodes(parent)
    }
    found_write = False
    current_method: str | None = None
    method_by_node: dict[ast.AST, str] = {}
    for method in strategy.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        for item in ast.walk(method):
            method_by_node[item] = method.name
    for item in ast.walk(strategy):
        if not (
            isinstance(item, ast.Attribute)
            and isinstance(item.value, ast.Name)
            and item.value.id == "self"
            and item.attr == field
        ):
            continue
        current_method = method_by_node.get(item)
        if isinstance(item.ctx, ast.Store):
            if current_method not in writers:
                return False
            found_write = True
            continue
        if not isinstance(item.ctx, ast.Load):
            return False
        parent = parents.get(item)
        while parent is not None and not isinstance(parent, ast.Call | ast.stmt):
            parent = parents.get(parent)
        if not isinstance(parent, ast.Call):
            return False
        call_name = _call_name(parent.func)
        if call_name not in {
            "debug",
            "info",
            "notification_msg",
            "send_msg",
            "warning",
        }:
            return False
    return found_write


def _method_calls(
    node: ast.FunctionDef,
    methods: dict[str, ast.FunctionDef],
) -> set[str]:
    aliases = _method_aliases(node, set(methods))
    result: set[str] = set()
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        function = item.func
        if (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "self"
            and function.attr in methods
        ):
            result.add(function.attr)
        elif isinstance(function, ast.Name):
            if function.id in methods:
                result.add(function.id)
            elif function.id in aliases:
                result.add(aliases[function.id])
    return result


def _method_aliases(
    node: ast.FunctionDef,
    available_methods: set[str],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for item in ast.walk(node):
        if (
            isinstance(item, ast.Assign)
            and len(item.targets) == 1
            and isinstance(item.targets[0], ast.Name)
            and isinstance(item.value, ast.Attribute)
            and isinstance(item.value.value, ast.Name)
            and item.value.value.id == "self"
            and item.value.attr in available_methods
        ):
            aliases[item.targets[0].id] = item.value.attr
    return aliases


def _called_method_name(
    function: ast.expr,
    aliases: dict[str, str],
    available_methods: set[str],
) -> str | None:
    if isinstance(function, ast.Name):
        if function.id in aliases:
            return aliases[function.id]
        if function.id in available_methods:
            return function.id
    if (
        isinstance(function, ast.Attribute)
        and isinstance(function.value, ast.Name)
        and function.value.id == "self"
        and function.attr in available_methods
    ):
        return function.attr
    return None


def _scalar_input_contract(node: ast.FunctionDef) -> dict[str, Any]:
    parameters = {
        argument.arg
        for argument in (
            list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
        )
        if argument.arg != "self"
    }
    loads = {
        name: sum(
            1
            for item in ast.walk(node)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load) and item.id == name
        )
        for name in sorted(parameters)
    }
    indexed_fields: dict[str, set[str]] = {}
    numeric_thresholds: dict[str, set[float]] = {}
    for item in ast.walk(node):
        if (
            isinstance(item, ast.Subscript)
            and isinstance(item.value, ast.Name)
            and item.value.id in parameters
            and isinstance(item.slice, ast.Constant)
            and isinstance(item.slice.value, str)
        ):
            indexed_fields.setdefault(item.value.id, set()).add(item.slice.value)
        if not isinstance(item, ast.Compare):
            continue
        operands: list[ast.expr] = [item.left, *item.comparators]
        for left, right in zip(operands, operands[1:], strict=False):
            if isinstance(left, ast.Name) and left.id in parameters:
                value = _numeric_literal(right)
                if value is not None:
                    numeric_thresholds.setdefault(left.id, set()).add(value)
            if isinstance(right, ast.Name) and right.id in parameters:
                value = _numeric_literal(left)
                if value is not None:
                    numeric_thresholds.setdefault(right.id, set()).add(value)
    return {
        "parameter_loads": {name: count for name, count in loads.items() if count > 0},
        "indexed_fields": {name: sorted(fields) for name, fields in sorted(indexed_fields.items())},
        "numeric_thresholds": {
            name: sorted(values) for name, values in sorted(numeric_thresholds.items())
        },
    }


def _numeric_literal(node: ast.expr) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub | ast.UAdd)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int | float)
        and not isinstance(node.operand.value, bool)
    ):
        value = float(node.operand.value)
        return -value if isinstance(node.op, ast.USub) else value
    return None


def _type_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return type(node).__name__
