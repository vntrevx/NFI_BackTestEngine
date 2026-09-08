"""Source-compiled adjustment dispatch for a proven active system."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast

from ..callback_order_state import _lower_x7_order_filled
from ..errors import StrategyAnalysisError
from ..trade_ir import compile_scalar_ast_program
from .managed_exit_ir import _compile_tag_matcher

_PREFIX = """
if not self.position_adjustment_enable:
    return None
trade_is_short = trade.is_short
enter_tag = "empty"
if hasattr(trade, "enter_tag"):
    trade_enter_tag = trade.enter_tag
    if trade_enter_tag is not None:
        enter_tag = trade_enter_tag
enter_tags = enter_tag.split()
is_backtest = self.is_backtest_mode()
is_system_v3, is_system_v3_1, is_system_v3_2 = self.get_system_version_flags(trade)
is_system_v3_family = is_system_v3 or is_system_v3_1 or is_system_v3_2
is_system_v4 = self.is_system_v4(trade)
"""
_ARGUMENTS = (
    "trade",
    "enter_tags",
    "current_time",
    "current_rate",
    "current_profit",
    "min_stake",
    "max_stake",
    "current_entry_rate",
    "current_exit_rate",
    "current_entry_profit",
    "current_exit_profit",
)
_TARGETS = {
    "long_rebuy_adjust_trade_position_v4": "long-rebuy",
    "short_rebuy_adjust_trade_position_v4": "short-rebuy",
    "long_grind_adjust_trade_position_v4": "long-system",
    "short_grind_adjust_trade_position_v4": "short-system",
    "long_grind_adjust_trade_position": "long-legacy",
    "short_grind_adjust_trade_position": "short-legacy",
    "long_rebuy_adjust_trade_position_v3": "long-rebuy",
    "short_rebuy_adjust_trade_position_v3": "short-rebuy",
    "long_grind_adjust_trade_position_v3": "long-system",
    "short_grind_adjust_trade_position_v3": "short-system",
}


def active_system_flags(constants: Mapping[str, Any]) -> dict[str, bool]:
    flags = {
        f"is_system_{version}": isinstance(constants.get("system_name_use"), str)
        and constants.get("system_name_use") == constants.get(f"system_{version}_name")
        for version in ("v3", "v3_1", "v3_2", "v4")
    }
    flags.update(is_backtest=True, is_v2_date=True)
    flags["is_system_v3_family"] = any(flags[f"is_system_{v}"] for v in ("v3", "v3_1", "v3_2"))
    return flags


_LEGACY_TARGETS = {
    f"{side}_{kind}_adjust_trade_position{suffix}"
    for side in ("long", "short")
    for kind in ("rebuy", "grind")
    for suffix in ("", "_v2", "_v3")
} - _TARGETS.keys()


def _body(method: ast.FunctionDef) -> list[ast.stmt]:
    return [
        node
        for node in method.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]


def _same(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def _require_body(method: ast.FunctionDef, expected: str) -> None:
    actual = ast.Module(body=_body(method), type_ignores=[])
    if not _same(actual, ast.parse(expected)):
        raise StrategyAnalysisError(f"NFI active-system helper changed: {method.name}")


def prove_active_system(methods: Mapping[str, ast.FunctionDef], constants: dict[str, Any]) -> str:
    """Establish the value written on entry and used by the dispatch helpers."""
    name = constants.get("system_name_use")
    names = [constants.get(f"system_{version}_name") for version in ("v3", "v3_1", "v3_2", "v4")]
    if not isinstance(name, str) or not name or name not in names:
        raise StrategyAnalysisError("NFI active system is not a declared system")
    if not all(isinstance(value, str) and value for value in names) or len(set(names)) != len(
        names
    ):
        raise StrategyAnalysisError("NFI active-system names are ambiguous")
    templates = {
        "is_system_v4": 'return trade.get_custom_data(key="system_version") == self.system_v4_name',
        "is_backtest_mode": 'return self.dp.runmode.value in ["backtest", "hyperopt"]',
        "get_system_version_flags": """
system_version = trade.get_custom_data(key="system_version")
return (system_version == self.system_v3_name,
        system_version == self.system_v3_1_name,
        system_version == self.system_v3_2_name)
