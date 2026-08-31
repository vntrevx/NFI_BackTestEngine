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

SYSTEM_ADJUSTMENT_PROGRAM_VERSION = "system-adjustment-program-v2"

_DERISK_TAG = re.compile(r"^derisk_level_(\d+)$")
_GRIND_TAG = re.compile(r"^grind_(\d+)_(entry|exit|derisk)$")
_GRIND_VARIABLE = re.compile(r"^grind_(\d+)_(.+)$")
_DERISK_VARIABLE = re.compile(r"^is_derisk_(\d+)(?:_found)?$")
_DERISK_ENABLE_VARIABLE = re.compile(r"^derisk_(\d+)_enable$")
_DERISK_VALUE_VARIABLE = re.compile(r"^derisk_(\d+)_(stake|threshold)$")
_MAXIMUM_KEY = re.compile(r"^grind_(\d+)_cluster_max_profit_(stake|rate)$")
_CURRENT_MAXIMUM = re.compile(r"^grind_(\d+)_current_grind_profit_(stake|rate)$")
_BUILTIN_NAMES = {"abs", "all", "any", "bool", "float", "int", "len", "max", "min", "str"}

_COMMON_BINDINGS = {
    "current_rate": "current-rate",
    "derisk_enable": "derisk-enabled-global",
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
    feature_aliases = _method_feature_aliases(method)
    runtime_aliases = _runtime_aliases(method)
    exit_program = _compile_exit_program(
        exit_method,
        constants,
        feature_aliases=_exit_feature_aliases(feature_aliases, exit_method),
    )
    constant_aliases = _constant_aliases(method, constants)
    compiled_actions = []
    for action in actions:
        if action.kind == "grind-exit":
            bindings = [
                {
                    **binding,
                    **({"level": action.level} if binding.get("level") == "action" else {}),
                }
                for binding in exit_program["bindings"]
            ]
            level_state = next(
                record for record in order_scan["grind_levels"] if record["level"] == action.level
            )
            if any(
                binding["kind"]
                in {
                    "cluster-maximum-profit-stake",
                    "cluster-maximum-profit-rate",
                }
                for binding in bindings
            ) and (
                level_state["maximum_profit_stake_key"] is None
                or level_state["maximum_profit_rate_key"] is None
            ):
                raise StrategyAnalysisError("system adjustment maximum binding has no source state")
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
        compiled = _compile_action_program(
            action,
            constants,
            constant_aliases=constant_aliases,
            feature_aliases=feature_aliases,
            runtime_aliases=runtime_aliases,
        )
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
        "execution_mode": "primary",
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


def _method_alias_available(method: ast.FunctionDef, method_name: str) -> bool:
    assignments = [
        statement
        for statement in method.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == method_name
    ]
    stores = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Name)
        and isinstance(getattr(node, "ctx", None), ast.Store)
        and node.id == method_name
    ]
    if not assignments and not stores:
        return False
    if (
        len(assignments) != 1
        or len(stores) != 1
        or not isinstance(assignments[0].value, ast.Attribute)
        or not isinstance(assignments[0].value.value, ast.Name)
        or assignments[0].value.value.id != "self"
        or assignments[0].value.attr != method_name
    ):
        raise StrategyAnalysisError(f"system adjustment method alias changed: {method_name}")
    return True


def _is_exit_call(node: ast.AST, method_name: str, *, alias_available: bool) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr == method_name
    ) or (
        alias_available
        and isinstance(node.func, ast.Name)
        and node.func.id == method_name
    )


