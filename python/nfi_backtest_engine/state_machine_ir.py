"""Compile a bounded subset of stateful Freqtrade callbacks into generic IR."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Never

from .errors import StrategyAnalysisError
from .specs import STATE_MACHINE_PROGRAM_SCHEMA, validate_schema
from .strategy_ir import analyze_strategy

STATE_MACHINE_PROGRAM_VERSION = "state-machine-program-v1"
STATE_MACHINE_ENTRYPOINTS = (
    "order_filled",
    "adjust_trade_position",
    "custom_exit",
)
_SCALAR_CALLS = {"abs", "min", "max", "float", "int", "bool", "len"}
_CANDLE_INPUTS = {
    "current_time",
    "current_rate",
    "current_profit",
    "current_entry_rate",
    "current_exit_rate",
    "current_entry_profit",
    "current_exit_profit",
}
_WALLET_INPUTS = {"min_stake", "max_stake"}


class StateMachineCompileError(StrategyAnalysisError):
    """A source location cannot be represented by the bounded VM."""


def compile_state_machine_program(
    source: str | Path,
    *,
    class_name: str | None = None,
) -> dict[str, Any]:
    """Compile supported stateful callbacks without executing strategy code."""

    path = Path(source).resolve()
    analysis = analyze_strategy(path, class_name=class_name)
    diagnostics = [
        item
        for item in analysis["diagnostics"]
        if item.get("severity") == "error"
    ]
    if diagnostics:
        first = diagnostics[0]
        location = first["location"]
        raise StateMachineCompileError(
            f"{location['path']}:{location['line']}:{location['column']}: "
            f"{first['code']}: {first['message']}"
        )
    strategies = analysis["strategies"]
    if len(strategies) != 1:
        raise StateMachineCompileError(
            "state-machine compilation requires one selected strategy"
        )
    selected_class = str(strategies[0]["name"])
    tree = ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))
    class_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == selected_class
        ),
        None,
    )
    if class_node is None:
        raise StateMachineCompileError(
            f"selected strategy class was not found: {selected_class}"
        )
    constants = strategies[0].get("constants")
    compiler = _Compiler(
        path,
        class_constants=constants if isinstance(constants, Mapping) else {},
    )
    entrypoints = {}
    for node in class_node.body:
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in STATE_MACHINE_ENTRYPOINTS
        ):
            if isinstance(node, ast.AsyncFunctionDef):
                compiler.unsupported(node, "async callback")
            compiler.parameters = {
                argument.arg for argument in (*node.args.args, *node.args.kwonlyargs)
            }
            compiler.locals = set()
            instructions = compiler.statements(node.body)
            entrypoints[node.name] = {
                "max_steps": max(1, _instruction_steps(instructions)),
                "instructions": instructions,
            }
    program = {
        "schema_version": STATE_MACHINE_PROGRAM_VERSION,
        "entrypoints": entrypoints,
        "required_reads": [
            {"source": source_name, "key": key}
            for source_name, key in sorted(compiler.required_reads)
        ],
        "required_columns": sorted(compiler.required_columns),
        "required_state_keys": sorted(compiler.required_state_keys),
        "opcodes": sorted(compiler.opcodes),
        "source_map": compiler.source_map,
    }
    validate_schema(program, STATE_MACHINE_PROGRAM_SCHEMA)
    return program


class _Compiler:
    def __init__(
        self,
        path: Path,
        *,
        class_constants: Mapping[str, Any],
    ) -> None:
        self.path = path
        self.class_constants = class_constants
        self.next_id = 1
        self.source_map: dict[str, dict[str, Any]] = {}
        self.required_reads: set[tuple[str, str]] = set()
        self.required_columns: set[str] = set()
        self.required_state_keys: set[str] = set()
        self.opcodes: set[str] = set()
        self.parameters: set[str] = set()
        self.locals: set[str] = set()

    def statements(self, nodes: Sequence[ast.stmt]) -> list[dict[str, Any]]:
        return [instruction for node in nodes for instruction in self.statement(node)]

    def statement(self, node: ast.stmt) -> list[dict[str, Any]]:
        if isinstance(node, ast.If):
            return [
                self.instruction(
                    node,
                    "if",
                    condition=self.expression(node.test),
                    then_instructions=self.statements(node.body),
                    else_instructions=self.statements(node.orelse),
                )
            ]
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                self.unsupported(node, "assignment target")
            self.locals.add(target.id)
            return [
                self.instruction(
                    node,
                    "set_local",
                    name=target.id,
                    value=self.expression(node.value),
                )
            ]
        if isinstance(node, ast.AnnAssign):
            if not isinstance(node.target, ast.Name) or node.value is None:
                self.unsupported(node, "annotated assignment")
            self.locals.add(node.target.id)
            return [
                self.instruction(
                    node,
                    "set_local",
                    name=node.target.id,
                    value=self.expression(node.value),
                )
            ]
        if isinstance(node, ast.AugAssign):
            if not isinstance(node.target, ast.Name):
                self.unsupported(node, "augmented assignment target")
            name = node.target.id
            self.locals.add(name)
            return [
                self.instruction(
                    node,
                    "set_local",
                    name=name,
                    value={
                        "kind": "binary",
                        "operator": _binary_operator(node.op, self, node),
                        "left": self.read("local", name),
                        "right": self.expression(node.value),
                    },
                )
            ]
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            custom = self.custom_state_instruction(node, node.value)
            if custom is not None:
                return [custom]
            return [
                self.instruction(
                    node,
                    "evaluate",
                    expression=self.expression(node.value),
                )
            ]
        if isinstance(node, ast.Return):
            return [self.return_action(node)]
        if isinstance(node, ast.For):
            if not isinstance(node.target, ast.Name) or node.orelse:
                self.unsupported(node, "for loop shape")
            start, stop = _range_bounds(node.iter, self)
            iterations = stop - start
            if iterations < 0:
                self.unsupported(node, "descending range")
            self.locals.add(node.target.id)
            return [
                self.instruction(
                    node,
                    "bounded_for",
                    variable=node.target.id,
                    start=start,
                    stop=stop,
                    max_iterations=iterations,
                    instructions=self.statements(node.body),
                )
            ]
        if isinstance(node, ast.Pass):
            return []
        self.unsupported(node, type(node).__name__)

    def custom_state_instruction(
        self,
        statement: ast.stmt,
        call: ast.Call,
    ) -> dict[str, Any] | None:
        name = _call_leaf(call)
        if name not in {"set_custom_data", "delete_custom_data"}:
            return None
        if not call.args or not _literal_string(call.args[0]):
            self.unsupported(call, f"{name} dynamic key")
        key = str(ast.literal_eval(call.args[0]))
        self.required_state_keys.add(key)
        if name == "delete_custom_data":
            if len(call.args) != 1 or call.keywords:
                self.unsupported(call, "delete_custom_data arguments")
            return self.instruction(statement, "delete_state", key=key)
        if len(call.args) != 2 or call.keywords:
            self.unsupported(call, "set_custom_data arguments")
        value = self.expression(call.args[1])
        return self.instruction(
            statement,
            "set_state",
            key=key,
            value_type=_value_type(call.args[1]),
            value=value,
        )

    def return_action(self, node: ast.Return) -> dict[str, Any]:
        if node.value is None or (
            isinstance(node.value, ast.Constant) and node.value.value is None
        ):
            return self.instruction(
                node,
                "action",
                kind="no_op",
                stake=None,
                tag=None,
            )
        if isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2:
            stake_node, tag_node = node.value.elts
            tag = self.expression(tag_node)
            return self.instruction(
                node,
                "action",
                kind=_position_action_kind(stake_node, self),
                stake=self.expression(stake_node),
                tag=tag,
            )
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return self.instruction(
                node,
                "action",
                kind="exit",
                stake=None,
                tag=self.expression(node.value),
            )
        self.unsupported(node, "return value")

    def expression(self, node: ast.AST) -> dict[str, Any]:
        if isinstance(node, ast.Constant):
            if node.value is Ellipsis:
                self.unsupported(node, "ellipsis")
            return {"kind": "literal", "value": node.value}
        if isinstance(node, ast.Name):
            if node.id in self.locals:
                return self.read("local", node.id)
            if node.id in _CANDLE_INPUTS:
                return self.read("candle", node.id)
            if node.id in _WALLET_INPUTS:
                return self.read("wallet", node.id)
            if node.id in self.parameters:
                return self.read("input", node.id)
            self.unsupported(node, f"unbound name {node.id}")
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "trade":
                return self.read("trade", node.attr)
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in self.class_constants
            ):
                return {
                    "kind": "literal",
                    "value": self.class_constants[node.attr],
                }
            self.unsupported(node, "attribute read")
        if isinstance(node, ast.Subscript):
            if (
                isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and "dataframe" in ast.unparse(node.value).lower()
            ):
                key = node.slice.value
                self.required_columns.add(key)
                return self.read("candle", key)
            self.unsupported(node, "subscript")
        if isinstance(node, ast.UnaryOp):
            return {
                "kind": "unary",
                "operator": _unary_operator(node.op, self, node),
                "operand": self.expression(node.operand),
            }
        if isinstance(node, ast.BinOp):
            return {
                "kind": "binary",
                "operator": _binary_operator(node.op, self, node),
                "left": self.expression(node.left),
                "right": self.expression(node.right),
            }
        if isinstance(node, ast.BoolOp):
            return {
                "kind": "boolean",
                "operator": "and" if isinstance(node.op, ast.And) else "or",
                "values": [self.expression(value) for value in node.values],
            }
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            return {
                "kind": "compare",
                "operator": _comparison(node.ops[0], self, node),
                "left": self.expression(node.left),
                "right": self.expression(node.comparators[0]),
            }
        if isinstance(node, ast.Call):
            name = _call_leaf(node)
            if name == "get_custom_data":
                if not node.args or not _literal_string(node.args[0]) or node.keywords:
                    self.unsupported(node, "get_custom_data arguments")
                key = str(ast.literal_eval(node.args[0]))
                self.required_state_keys.add(key)
                default = (
                    self.expression(node.args[1]) if len(node.args) == 2 else None
                )
                if len(node.args) > 2:
                    self.unsupported(node, "get_custom_data arguments")
                return self.read("custom_state", key, default=default)
            if name == "select_filled_orders":
                if len(node.args) != 1 or node.keywords:
                    self.unsupported(node, "select_filled_orders arguments")
                selector = ast.unparse(node.args[0])
                if selector == "trade.entry_side":
                    key = "filled_entries"
                elif selector == "trade.exit_side":
                    key = "filled_exits"
                else:
                    self.unsupported(node, "select_filled_orders side")
                return self.read("orders", key)
            if name in _SCALAR_CALLS and not node.keywords:
                return {
                    "kind": "scalar_call",
                    "name": name,
                    "arguments": [self.expression(argument) for argument in node.args],
                }
            self.unsupported(node, f"call {name or ast.unparse(node.func)}")
        self.unsupported(node, f"expression {type(node).__name__}")

    def read(
        self,
        source: str,
        key: str,
        *,
        default: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if source != "local":
            self.required_reads.add((source, key))
        return {
            "kind": "read",
            "source": source,
            "key": key,
            "default": default,
        }

    def instruction(
        self,
        node: ast.AST,
        opcode: str,
        **fields: Any,
    ) -> dict[str, Any]:
        identifier = f"i{self.next_id}"
        self.next_id += 1
        self.opcodes.add(opcode)
        self.source_map[identifier] = {
            "path": self.path.name,
            "line": getattr(node, "lineno", 1),
            "column": getattr(node, "col_offset", 0),
            "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
            "end_column": getattr(
                node,
                "end_col_offset",
                getattr(node, "col_offset", 0),
            ),
        }
        return {"opcode": opcode, "id": identifier, **fields}

    def unsupported(self, node: ast.AST, construct: str) -> Never:
        raise StateMachineCompileError(
            f"{self.path}:{getattr(node, 'lineno', 1)}:"
            f"{getattr(node, 'col_offset', 0)}: "
            f"STATE_MACHINE_UNSUPPORTED: {construct}"
        )


def _literal_string(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _call_leaf(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _range_bounds(node: ast.AST, compiler: _Compiler) -> tuple[int, int]:
    if not isinstance(node, ast.Call) or _call_leaf(node) != "range" or node.keywords:
        compiler.unsupported(node, "non-range for loop")
    try:
        values = [ast.literal_eval(argument) for argument in node.args]
    except (ValueError, TypeError):
        compiler.unsupported(node, "dynamic range bound")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        compiler.unsupported(node, "non-integer range bound")
    if len(values) == 1:
        return 0, values[0]
    if len(values) == 2:
        return values[0], values[1]
    compiler.unsupported(node, "range step")


def _unary_operator(
    node: ast.unaryop,
    compiler: _Compiler,
    owner: ast.AST,
) -> str:
    mapping = {ast.Not: "not", ast.USub: "negative", ast.UAdd: "positive"}
    value = mapping.get(type(node))
    return value if value is not None else compiler.unsupported(owner, "unary operator")


def _binary_operator(
    node: ast.operator,
    compiler: _Compiler,
    owner: ast.AST,
) -> str:
    mapping = {
        ast.Add: "add",
        ast.Sub: "subtract",
        ast.Mult: "multiply",
        ast.Div: "divide",
        ast.FloorDiv: "floor_divide",
        ast.Mod: "modulo",
        ast.Pow: "power",
    }
    value = mapping.get(type(node))
    return value if value is not None else compiler.unsupported(owner, "binary operator")


def _comparison(
    node: ast.cmpop,
    compiler: _Compiler,
    owner: ast.AST,
) -> str:
    mapping = {
        ast.Eq: "equal",
        ast.NotEq: "not_equal",
        ast.Lt: "less",
        ast.LtE: "less_equal",
        ast.Gt: "greater",
        ast.GtE: "greater_equal",
        ast.Is: "is",
        ast.IsNot: "is_not",
    }
    value = mapping.get(type(node))
    return value if value is not None else compiler.unsupported(owner, "comparison")


def _value_type(node: ast.AST) -> str:
    if not isinstance(node, ast.Constant):
        return "json"
    value = node.value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "json"


def _position_action_kind(stake: ast.AST, compiler: _Compiler) -> str:
    sign = _static_numeric_sign(stake)
    if sign is None or sign == 0:
        compiler.unsupported(stake, "position adjustment stake sign")
    return "partial_exit" if sign < 0 else "add_entry"


def _static_numeric_sign(node: ast.AST) -> int | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return (node.value > 0) - (node.value < 0)
    if isinstance(node, ast.Name) and node.id in {"min_stake", "max_stake"}:
        return 1
    if isinstance(node, ast.UnaryOp):
        sign = _static_numeric_sign(node.operand)
        if sign is None:
            return None
        if isinstance(node.op, ast.USub):
            return -sign
        if isinstance(node.op, ast.UAdd):
            return sign
    if (
        isinstance(node, ast.Call)
        and _call_leaf(node) == "abs"
        and len(node.args) == 1
        and not node.keywords
    ):
        return 1
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult | ast.Div):
        left = _static_numeric_sign(node.left)
        right = _static_numeric_sign(node.right)
        return left * right if left is not None and right is not None else None
    return None


def _instruction_steps(instructions: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for instruction in instructions:
        total += 1 + _expression_steps(instruction)
        opcode = instruction["opcode"]
        if opcode == "if":
            total += max(
                _instruction_steps(instruction["then_instructions"]),
                _instruction_steps(instruction["else_instructions"]),
            )
        elif opcode == "bounded_for":
            total += int(instruction["max_iterations"]) * _instruction_steps(
                instruction["instructions"]
            )
    return total


def _expression_steps(value: Any) -> int:
    if isinstance(value, Mapping):
        own = 1 if "kind" in value else 0
        return own + sum(_expression_steps(item) for item in value.values())
    if isinstance(value, list):
        return sum(_expression_steps(item) for item in value)
    return 0