""",
    }
    for helper, template in templates.items():
        if helper not in methods:
            raise StrategyAnalysisError(f"NFI active-system helper is missing: {helper}")
        _require_body(methods[helper], template)
    callback = methods.get("order_filled")
    lowered = _lower_x7_order_filled(callback, constants=constants) if callback else None
    operation = cast(dict[str, Any], lowered["operation"]) if lowered else {}
    if not operation.get("initial_entry_requires_no_exits"):
        raise StrategyAnalysisError("NFI active system requires guarded initial order state")
    actions = operation["order_tag_actions"]
    if any(write["key"] == "system_version" for writes in actions.values() for write in writes):
        raise StrategyAnalysisError("NFI order tags may change the active system")
    # No other callback may mutate the system marker after initial entry.
    for method in methods.values():
        if method.name == "order_filled":
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set_custom_data"
            ):
                key = next((item.value for item in node.keywords if item.arg == "key"), None)
                key = node.args[0] if node.args else key
                if not isinstance(key, ast.Constant) or key.value == "system_version":
                    raise StrategyAnalysisError("NFI active-system state may be mutated")
    return name


def compile_adjustment_dispatch(
    methods: Mapping[str, ast.FunctionDef],
    constants: dict[str, Any],
) -> dict[str, Any]:
    system = prove_active_system(methods, constants)
    method = methods["adjust_trade_position"]
    expected_prefix = ast.parse(_PREFIX).body
    if len(method.body) < len(expected_prefix) + 3 or any(
        not _same(actual, expected)
        for actual, expected in zip(method.body, expected_prefix, strict=False)
    ):
        raise StrategyAnalysisError("NFI adjustment dispatch input prelude changed")
    remainder = copy.deepcopy(method.body[len(expected_prefix) :])
    date_gate = remainder.pop(0)
    if (
        not isinstance(date_gate, ast.If)
        or not _same(date_gate.test, ast.Name(id="is_backtest", ctx=ast.Load()))
        or not _same(
            ast.Module(body=date_gate.body, type_ignores=[]), ast.parse("is_v2_date = True")
        )
    ):
        raise StrategyAnalysisError("NFI adjustment backtest date policy changed")
    args = remainder.pop(0)
    expected_args = ast.parse("args = (" + ",".join(_ARGUMENTS) + ",)").body[0]
    if not _same(args, expected_args):
        raise StrategyAnalysisError("NFI adjustment dispatch callback arguments changed")
    fragment = ast.parse("def dispatch(is_short):\n trade_is_short = is_short\n").body[0]
    assert isinstance(fragment, ast.FunctionDef)
    fragment.body.extend(remainder)
    specializer = _DispatchLowerer(constants)
    specializer.visit(fragment)
    if constants.get("position_adjustment_enable") is False:
        fragment.body.insert(0, ast.Return(value=ast.Constant(value=None)))
    elif constants.get("position_adjustment_enable") is not True:
        raise StrategyAnalysisError("NFI position adjustment enablement must be boolean")
    fragment.args.args.extend(ast.arg(arg=name) for name in specializer.predicates)
    program = compile_scalar_ast_program(ast.fix_missing_locations(fragment), constants={})
    _prove_no_unsupported_target(fragment, specializer.predicates)
    result = {
        "schema_version": "adjustment-dispatch-program-v1",
        "system_version": system,
        "predicates": specializer.predicates,
        "program": program,
    }
    result["fingerprint"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def prove_adjustment_inputs(
    method: ast.FunctionDef,
    *,
    side: str,
    family: str = "v4",
) -> None:
    """Bind specialized flags and generic operands to their actual source values."""
    expected = {
        "is_system_v4": "self.is_system_v4(trade)",
        "is_not_trade_max_stake_v4": (
            "current_stake_amount < (slice_amount * self.system_v4_max_stake)"
        ),
        f"is_{side}_grind_entry": (
            "self.long_grind_entry_v4(last_candle, previous_candle, "
            "num_open_grinds_and_buybacks, slice_profit, slice_profit_entry, "
            "slice_profit_exit, True)"
            if side == "long"
            else "self.short_grind_entry_v4(last_candle, previous_candle, slice_profit, True)"
        ),
    }
    if family not in {"v3", "v4"}:
        raise StrategyAnalysisError("NFI adjustment operand family is unsupported")
    if family == "v3":
        del expected["is_system_v4"]
        expected = {
            name.replace("v4", "v3"): expression.replace("v4", "v3")
            for name, expression in expected.items()
        }
        statement = ast.parse(
            "is_system_v3, is_system_v3_1, is_system_v3_2 = self.get_system_version_flags(trade)"
        ).body[0]
        if sum(_same(node, statement) for node in method.body) != 1:
            raise StrategyAnalysisError("NFI adjustment source system flags changed")
        for flag in ("is_system_v3", "is_system_v3_1", "is_system_v3_2"):
            if (
                sum(
                    isinstance(node, ast.Name)
                    and node.id == flag
                    and not isinstance(node.ctx, ast.Load)
                    for node in ast.walk(method)
                )
                != 1
            ):
                raise StrategyAnalysisError("NFI adjustment source system flag is reassigned")
    for name, expression in expected.items():
        assignments = [
            node
            for node in method.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ]
        stores = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store)
        ]
        if (
            len(assignments) != 1
            or len(stores) != 1
            or not _same(assignments[0].value, ast.parse(expression, mode="eval").body)
        ):
            raise StrategyAnalysisError(f"NFI adjustment source operand changed: {name}")


class _DispatchLowerer(ast.NodeTransformer):
    def __init__(self, constants: Mapping[str, Any]) -> None:
        self.constants = constants
        self.flags = active_system_flags(constants)
        self.predicates: dict[str, dict[str, Any]] = {}

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.flags:
            if not isinstance(node.ctx, ast.Load):
                raise StrategyAnalysisError("NFI adjustment system predicate is reassigned")
            return ast.copy_location(ast.Constant(value=self.flags[node.id]), node)
        return node

    def visit_If(self, node: ast.If) -> ast.AST | list[ast.stmt]:
        node.test = self.visit(node.test)
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            branch = node.body if node.test.value else node.orelse
            result = []
            for statement in branch:
                lowered = self.visit(statement)
                result.extend(lowered if isinstance(lowered, list) else [lowered])
            return result
        return self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> ast.AST:
        call = node.value
        if isinstance(call, ast.Constant) and call.value is None:
            return node
        if (
            not isinstance(call, ast.Call)
            or not isinstance(call.func, ast.Attribute)
            or not isinstance(call.func.value, ast.Name)
            or call.func.value.id != "self"
            or call.func.attr not in _TARGETS.keys() | _LEGACY_TARGETS
            or call.keywords
            or len(call.args) != 1
            or not isinstance(call.args[0], ast.Starred)
            or not isinstance(call.args[0].value, ast.Name)
            or call.args[0].value.id != "args"
        ):
            raise StrategyAnalysisError(
                "NFI adjustment dispatch return target or arguments changed"
            )
        target = _TARGETS.get(call.func.attr, "unsupported")
        return ast.copy_location(ast.Return(value=ast.Constant(value=target)), node)

    def visit_Call(self, node: ast.Call) -> ast.AST:
        matcher = _compile_tag_matcher(node, {}, self.constants)
        if matcher is None:
            raise StrategyAnalysisError("NFI adjustment dispatch contains an unsupported call")
        name = next(
            (key for key, value in self.predicates.items() if value == matcher),
            f"tag_predicate_{len(self.predicates)}",
        )
        self.predicates[name] = matcher
        return ast.copy_location(ast.Name(id=name, ctx=ast.Load()), node)


def _prove_no_unsupported_target(
    method: ast.FunctionDef,
    predicates: dict[str, dict[str, Any]],
) -> None:
    # any/all membership is invariant under tag order, duplicate words, and
    # replacement by a word with the same memberships. Enumerating subsets of
    # these equivalence classes proves all possible compound-tag inputs.
    groups = [set(record["entry_tags"]) for record in predicates.values()]
    signatures = {tuple(tag in group for group in groups) for tag in set().union(*groups)}
    signatures.add((False,) * len(groups))  # Every word outside the known universe.
    classes = sorted(signatures)
    if len(classes) > 16:
        raise StrategyAnalysisError("NFI adjustment tag partition exceeds proof bound")
    for mask in range(1 << len(classes)):
        selected = [record for index, record in enumerate(classes) if mask & (1 << index)]
        values = {
            name: (any if predicate["operator"] == "any" else all)(
                signature[index] for signature in selected
            )
            for index, (name, predicate) in enumerate(predicates.items())
        }
        for is_short in (False, True):
            outcome = _dispatch_block(method.body, {**values, "is_short": is_short})
            if outcome == "unsupported":
                raise StrategyAnalysisError("NFI adjustment dispatch reaches an unsupported family")


_FALLTHROUGH = object()


def _dispatch_block(body: list[ast.stmt], values: dict[str, Any]) -> Any:
    for statement in body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            values[statement.targets[0].id] = _dispatch_value(statement.value, values)
        elif isinstance(statement, ast.If):
            branch = statement.body if _dispatch_value(statement.test, values) else statement.orelse
            result = _dispatch_block(branch, values)
            if result is not _FALLTHROUGH:
                return result
        elif isinstance(statement, ast.Return):
            return _dispatch_value(statement.value, values)
        else:
            raise StrategyAnalysisError("NFI adjustment dispatch proof cannot represent statement")
    return _FALLTHROUGH


def _dispatch_value(node: ast.AST | None, values: dict[str, Any]) -> Any:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _dispatch_value(node.operand, values)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And | ast.Or):
        return (all if isinstance(node.op, ast.And) else any)(
            _dispatch_value(value, values) for value in node.values
        )
    raise StrategyAnalysisError("NFI adjustment dispatch proof cannot represent expression")