def _source_actions(method: ast.FunctionDef, exit_method_name: str) -> list[_SourceAction]:
    actions: list[_SourceAction] = []
    exit_alias_available = _method_alias_available(method, exit_method_name)
    for statement in method.body:
        if not isinstance(statement, ast.If):
            continue
        exit_call = next(
            (
                node
                for node in ast.walk(statement)
                if isinstance(node, ast.Call)
                and _is_exit_call(
                    node,
                    exit_method_name,
                    alias_available=exit_alias_available,
                )
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
        f"grind-{kind}:{level}" for level in levels for kind in ("entry", "exit", "derisk")
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
    maxima = _maximum_keys(method, levels, side=side)
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


def _maximum_keys(
    method: ast.FunctionDef,
    levels: list[int],
    *,
    side: str,
) -> dict[int, dict[str, str | None]]:
    reads: dict[tuple[int, str], tuple[str, int]] = {}
    writes: dict[tuple[int, str], int] = {}
    consumed_calls: set[int] = set()
    custom_calls: dict[int, ast.Call] = {}
    maximum_names = {
        (int(match.group(1)), match.group(2))
        for node in ast.walk(method)
        if isinstance(node, ast.Name) and (match := _MAXIMUM_KEY.fullmatch(node.id)) is not None
    }
    for statement in method.body:
        calls = [
            node
            for node in ast.walk(statement)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get_custom_data", "set_custom_data"}
        ]
        names = {
            node.id
            for node in ast.walk(statement)
            if isinstance(node, ast.Name)
            and (
                _MAXIMUM_KEY.fullmatch(node.id) is not None
                or _CURRENT_MAXIMUM.fullmatch(node.id) is not None
            )
        }
        for call in calls:
            if names or _MAXIMUM_KEY.fullmatch(_custom_data_key(call) or "") is not None:
                custom_calls[id(call)] = call
    for statement in method.body:
        read = _maximum_read(statement)
        if read is not None:
            level, field, key, call = read
            identity = (level, field)
            if identity in reads:
                raise StrategyAnalysisError("system adjustment cluster maximum reads changed")
            reads[identity] = (key, id(call))
            consumed_calls.add(id(call))
        write = _maximum_write(statement, side=side)
        if write is not None:
            level, field, call = write
            identity = (level, field)
            if identity in writes:
                raise StrategyAnalysisError("system adjustment cluster maximum writes changed")
            writes[identity] = id(call)
            consumed_calls.add(id(call))
    if set(custom_calls) != consumed_calls:
        raise StrategyAnalysisError("system adjustment custom state shape changed")
    expected_levels = set(levels)
    observed_levels = {level for level, _ in set(reads) | set(writes) | maximum_names}
    if not observed_levels <= expected_levels:
        raise StrategyAnalysisError("system adjustment cluster maximum levels changed")
    result: dict[int, dict[str, str | None]] = {}
    for level in levels:
        identities = {(level, "stake"), (level, "rate")}
        present_reads = identities & set(reads)
        present_writes = identities & set(writes)
        if not present_reads and not present_writes:
            if identities & maximum_names:
                raise StrategyAnalysisError("system adjustment residual maximum state changed")
            result[level] = {"stake": None, "rate": None}
            continue
        if present_reads != identities or present_writes != identities:
            raise StrategyAnalysisError("system adjustment cluster maximum keys changed")
        result[level] = {field: reads[(level, field)][0] for field in ("stake", "rate")}
    return result


def _maximum_read(
    statement: ast.stmt,
) -> tuple[int, str, str, ast.Call] | None:
    if (
        not isinstance(statement, ast.Assign)
        or len(statement.targets) != 1
        or not isinstance(statement.targets[0], ast.Name)
        or not isinstance(statement.value, ast.BoolOp)
        or not isinstance(statement.value.op, ast.Or)
        or len(statement.value.values) != 2
    ):
        return None
    calls = [
        node
        for node in statement.value.values
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_custom_data"
    ]
    defaults = [
        node.value
        for node in statement.value.values
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ]
    if len(calls) != 1 or defaults != [0.0]:
        return None
    call = calls[0]
    key = _custom_data_key(call)
    match = _MAXIMUM_KEY.fullmatch(key or "")
    if match is None or statement.targets[0].id != key:
        return None
    return int(match.group(1)), match.group(2), str(key), call


def _maximum_write(
    statement: ast.stmt,
    *,
    side: str,
) -> tuple[int, str, ast.Call] | None:
    if (
        not isinstance(statement, ast.If)
        or not isinstance(statement.test, ast.Compare)
        or len(statement.test.ops) != 1
        or len(statement.test.comparators) != 1
        or not isinstance(statement.test.left, ast.Name)
        or not isinstance(statement.test.comparators[0], ast.Name)
        or len(statement.body) != 1
        or not isinstance(statement.body[0], ast.Expr)
        or not isinstance(statement.body[0].value, ast.Call)
    ):
        return None
    call = statement.body[0].value
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "set_custom_data":
        return None
    key = _custom_data_key(call)
    match = _MAXIMUM_KEY.fullmatch(key or "")
    if match is None:
        return None
    level = int(match.group(1))
    field = match.group(2)
    current_name = f"grind_{level}_current_grind_profit_{field}"
    maximum_name = str(key)
    expected_operator = ast.Gt if field == "stake" or side == "long" else ast.Lt
    value = next(
        (keyword.value for keyword in call.keywords if keyword.arg == "value"),
        None,
    )
    if (
        statement.test.left.id != current_name
        or statement.test.comparators[0].id != maximum_name
        or not isinstance(statement.test.ops[0], expected_operator)
        or not isinstance(value, ast.Name)
        or value.id != current_name
    ):
        return None
    return level, field, call


def _custom_data_key(call: ast.Call) -> str | None:
    if (
        not isinstance(call.func, ast.Attribute)
        or not isinstance(call.func.value, ast.Name)
        or call.func.value.id != "trade"
    ):
        return None
    return next(
        (
            str(keyword.value.value)
            for keyword in call.keywords
            if keyword.arg == "key"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ),
        None,
    )


def _stake_scales(method: ast.FunctionDef, levels: list[int]) -> dict[int, str]:
    result: dict[int, str] = {}
    for statement in method.body:
        if (
            not isinstance(statement, ast.Assign)
            or len(statement.targets) != 1
            or not isinstance(statement.targets[0], ast.Name)
            or (match := re.fullmatch(r"grind_(\d+)_stakes", statement.targets[0].id)) is None
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


def _constant_aliases(
    method: ast.FunctionDef,
    constants: Mapping[str, Any],
) -> frozenset[str]:
    aliases = set()
    for statement in method.body:
        if (
            not isinstance(statement, ast.Assign)
            or len(statement.targets) != 1
            or not isinstance(statement.targets[0], ast.Name)
            or (name := statement.targets[0].id) not in constants
            or not name.startswith("system_v3_")
            or not isinstance(statement.value, ast.Attribute)
            or not isinstance(statement.value.value, ast.Name)
            or statement.value.value.id != "self"
            or statement.value.attr != name
        ):
            continue
        stores = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Name)
            and isinstance(getattr(node, "ctx", None), ast.Store)
            and node.id == name
        ]
        if len(stores) == 1:
            aliases.add(name)
    return frozenset(aliases)


class _ConstantAliasLowerer(ast.NodeTransformer):
    def __init__(self, aliases: frozenset[str]) -> None:
        self.aliases = aliases

    def visit_Name(self, node: ast.Name) -> ast.expr:
        if node.id not in self.aliases or not isinstance(
            getattr(node, "ctx", None), ast.Load
        ):
            return node
        return ast.copy_location(
            ast.Attribute(
                value=ast.Name(id="self", ctx=ast.Load()),
                attr=node.id,
                ctx=ast.Load(),
            ),
            node,
        )


def _compile_action_program(
    action: _SourceAction,
    constants: Mapping[str, Any],
    *,
    constant_aliases: frozenset[str],
    feature_aliases: Mapping[str, str],
    runtime_aliases: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    statement = copy.deepcopy(action.statement)
    _ConstantAliasLowerer(constant_aliases).visit(statement)
    _ExitFeatureAliasLowerer(feature_aliases).visit(statement)
    _RuntimeAliasLowerer(runtime_aliases).visit(statement)
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


def _method_feature_aliases(method: ast.FunctionDef) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for statement in method.body:
        if (
            not isinstance(statement, ast.Assign)
            or len(statement.targets) != 1
            or not isinstance(statement.targets[0], ast.Name)
            or not statement.targets[0].id.startswith("last_")
            or statement.targets[0].id == "last_candle"
            or not isinstance(statement.value, ast.Subscript)
            or not isinstance(statement.value.value, ast.Name)
            or statement.value.value.id != "last_candle"
            or not isinstance(statement.value.slice, ast.Constant)
            or not isinstance(statement.value.slice.value, str)
            or not statement.value.slice.value
        ):
            continue
        name = statement.targets[0].id
        stores = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Name)
            and isinstance(getattr(node, "ctx", None), ast.Store)
            and node.id == name
        ]
        if len(stores) == 1:
            aliases[name] = statement.value.slice.value
    return aliases


