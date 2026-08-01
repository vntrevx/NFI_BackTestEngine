"""Compile X7 system-v3 adjustment actions into strategy-neutral programs."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import StrategyAnalysisError
from ..trade_ir import compile_scalar_ast_program

SYSTEM_ADJUSTMENT_PROGRAM_VERSION = "system-adjustment-program-v1"

_DERISK_TAG = re.compile(r"^derisk_level_(\d+)$")
_GRIND_TAG = re.compile(r"^grind_(\d+)_(entry|exit|derisk)$")
_GRIND_VARIABLE = re.compile(r"^grind_(\d+)_(.+)$")
_DERISK_VARIABLE = re.compile(r"^is_derisk_(\d+)(?:_found)?$")
_MAXIMUM_KEY = re.compile(r"^grind_(\d+)_cluster_max_profit_(stake|rate)$")
_BUILTIN_NAMES = {"abs", "all", "any", "bool", "float", "int", "len", "max", "min", "str"}

_COMMON_BINDINGS = {
    "current_rate": "current-rate",
    "current_stake_amount": "current-stake-amount",
    "exit_rate": "exit-rate",
    "fee_close_rate": "fee-close-rate",
    "fee_open_rate": "fee-open-rate",
    "first_entry_amount": "first-entry-amount",
    "is_futures_mode": "is-futures-mode",
    "is_long_extra_checks_entry": "extra-entry-checks",
    "is_long_grind_entry": "grind-entry-signal",
    "is_short_extra_checks_entry": "extra-entry-checks",
    "is_short_grind_entry": "grind-entry-signal",
    "is_not_trade_max_stake_v3": "below-maximum-stake",
    "is_rebuy_mode": "is-rebuy-mode",
    "is_system_v3": "is-system-v3",
    "is_system_v3_1": "is-system-v31",
    "is_system_v3_2": "is-system-v32",
    "last_candle": "last-candle",
    "max_stake": "maximum-stake",
    "min_stake": "minimum-stake",
    "num_open_grinds_and_buybacks": "open-grind-count",
    "previous_candle": "previous-candle",
    "profit_ratio": "profit-ratio",
    "profit_stake": "profit-stake",
    "slice_amount": "slice-amount",
    "slice_profit": "slice-profit",
    "slice_profit_entry": "slice-profit-entry",
    "tag": "action-tag",
    "trade": "trade",
    "trade_amount": "trade-amount",
    "trade_leverage": "trade-leverage",
    "trade_stake_amount": "trade-stake-amount",
}

_CLUSTER_BINDINGS = {
    "sub_grind_count": "cluster-count",
    "max_sub_grinds": "cluster-maximum-count",
    "distance_ratio": "cluster-distance",
    "sub_thresholds": "cluster-thresholds",
    "stakes": "cluster-stakes",
    "total_amount": "cluster-total-amount",
    "current_open_rate": "cluster-open-rate",
    "current_grind_profit_rate": "cluster-profit-rate",
    "current_grind_profit_stake": "cluster-profit-stake",
    "profit_threshold": "cluster-profit-threshold",
    "derisk_grinds": "cluster-derisk-threshold",
    "cluster_max_profit_stake": "cluster-maximum-profit-stake",
    "cluster_max_profit_rate": "cluster-maximum-profit-rate",
}


@dataclass(frozen=True)
class _SourceAction:
    kind: str
    level: int
    tag: str
    statement: ast.If
    append_entry_ids: bool


def compile_system_adjustment_ir(
    method: ast.FunctionDef,
    exit_method: ast.FunctionDef,
    constants: Mapping[str, Any],
    *,
    side: str,
    retry_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Lower one source callback into source-ordered generic action programs."""

    if side not in {"long", "short"}:
        raise StrategyAnalysisError(f"system adjustment side is unsupported: {side}")
    actions = _source_actions(method, exit_method.name)
    levels = sorted({action.level for action in actions if action.kind.startswith("grind-")})
    if not levels:
        raise StrategyAnalysisError("system adjustment has no Grind levels")
    _validate_action_coverage(actions, levels)
    order_scan = _compile_order_scan(method, actions, levels, side=side)
    exit_program = _compile_exit_program(exit_method, constants)
    compiled_actions = []
    for action in actions:
        if action.kind == "grind-exit":
            bindings = [
                {
                    **binding,
                    **(
                        {"level": action.level}
                        if binding.get("level") == "action"
                        else {}
                    ),
                }
                for binding in exit_program["bindings"]
            ]
            compiled_actions.append(
                {
                    "kind": action.kind,
                    "level": action.level,
                    "tag": action.tag,
                    "append_entry_ids": True,
                    "decision_program": exit_program["decision_program"],
                    "bindings": bindings,
                    "input_contract": exit_program["input_contract"],
                    "location": _location(action.statement),
                }
            )
            continue
        compiled = _compile_action_program(action, constants)
        compiled_actions.append(
            {
                "kind": action.kind,
                "level": action.level,
                "tag": action.tag,
                "append_entry_ids": action.append_entry_ids,
                **compiled,
                "location": _location(action.statement),
            }
        )
    program: dict[str, Any] = {
        "schema_version": SYSTEM_ADJUSTMENT_PROGRAM_VERSION,
        "execution_mode": "primary-with-legacy-shadow",
        "side": side,
        "source_callback": method.name,
        "source_order": compiled_actions,
        "order_scan": order_scan,
        "input_contract": _merge_input_contracts(compiled_actions),
        "retry_policy": {
            "entry_retry_ms": _positive_int(retry_policy, "entry_retry_ms"),
            "stale_order_ms": _positive_int(retry_policy, "stale_order_ms"),
        },
        "location": _location(method),
    }
    encoded = json.dumps(
        program,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    program["fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return program


def _source_actions(method: ast.FunctionDef, exit_method_name: str) -> list[_SourceAction]:
    actions: list[_SourceAction] = []
    for statement in method.body:
        if not isinstance(statement, ast.If):
            continue
        exit_call = next(
            (
                node
                for node in ast.walk(statement)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr == exit_method_name
            ),
            None,
        )
        if exit_call is not None:
            tag = _exit_call_tag(exit_call)
            match = _GRIND_TAG.fullmatch(tag)
            if match is None or match.group(2) != "exit":
                raise StrategyAnalysisError("system adjustment Grind exit call changed")
            level = int(match.group(1))
            _validate_exit_call(exit_call, level)
            actions.append(_SourceAction("grind-exit", level, tag, statement, True))
            continue
        tag = _returned_or_assigned_tag(statement)
        if tag is None:
            continue
        derisk = _DERISK_TAG.fullmatch(tag)
        if derisk is not None:
            actions.append(_SourceAction("derisk", int(derisk.group(1)), tag, statement, False))
            continue
        grind = _GRIND_TAG.fullmatch(tag)
        if grind is None:
            continue
        action = grind.group(2)
        if action == "exit":
            raise StrategyAnalysisError("system adjustment inline Grind exit changed")
        actions.append(
            _SourceAction(
                f"grind-{action}",
                int(grind.group(1)),
                tag,
                statement,
                action == "derisk",
            )
        )
    if not actions:
        raise StrategyAnalysisError("system adjustment action sequence is empty")
    return actions


def _validate_action_coverage(actions: list[_SourceAction], levels: list[int]) -> None:
    derisk_levels = [action.level for action in actions if action.kind == "derisk"]
    if not derisk_levels or len(derisk_levels) != len(set(derisk_levels)):
        raise StrategyAnalysisError("system adjustment de-risk sequence changed")
    expected_prefix = [f"derisk:{level}" for level in sorted(derisk_levels)]
    actual = [f"{action.kind}:{action.level}" for action in actions]
    if actual[: len(expected_prefix)] != expected_prefix:
        raise StrategyAnalysisError("system adjustment de-risk source order changed")
    expected_grinds = [
        f"grind-{kind}:{level}"
        for level in levels
        for kind in ("entry", "exit", "derisk")
    ]
    if actual[len(expected_prefix) :] != expected_grinds:
        raise StrategyAnalysisError("system adjustment Grind source order changed")


def _compile_order_scan(
    method: ast.FunctionDef,
    actions: list[_SourceAction],
    levels: list[int],
    *,
    side: str,
) -> dict[str, Any]:
    loops = [
        node
        for node in method.body
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "order"
        and isinstance(node.iter, ast.Call)
        and isinstance(node.iter.func, ast.Name)
        and node.iter.func.id == "reversed"
    ]
    if len(loops) != 1:
        raise StrategyAnalysisError("system adjustment filled-order scan changed")
    sides = [
        str(node.comparators[0].value)
        for node in ast.walk(loops[0])
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and isinstance(node.left.value, ast.Name)
        and node.left.value.id == "order"
        and node.left.attr == "ft_order_side"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value in {"buy", "sell"}
    ]
    unique_sides = list(dict.fromkeys(sides))
    expected_sides = ["buy", "sell"] if side == "long" else ["sell", "buy"]
    if unique_sides != expected_sides:
        raise StrategyAnalysisError("system adjustment order directions changed")
    constants = {
        str(node.value)
        for node in ast.walk(loops[0])
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    global_tags = sorted(tag for tag in constants if tag == "derisk_global")
    if len(global_tags) != 1:
        raise StrategyAnalysisError("system adjustment global exit tag changed")
    maxima = _maximum_keys(method, levels)
    stake_scales = _stake_scales(method, levels)
    action_by_key = {(action.kind, action.level): action.tag for action in actions}
    return {
        "sequence": "reverse",
        "entry_order_side": unique_sides[0],
        "exit_order_side": unique_sides[1],
        "exclude_first_entry": True,
        "global_exit_tag": global_tags[0],
        "derisk_tags": [
            {"level": action.level, "tag": action.tag}
            for action in actions
            if action.kind == "derisk"
        ],
        "grind_levels": [
            {
                "level": level,
                "entry_tag": action_by_key[("grind-entry", level)],
                "exit_tag": action_by_key[("grind-exit", level)],
                "derisk_tag": action_by_key[("grind-derisk", level)],
                "maximum_profit_stake_key": maxima[level]["stake"],
                "maximum_profit_rate_key": maxima[level]["rate"],
                "minimum_scale_leverage": stake_scales[level],
            }
            for level in levels
        ],
        "partial_fill_policy": "filled-orders-have-zero-remaining",
    }


def _maximum_keys(method: ast.FunctionDef, levels: list[int]) -> dict[int, dict[str, str]]:
    result: dict[int, dict[str, str]] = {}
    for node in method.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.BoolOp):
            continue
        for call in ast.walk(node.value):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr != "get_custom_data":
                continue
            key = next(
                (
                    str(keyword.value.value)
                    for keyword in call.keywords
                    if keyword.arg == "key"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                None,
            )
            match = _MAXIMUM_KEY.fullmatch(key or "")
            if match is not None:
                result.setdefault(int(match.group(1)), {})[match.group(2)] = str(key)
    if sorted(result) != levels or any(set(result[level]) != {"stake", "rate"} for level in levels):
        raise StrategyAnalysisError("system adjustment cluster maximum keys changed")
    return result


def _stake_scales(method: ast.FunctionDef, levels: list[int]) -> dict[int, str]:
    result: dict[int, str] = {}
    for statement in method.body:
        if (
            not isinstance(statement, ast.Assign)
            or len(statement.targets) != 1
            or not isinstance(statement.targets[0], ast.Name)
            or (match := re.fullmatch(r"grind_(\d+)_stakes", statement.targets[0].id))
            is None
            or not isinstance(statement.value, ast.Call)
            or _call_path(statement.value.func)
            not in {
                "scale_stakes_for_min_stake",
                "self.scale_stakes_for_min_stake",
            }
            or len(statement.value.args) < 4
            or not isinstance(statement.value.args[3], ast.Name)
        ):
            continue
        scale = {
            "trade_leverage": "trade-leverage",
            "stake_scale_leverage": "market-mode-leverage",
        }.get(statement.value.args[3].id)
        if scale is None:
            raise StrategyAnalysisError("system adjustment minimum-stake scaling changed")
        result[int(match.group(1))] = scale
    if sorted(result) != levels:
        raise StrategyAnalysisError("system adjustment Grind stake scaling changed")
    return result


def _compile_action_program(
    action: _SourceAction,
    constants: Mapping[str, Any],
) -> dict[str, Any]:
    statement = copy.deepcopy(action.statement)
    _FirstEntryAmountLowerer().visit(statement)
    lowered = _ActionLowerer(none_result="return-none").visit(statement)
    if not isinstance(lowered, ast.stmt):
        raise StrategyAnalysisError("system adjustment action lowering failed")
    fragment = _decision_fragment(
        f"__system_adjustment_{action.kind}_{action.level}",
        [lowered, ast.Return(value=ast.Constant(value="continue"))],
    )
    bindings = _bindings_for_fragment(fragment, action.level)
    program = compile_scalar_ast_program(fragment, constants=dict(constants))
    return {
        "decision_program": program,
        "bindings": bindings,
        "input_contract": _input_contract(fragment),
    }


def _compile_exit_program(
    method: ast.FunctionDef,
    constants: Mapping[str, Any],
) -> dict[str, Any]:
    lowerer = _ActionLowerer(none_result="continue")
    body = copy.deepcopy(method.body)
    lowered = [node for statement in body if (node := lowerer.visit(statement)) is not None]
    fragment = _decision_fragment("__system_adjustment_grind_exit", lowered)
    bindings = _bindings_for_fragment(fragment, None)
    program = compile_scalar_ast_program(fragment, constants=dict(constants))
    return {
        "decision_program": program,
        "bindings": bindings,
        "input_contract": _input_contract(fragment),
    }


class _FirstEntryAmountLowerer(ast.NodeTransformer):
    def visit_Attribute(self, node: ast.Attribute) -> ast.expr:
        if (
            node.attr == "safe_filled"
            and isinstance(node.value, ast.Subscript)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "filled_entries"
            and isinstance(node.value.slice, ast.Constant)
            and node.value.slice.value == 0
        ):
            return ast.copy_location(ast.Name(id="first_entry_amount", ctx=ast.Load()), node)
        visited = self.generic_visit(node)
        if not isinstance(visited, ast.expr):
            raise StrategyAnalysisError("system adjustment attribute lowering failed")
        return visited


class _ActionLowerer(ast.NodeTransformer):
    def __init__(self, *, none_result: str) -> None:
        self.none_result = none_result

    def visit_Expr(self, node: ast.Expr) -> ast.stmt | None:
        if isinstance(node.value, ast.Call) and _call_path(node.value.func) in {
            "send_msg",
            "log.info",
            "self.dp.send_msg",
        }:
            return None
        raise StrategyAnalysisError("system adjustment action contains an uncompiled expression")

    def visit_Assign(self, node: ast.Assign) -> ast.stmt | None:
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"grind_profit", "stake_fmt"}
        ):
            return None
        visited = self.generic_visit(node)
        if not isinstance(visited, ast.stmt):
            raise StrategyAnalysisError("system adjustment assignment lowering failed")
        return visited

    def visit_For(self, node: ast.For) -> None:
        names = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
        if "order_tag" not in names:
            raise StrategyAnalysisError("system adjustment action loop changed")
        return None

    def visit_If(self, node: ast.If) -> ast.stmt:
        if isinstance(node.test, ast.Name) and node.test.id == "has_order_tags":
            if len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
                raise StrategyAnalysisError("system adjustment tagged return changed")
            value = node.body[0].value
            if not isinstance(value, ast.Tuple) or len(value.elts) != 2:
                raise StrategyAnalysisError("system adjustment tagged result changed")
            return ast.copy_location(copy.deepcopy(node.body[0]), node)
        visited = self.generic_visit(node)
        if not isinstance(visited, ast.stmt):
            raise StrategyAnalysisError("system adjustment branch lowering failed")
        return visited

    def visit_Return(self, node: ast.Return) -> ast.Return:
        value = node.value
        if value is None or (isinstance(value, ast.Constant) and value.value is None) or (
            isinstance(value, ast.Tuple)
            and value.elts
            and all(isinstance(item, ast.Constant) and item.value is None for item in value.elts)
        ):
            return ast.copy_location(ast.Return(value=ast.Constant(value=self.none_result)), node)
        return copy.deepcopy(node)


def _decision_fragment(name: str, body: list[ast.stmt]) -> ast.FunctionDef:
    temporary = ast.FunctionDef(
        name=name,
        args=ast.arguments(
            posonlyargs=[],
            args=[],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=body,
        decorator_list=[],
        returns=None,
        type_comment=None,
        type_params=[],
    )
    free = _free_names(temporary)
    temporary.args.args = [ast.arg(arg=name) for name in free]
    ast.fix_missing_locations(temporary)
    return temporary


def _free_names(fragment: ast.FunctionDef) -> list[str]:
    stored = {
        node.id
        for node in ast.walk(fragment)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    loaded = {
        node.id
        for node in ast.walk(fragment)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return sorted(loaded - stored - _BUILTIN_NAMES - {"self"})


def _bindings_for_fragment(
    fragment: ast.FunctionDef,
    action_level: int | None,
) -> list[dict[str, Any]]:
    result = []
    for argument in fragment.args.args:
        name = argument.arg
        common = _COMMON_BINDINGS.get(name)
        if common is not None:
            result.append({"name": name, "kind": common})
            continue
        derisk = _DERISK_VARIABLE.fullmatch(name)
        if derisk is not None:
            result.append({"name": name, "kind": "derisk-found", "level": int(derisk.group(1))})
            continue
        cluster = _GRIND_VARIABLE.fullmatch(name)
        if cluster is not None:
            level = int(cluster.group(1))
            kind = _CLUSTER_BINDINGS.get(cluster.group(2))
            if kind is None or (action_level is not None and level != action_level):
                raise StrategyAnalysisError(f"unsupported system adjustment input: {name}")
            result.append({"name": name, "kind": kind, "level": level})
            continue
        exit_helper = {
            "grind_profit_rate": "cluster-profit-rate",
            "grind_total_amount": "cluster-total-amount",
            "grind_exit_profit_threshold": "cluster-profit-threshold",
            "max_profit": "cluster-maximum-profit-stake",
            "max_profit_rate": "cluster-maximum-profit-rate",
            "grind_profit_stake": "cluster-profit-stake",
        }.get(name)
        if exit_helper is not None and action_level is None:
            result.append({"name": name, "kind": exit_helper, "level": "action"})
            continue
        raise StrategyAnalysisError(f"unsupported system adjustment input: {name}")
    return result


def _returned_or_assigned_tag(statement: ast.If) -> str | None:
    returned = {
        str(node.value.elts[1].value)
        for node in ast.walk(statement)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Tuple)
        and len(node.value.elts) == 2
        and isinstance(node.value.elts[1], ast.Constant)
        and isinstance(node.value.elts[1].value, str)
    }
    assigned = {
        str(node.value.value)
        for node in ast.walk(statement)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "order_tag"
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    tags = returned | assigned
    return next(iter(tags)) if len(tags) == 1 else None


def _exit_call_tag(call: ast.Call) -> str:
    tags = [
        str(argument.value)
        for argument in call.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]
    if len(tags) != 2 or tags[0] != tags[1]:
        raise StrategyAnalysisError("system adjustment Grind exit tags changed")
    return tags[0]


def _validate_exit_call(call: ast.Call, level: int) -> None:
    prefix = f"grind_{level}_"
    names = {
        node.id
        for argument in call.args
        for node in ast.walk(argument)
        if isinstance(node, ast.Name) and node.id.startswith("grind_")
    }
    if not names or any(not name.startswith(prefix) for name in names):
        raise StrategyAnalysisError(f"system adjustment Grind {level} exit bindings changed")


def _input_contract(fragment: ast.FunctionDef) -> dict[str, Any]:
    indexed: dict[str, set[str]] = {}
    for node in ast.walk(fragment):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            indexed.setdefault(node.value.id, set()).add(node.slice.value)
    return {
        "indexed_fields": {
            name: sorted(fields) for name, fields in sorted(indexed.items())
        }
    }


def _merge_input_contracts(actions: list[dict[str, Any]]) -> dict[str, Any]:
    indexed: dict[str, set[str]] = {}
    for action in actions:
        contract = action.get("input_contract")
        fields_by_input = contract.get("indexed_fields") if isinstance(contract, dict) else None
        if not isinstance(fields_by_input, dict):
            continue
        for name, fields in fields_by_input.items():
            if isinstance(name, str) and isinstance(fields, list):
                indexed.setdefault(name, set()).update(
                    field for field in fields if isinstance(field, str)
                )
    return {
        "indexed_fields": {
            name: sorted(fields) for name, fields in sorted(indexed.items())
        }
    }


def _positive_int(record: Mapping[str, Any], name: str) -> int:
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StrategyAnalysisError(f"system adjustment policy {name} is invalid")
    return value


def _call_path(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_path(node.value)
        return f"{parent}.{node.attr}" if parent is not None else None
    return None


def _location(node: ast.AST) -> dict[str, int]:
    return {
        "line": int(getattr(node, "lineno", 0)),
        "column": int(getattr(node, "col_offset", 0)),
        "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 0))),
        "end_column": int(getattr(node, "end_col_offset", getattr(node, "col_offset", 0))),
    }