def _runtime_aliases(method: ast.FunctionDef) -> dict[str, tuple[str, str]]:
    expected = {
        "trade_is_short": ("trade", "is_short"),
        "trade_liquidation_price": ("trade", "liquidation_price"),
    }
    aliases: dict[str, tuple[str, str]] = {}
    for name, identity in expected.items():
        assignments = [
            statement
            for statement in method.body
            if isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == name
        ]
        stores = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Name)
            and isinstance(getattr(node, "ctx", None), ast.Store)
            and node.id == name
        ]
        value = assignments[0].value if len(assignments) == 1 else None
        if (
            len(stores) == 1
            and isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and (value.value.id, value.attr) == identity
        ):
            aliases[name] = identity
    return aliases


class _RuntimeAliasLowerer(ast.NodeTransformer):
    def __init__(self, aliases: Mapping[str, tuple[str, str]]) -> None:
        self.aliases = aliases

    def visit_Name(self, node: ast.Name) -> ast.expr:
        identity = self.aliases.get(node.id)
        if identity is None or not isinstance(getattr(node, "ctx", None), ast.Load):
            return node
        return ast.copy_location(
            ast.Attribute(
                value=ast.Name(id=identity[0], ctx=ast.Load()),
                attr=identity[1],
                ctx=ast.Load(),
            ),
            node,
        )


def _exit_feature_aliases(
    aliases: Mapping[str, str],
    exit_method: ast.FunctionDef,
) -> dict[str, str]:
    parameters = {
        argument.arg
        for argument in exit_method.args.args
        if argument.arg.startswith("last_") and argument.arg != "last_candle"
    }
    missing = sorted(parameters - aliases.keys())
    if missing:
        raise StrategyAnalysisError(
            "system adjustment exit feature aliases changed: " + ", ".join(missing)
        )
    return {name: aliases[name] for name in sorted(parameters)}


class _ExitFeatureAliasLowerer(ast.NodeTransformer):
    def __init__(self, aliases: Mapping[str, str]) -> None:
        self.aliases = aliases

    def visit_Name(self, node: ast.Name) -> ast.expr:
        feature = self.aliases.get(node.id)
        if feature is None or not isinstance(getattr(node, "ctx", None), ast.Load):
            return node
        return ast.copy_location(
            ast.Subscript(
                value=ast.Name(id="last_candle", ctx=ast.Load()),
                slice=ast.Constant(value=feature),
                ctx=ast.Load(),
            ),
            node,
        )


def _compile_exit_program(
    method: ast.FunctionDef,
    constants: Mapping[str, Any],
    *,
    feature_aliases: Mapping[str, str],
) -> dict[str, Any]:
    alias_lowerer = _ExitFeatureAliasLowerer(feature_aliases)
    body = [
        alias_lowerer.visit(statement)
        for statement in copy.deepcopy(method.body)
    ]
    lowerer = _ActionLowerer(
        none_result="continue",
        observability_only_locals=_observability_only_config_locals(body),
    )
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


_DISCARDED_REPORTING_LOCALS = frozenset({"grind_profit", "stake_fmt"})
_DISCARDED_REPORTING_CALLS = frozenset({"send_msg", "log.info", "self.dp.send_msg"})


def _observability_only_config_locals(body: list[ast.stmt]) -> frozenset[str]:
    module = ast.Module(body=body, type_ignores=[])
    parents = {
        id(child): parent for parent in ast.walk(module) for child in ast.iter_child_nodes(parent)
    }
    candidates = {
        target.id
        for node in ast.walk(module)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
        and isinstance(node.value, ast.Subscript)
        and isinstance(node.value.value, ast.Attribute)
        and isinstance(node.value.value.value, ast.Name)
        and node.value.value.value.id == "self"
        and node.value.value.attr == "config"
        and isinstance(node.value.slice, ast.Constant)
        and isinstance(node.value.slice.value, str)
    }
    result: set[str] = set()
    for candidate in candidates:
        loads = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id == candidate
        ]
        if all(_load_is_discarded(node, parents) for node in loads):
            result.add(candidate)
    return frozenset(result)


def _load_is_discarded(node: ast.Name, parents: Mapping[int, ast.AST]) -> bool:
    current: ast.AST = node
    while (parent := parents.get(id(current))) is not None:
        if (
            isinstance(parent, ast.Expr)
            and isinstance(parent.value, ast.Call)
            and _call_path(parent.value.func) in _DISCARDED_REPORTING_CALLS
        ):
            return True
        if (
            isinstance(parent, ast.Assign)
            and len(parent.targets) == 1
            and isinstance(parent.targets[0], ast.Name)
            and parent.targets[0].id in _DISCARDED_REPORTING_LOCALS
        ):
            return True
        current = parent
    return False


class _ActionLowerer(ast.NodeTransformer):
    def __init__(
        self,
        *,
        none_result: str,
        observability_only_locals: frozenset[str] = frozenset(),
    ) -> None:
        self.none_result = none_result
        self.observability_only_locals = observability_only_locals

    def visit_Expr(self, node: ast.Expr) -> ast.stmt | None:
        if (
            isinstance(node.value, ast.Call)
            and _call_path(node.value.func) in _DISCARDED_REPORTING_CALLS
        ):
            return None
        raise StrategyAnalysisError("system adjustment action contains an uncompiled expression")

    def visit_Assign(self, node: ast.Assign) -> ast.stmt | None:
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in _DISCARDED_REPORTING_LOCALS | self.observability_only_locals
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

    def visit_If(self, node: ast.If) -> ast.stmt | None:
        if (
            not node.orelse
            and node.body
            and all(
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and _call_path(statement.value.func) in _DISCARDED_REPORTING_CALLS
                for statement in node.body
            )
        ):
            return None
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
        if (
            value is None
            or (isinstance(value, ast.Constant) and value.value is None)
            or (
                isinstance(value, ast.Tuple)
                and value.elts
                and all(
                    isinstance(item, ast.Constant) and item.value is None for item in value.elts
                )
            )
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
        derisk_enable = _DERISK_ENABLE_VARIABLE.fullmatch(name)
        if derisk_enable is not None:
            level = int(derisk_enable.group(1))
            if action_level is not None and level != action_level:
                raise StrategyAnalysisError(f"unsupported system adjustment input: {name}")
            result.append({"name": name, "kind": "derisk-enabled", "level": level})
            continue
        derisk_value = _DERISK_VALUE_VARIABLE.fullmatch(name)
        if derisk_value is not None:
            level = int(derisk_value.group(1))
            if action_level is not None and level != action_level:
                raise StrategyAnalysisError(f"unsupported system adjustment input: {name}")
            result.append(
                {
                    "name": name,
                    "kind": f"derisk-{derisk_value.group(2)}",
                    "level": level,
                }
            )
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
    return {"indexed_fields": {name: sorted(fields) for name, fields in sorted(indexed.items())}}


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
    return {"indexed_fields": {name: sorted(fields) for name, fields in sorted(indexed.items())}}


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
