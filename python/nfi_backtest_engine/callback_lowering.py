"""Fail-closed structural lowering for strategy callbacks.

The compiler recognizes only source shapes whose backtest behavior can be
proven without executing user Python in the candle loop. Near misses remain
uncompiled.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .errors import StrategyAnalysisError
from .trade_ir import build_trade_dependency_ir

CALLBACK_LOWERING_VERSION = "1.7.0"


def lower_strategy_callbacks(
    analysis: dict[str, Any],
    *,
    run_mode: str | None,
    config: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return exact callback lowerings keyed by callback name."""
    strategies = analysis.get("strategies")
    source = analysis.get("source")
    if not isinstance(strategies, list) or len(strategies) != 1 or not isinstance(source, dict):
        raise StrategyAnalysisError("callback lowering requires one selected strategy")
    source_path = source.get("path")
    source_sha256 = source.get("sha256")
    if not isinstance(source_path, str) or not isinstance(source_sha256, str):
        raise StrategyAnalysisError("callback lowering requires a hash-bound source")
    path = Path(source_path).resolve()
    try:
        source_bytes = path.read_bytes()
        source_text = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StrategyAnalysisError(f"callback lowering source cannot be read: {path}") from exc
    if hashlib.sha256(source_bytes).hexdigest() != source_sha256:
        raise StrategyAnalysisError("callback lowering source hash differs from analysis")
    try:
        tree = ast.parse(source_text, filename=str(path), type_comments=True)
    except SyntaxError as exc:  # pragma: no cover - analysis already parsed this source
        raise StrategyAnalysisError("callback lowering source no longer parses") from exc

    strategy_name = strategies[0].get("name")
    strategy_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy_name
        ),
        None,
    )
    if strategy_node is None:
        raise StrategyAnalysisError("callback lowering strategy class disappeared")

    lowered: dict[str, dict[str, Any]] = {}
    constants = strategies[0].get("constants", {})
    if not isinstance(constants, dict):
        raise StrategyAnalysisError("callback lowering strategy constants are invalid")
    effective_constants = dict(constants)
    if config is not None:
        # NFI's safe configuration wrapper copies these top-level values onto
        # the strategy instance before a backtest starts. Freeze the same
        # effective values into callback IR so Rust never observes stale class
        # defaults. Only fields consumed by reviewed lowerers belong here.
        for name in (
            "exit_profit_only",
            "exit_profit_offset",
            "futures_mode_leverage",
            "futures_mode_leverage_rebuy_mode",
            "futures_mode_leverage_grind_mode",
        ):
            value = config.get(name)
            if isinstance(value, bool | int | float):
                effective_constants[name] = value
    method_nodes = {
        node.name: node
        for node in strategy_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for node in method_nodes.values():
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        descriptor = _lower_callback(
            node,
            run_mode=run_mode,
            constants=effective_constants,
            method_nodes=method_nodes,
        )
        if descriptor is not None:
            lowered[node.name] = descriptor
    scalar_callbacks = {
        "adjust_trade_position": (
            "rust-adjustment-vm",
            "adjust-trade-position-scalar-bundle-v1",
        ),
        "custom_exit": (
            "rust-custom-exit-vm",
            "custom-exit-scalar-bundle-v1",
        ),
    }
    for callback_name, (backend, opcode) in scalar_callbacks.items():
        if callback_name not in method_nodes or callback_name in lowered:
            continue
        descriptor = _lower_scalar_trade_callback(
            analysis,
            callback_name=callback_name,
            backend=backend,
            opcode=opcode,
        )
        if descriptor is not None:
            lowered[callback_name] = descriptor
    return lowered


def _lower_scalar_trade_callback(
    analysis: dict[str, Any],
    *,
    callback_name: str,
    backend: str,
    opcode: str,
) -> dict[str, Any] | None:
    report = build_trade_dependency_ir(analysis, roots=(callback_name,))
    compiled = report.get("compiled_scalar_methods")
    if not isinstance(compiled, dict) or callback_name not in compiled:
        return None
    pending = [callback_name]
    selected: dict[str, Any] = {}
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        record = compiled.get(name)
        if not isinstance(record, dict) or not isinstance(record.get("program"), dict):
            return None
        selected[name] = record["program"]
        called_methods = record.get("called_methods", [])
        if not isinstance(called_methods, list) or not all(
            isinstance(item, str) for item in called_methods
        ):
            return None
        pending.extend(called_methods)
    operation = {
        "opcode": opcode,
        "schema_version": "1.0.0",
        "entry": callback_name,
        "programs": {name: selected[name] for name in sorted(selected)},
    }
    return {
        "backend": backend,
        "executable_in_rust": True,
        "operation": operation,
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": f"transitive-scalar-{callback_name.replace('_', '-')}-v1",
            "trade_ir_fingerprint": report["fingerprint"],
            "program_count": len(selected),
            "program_sha256": hashlib.sha256(
                json.dumps(
                    operation,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
    }


def _lower_callback(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    run_mode: str | None,
    constants: dict[str, Any],
    method_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> dict[str, Any] | None:
    if node.name == "bot_loop_start":
        return _lower_backtest_bot_loop_start(node, run_mode=run_mode)
    if node.name == "order_filled":
        return _lower_x7_order_filled(node, constants=constants)
    if node.name == "custom_stake_amount":
        return _lower_custom_stake_amount(node, constants=constants)
    if node.name == "leverage":
        return _lower_x7_leverage(node, constants=constants)
    if node.name == "confirm_trade_entry":
        return _lower_confirm_trade_entry(
            node,
            constants=constants,
            method_nodes=method_nodes,
        )
    if node.name == "confirm_trade_exit":
        return _lower_confirm_trade_exit(
            node,
            constants=constants,
            method_nodes=method_nodes,
            run_mode=run_mode,
        )
    return None


def _lower_x7_leverage(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    constants: dict[str, Any],
) -> dict[str, Any] | None:
    """Freeze NFI X7's bounded tag-to-leverage callback.

    The matcher intentionally accepts one source shape only: split the entry
    tag, test the reviewed long rebuy and grind tag lists in that order, then
    return the default. A new condition, external call, or changed precedence
    remains uncompiled until it is reviewed.
    """
    if isinstance(node, ast.AsyncFunctionDef) or len(node.body) != 5:
        return None
    if not _is_name_call_assignment(node.body[0], "enter_tags", "entry_tag", "split"):
        return None
    if not _is_self_attribute_assignment(
        node.body[1],
        target="long_rebuy_mode_tags",
        attribute="long_rebuy_mode_tags",
    ):
        return None
    if not _is_self_attribute_assignment(
        node.body[2],
        target="long_grind_mode_tags",
        attribute="long_grind_mode_tags",
    ):
        return None
    branch = node.body[3]
    if (
        not isinstance(branch, ast.If)
        or not _is_all_tag_membership(
            branch.test,
            tags_name="long_rebuy_mode_tags",
            values_name="enter_tags",
        )
        or len(branch.body) != 1
        or not _is_return_self_attribute(
            branch.body[0],
            "futures_mode_leverage_rebuy_mode",
        )
        or len(branch.orelse) != 1
        or not isinstance(branch.orelse[0], ast.If)
    ):
        return None
    grind_branch = branch.orelse[0]
    if (
        not _is_all_tag_membership(
            grind_branch.test,
            tags_name="long_grind_mode_tags",
            values_name="enter_tags",
        )
        or len(grind_branch.body) != 1
        or not _is_return_self_attribute(
            grind_branch.body[0],
            "futures_mode_leverage_grind_mode",
        )
        or grind_branch.orelse
        or not _is_return_self_attribute(node.body[4], "futures_mode_leverage")
    ):
        return None

    names = (
        "futures_mode_leverage",
        "futures_mode_leverage_rebuy_mode",
        "futures_mode_leverage_grind_mode",
    )
    values: dict[str, float] = {}
    for name in names:
        value = constants.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        numeric = float(value)
        if not math.isfinite(numeric) or numeric <= 0.0:
            return None
        values[name] = numeric
    tag_lists: dict[str, list[str]] = {}
    for name in ("long_rebuy_mode_tags", "long_grind_mode_tags"):
        raw = constants.get(name)
        if (
            not isinstance(raw, list)
            or not raw
            or not all(isinstance(tag, str) and tag for tag in raw)
        ):
            return None
        tag_lists[name] = list(dict.fromkeys(raw))

    operation = {
        "opcode": "nfi-x7-leverage-v1",
        "default": values["futures_mode_leverage"],
        "ordered_tag_overrides": [
            {
                "entry_tags": tag_lists["long_rebuy_mode_tags"],
                "leverage": values["futures_mode_leverage_rebuy_mode"],
            },
            {
                "entry_tags": tag_lists["long_grind_mode_tags"],
                "leverage": values["futures_mode_leverage_grind_mode"],
            },
        ],
    }
    return {
        "backend": "rust-nfi-x7-leverage",
        "executable_in_rust": True,
        "operation": operation,
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": "nfi-x7-ordered-tag-leverage-v1",
            "ast_sha256": hashlib.sha256(
                ast.dump(node, annotate_fields=True, include_attributes=False).encode()
            ).hexdigest(),
            "effective_values": values,
        },
    }


def _is_name_call_assignment(
    statement: ast.stmt,
    target: str,
    receiver: str,
    method: str,
) -> bool:
    return (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == target
        and isinstance(statement.value, ast.Call)
        and not statement.value.args
        and not statement.value.keywords
        and isinstance(statement.value.func, ast.Attribute)
        and statement.value.func.attr == method
        and isinstance(statement.value.func.value, ast.Name)
        and statement.value.func.value.id == receiver
    )


def _is_self_attribute_assignment(
    statement: ast.stmt,
    *,
    target: str,
    attribute: str,
) -> bool:
    return (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == target
        and isinstance(statement.value, ast.Attribute)
        and statement.value.attr == attribute
        and isinstance(statement.value.value, ast.Name)
        and statement.value.value.id == "self"
    )


def _is_all_tag_membership(
    expression: ast.expr,
    *,
    tags_name: str,
    values_name: str,
) -> bool:
    if (
        not isinstance(expression, ast.Call)
        or not isinstance(expression.func, ast.Name)
        or expression.func.id != "all"
        or len(expression.args) != 1
        or expression.keywords
        or not isinstance(expression.args[0], ast.GeneratorExp)
    ):
        return False
    generator = expression.args[0]
    if (
        len(generator.generators) != 1
        or not isinstance(generator.elt, ast.Compare)
        or len(generator.elt.ops) != 1
        or not isinstance(generator.elt.ops[0], ast.In)
        or len(generator.elt.comparators) != 1
    ):
        return False
    clause = generator.generators[0]
    return (
        isinstance(clause.target, ast.Name)
        and clause.target.id == "c"
        and isinstance(clause.iter, ast.Name)
        and clause.iter.id == values_name
        and not clause.ifs
        and clause.is_async == 0
        and isinstance(generator.elt.left, ast.Name)
        and generator.elt.left.id == "c"
        and isinstance(generator.elt.comparators[0], ast.Name)
        and generator.elt.comparators[0].id == tags_name
    )


def _is_return_self_attribute(statement: ast.stmt, attribute: str) -> bool:
    return (
        isinstance(statement, ast.Return)
        and isinstance(statement.value, ast.Attribute)
        and statement.value.attr == attribute
        and isinstance(statement.value.value, ast.Name)
        and statement.value.value.id == "self"
    )


def _lower_backtest_bot_loop_start(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    run_mode: str | None,
) -> dict[str, Any] | None:
    if (
        isinstance(node, ast.AsyncFunctionDef)
        or run_mode not in {"backtest", "hyperopt"}
        or not node.body
    ):
        return None
    first = node.body[0]
    if not isinstance(first, ast.If):
        return None
    excluded_modes = _runmode_not_in_values(first.test)
    if excluded_modes is None or run_mode in excluded_modes:
        return None
    if len(first.body) != 1 or not _is_base_callback_return(
        first.body[0],
        "bot_loop_start",
    ):
        return None
    return {
        "backend": "rust-noop",
        "executable_in_rust": True,
        "operation": {
            "opcode": "noop",
            "reason": "backtest branch delegates directly to the Freqtrade base callback",
        },
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": "runmode-not-in-base-delegation-v1",
            "run_mode": run_mode,
            "excluded_modes": sorted(excluded_modes),
            "first_statement_line": first.lineno,
        },
    }


def _runmode_not_in_values(node: ast.AST) -> set[str] | None:
    if (
        not isinstance(node, ast.Compare)
        or len(node.ops) != 1
        or not isinstance(node.ops[0], ast.NotIn)
        or len(node.comparators) != 1
        or not _is_self_config_runmode_value(node.left)
    ):
        return None
    comparator = node.comparators[0]
    if not isinstance(comparator, ast.Tuple | ast.List | ast.Set):
        return None
    values: set[str] = set()
    for item in comparator.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        values.add(item.value)
    return values


def _is_self_config_runmode_value(node: ast.AST) -> bool:
    if not isinstance(node, ast.Attribute) or node.attr != "value":
        return False
    subscription = node.value
    return (
        isinstance(subscription, ast.Subscript)
        and isinstance(subscription.value, ast.Attribute)
        and subscription.value.attr == "config"
        and isinstance(subscription.value.value, ast.Name)
        and subscription.value.value.id == "self"
        and isinstance(subscription.slice, ast.Constant)
        and subscription.slice.value == "runmode"
    )


def _is_base_callback_return(node: ast.AST, callback: str) -> bool:
    if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
        return False
    function = node.value.func
    return (
        isinstance(function, ast.Attribute)
        and function.attr == callback
        and isinstance(function.value, ast.Call)
        and isinstance(function.value.func, ast.Name)
        and function.value.func.id == "super"
        and not function.value.args
        and not function.value.keywords
    )


def _lower_x7_order_filled(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    constants: dict[str, Any],
) -> dict[str, Any] | None:
    """Lower the bounded X7 custom-data state machine without a source hash."""
    if isinstance(node, ast.AsyncFunctionDef):
        return None
    forbidden = (
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.Try,
        ast.With,
        ast.AsyncWith,
        ast.Match,
        ast.Raise,
        ast.Delete,
        ast.AugAssign,
        ast.Yield,
        ast.YieldFrom,
        ast.Await,
        ast.Lambda,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
    )
    if any(isinstance(item, forbidden) for item in ast.walk(node)):
        return None
    if any(
        isinstance(item, ast.Return) and not _is_none_expression(item.value)
        for item in ast.walk(node)
    ):
        return None
    allowed_calls = {
        "len",
        "order_tag.split",
        "set_custom_data",
        "trade.select_filled_orders",
        "trade.set_custom_data",
    }
    if any(
        _qualified_name(item.func) not in allowed_calls
        for item in ast.walk(node)
        if isinstance(item, ast.Call)
    ):
        return None

    body = list(node.body)
    environment: dict[str, Any] = {}
    while body:
        statement = body[0]
        if not isinstance(statement, ast.Assign):
            break
        if not _record_static_alias(statement, environment, constants):
            return None
        del body[0]
    if len(body) != 3:
        return None
    first_entry = body[0]
    system_branch = body[1]
    final_return = body[2]
    if not isinstance(first_entry, ast.If) or not isinstance(system_branch, ast.If):
        return None
    if not _is_first_successful_entry_test(first_entry.test):
        return None
    if not isinstance(final_return, ast.Return) or not _is_none_expression(final_return.value):
        return None
    if len(first_entry.body) != 1 or not isinstance(first_entry.body[0], ast.If):
        return None

    selected_initial = _select_static_if(first_entry.body[0], environment)
    if selected_initial is None:
        return None
    initial_writes = _literal_write_block(selected_initial, environment, constants)
    if initial_writes is None:
        return None
    selected_system = constants.get("system_name_use")
    if not isinstance(selected_system, str) or not any(
        write == {"key": "system_version", "value": selected_system} for write in initial_writes
    ):
        return None

    system_selected = _static_bool(system_branch.test, environment)
    if system_selected is None:
        return None
    tag_actions: dict[str, list[dict[str, Any]]] = {}
    if system_selected:
        tag_actions = _extract_order_tag_actions(
            system_branch.body,
            environment,
            constants,
        )
        if not tag_actions:
            return None

    all_writes = {
        id(item)
        for branch in (first_entry, system_branch)
        for item in ast.walk(branch)
        if isinstance(item, ast.Call) and _is_set_custom_data_call(item)
    }
    outside_writes = {
        id(item)
        for statement in (*node.body[: len(node.body) - 3], final_return)
        for item in ast.walk(statement)
        if isinstance(item, ast.Call) and _is_set_custom_data_call(item)
    }
    if outside_writes or not all_writes:
        return None

    return {
        "backend": "rust-order-state",
        "executable_in_rust": True,
        "operation": {
            "opcode": "order-filled-state-v1",
            "initial_successful_entry_writes": initial_writes,
            "order_tag_actions": tag_actions,
        },
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": "x7-static-system-order-state-v1",
            "selected_system": selected_system,
            "literal_write_sites": len(all_writes),
            "program_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "initial": initial_writes,
                        "actions": tag_actions,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
    }


def _record_static_alias(
    statement: ast.Assign,
    environment: dict[str, Any],
    constants: dict[str, Any],
) -> bool:
    if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
        return False
    name = statement.targets[0].id
    value = statement.value
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "self"
        and value.attr in constants
    ):
        environment[name] = constants[value.attr]
        return True
    if (
        name == "set_custom_data"
        and isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "trade"
        and value.attr == "set_custom_data"
    ):
        environment[name] = "trade.set_custom_data"
        return True
    return False


def _is_first_successful_entry_test(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
        and isinstance(node.left, ast.Attribute)
        and isinstance(node.left.value, ast.Name)
        and node.left.value.id == "trade"
        and node.left.attr == "nr_of_successful_entries"
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value == 1
    )


def _select_static_if(
    node: ast.If,
    environment: dict[str, Any],
) -> list[ast.stmt] | None:
    selected = _static_bool(node.test, environment)
    if selected is None:
        return None
    statements = node.body if selected else node.orelse
    if len(statements) == 1 and isinstance(statements[0], ast.If):
        return _select_static_if(statements[0], environment)
    return statements


def _static_bool(node: ast.AST, environment: dict[str, Any]) -> bool | None:
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and len(node.comparators) == 1
        and isinstance(node.ops[0], ast.Eq | ast.NotEq)
    ):
        left = _environment_value(node.left, environment)
        right = _environment_value(node.comparators[0], environment)
        if left is _UNKNOWN or right is _UNKNOWN:
            return None
        equal = left == right
        return equal if isinstance(node.ops[0], ast.Eq) else not equal
    return None


def _environment_value(node: ast.AST, environment: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        return environment.get(node.id, _UNKNOWN)
    if isinstance(node, ast.Constant):
        return node.value
    return _UNKNOWN


def _literal_write_block(
    statements: list[ast.stmt],
    environment: dict[str, Any],
    constants: dict[str, Any],
) -> list[dict[str, Any]] | None:
    writes: list[dict[str, Any]] = []
    for statement in statements:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            return None
        write = _literal_write(statement.value, environment, constants)
        if write is None:
            return None
        writes.append(write)
    return writes


def _literal_write(
    call: ast.Call,
    environment: dict[str, Any],
    constants: dict[str, Any],
) -> dict[str, Any] | None:
    if not _is_set_custom_data_call(call) or call.args:
        return None
    keywords = {item.arg: item.value for item in call.keywords if item.arg is not None}
    if set(keywords) != {"key", "value"}:
        return None
    key = _literal_value(keywords["key"], environment, constants)
    value = _literal_value(keywords["value"], environment, constants)
    if not isinstance(key, str) or value is _UNKNOWN:
        return None
    if value is not None and not isinstance(value, bool | int | float | str):
        return None
    return {"key": key, "value": value}


def _literal_value(
    node: ast.AST,
    environment: dict[str, Any],
    constants: dict[str, Any],
) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return environment.get(node.id, _UNKNOWN)
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return constants.get(node.attr, _UNKNOWN)
    return _UNKNOWN


def _extract_order_tag_actions(
    statements: list[ast.stmt],
    environment: dict[str, Any],
    constants: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    effectful = [
        statement
        for statement in statements
        if any(
            isinstance(item, ast.Call) and _is_set_custom_data_call(item)
            for item in ast.walk(statement)
        )
    ]
    if len(effectful) != 1 or not isinstance(effectful[0], ast.If):
        return {}
    if not any(_is_none_order_tag_return(item) for item in statements):
        return {}
    actions: dict[str, list[dict[str, Any]]] = {}
    current: ast.If | None = effectful[0]
    while current is not None:
        modes = _order_mode_values(current.test)
        writes = _literal_write_block(current.body, environment, constants)
        if modes is None or writes is None or not writes:
            return {}
        for mode in modes:
            if mode in actions:
                return {}
            actions[mode] = writes
        if not current.orelse:
            current = None
        elif len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
            current = current.orelse[0]
        else:
            return {}
    return dict(sorted(actions.items()))


def _is_none_order_tag_return(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Is)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "order_tag"
        and len(node.test.comparators) == 1
        and _is_none_expression(node.test.comparators[0])
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Return)
        and _is_none_expression(node.body[0].value)
    )


def _order_mode_values(node: ast.AST) -> list[str] | None:
    if (
        not isinstance(node, ast.Compare)
        or len(node.ops) != 1
        or not isinstance(node.ops[0], ast.In)
        or len(node.comparators) != 1
        or not isinstance(node.left, ast.Name)
        or node.left.id != "order_mode"
        or not isinstance(node.comparators[0], ast.List | ast.Tuple | ast.Set)
    ):
        return None
    values = []
    for item in node.comparators[0].elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        values.append(item.value)
    return values


def _is_set_custom_data_call(call: ast.Call) -> bool:
    return _qualified_name(call.func) in {"set_custom_data", "trade.set_custom_data"}


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _is_none_expression(node: ast.AST | None) -> bool:
    return node is None or (isinstance(node, ast.Constant) and node.value is None)


_UNKNOWN = object()


def _lower_custom_stake_amount(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    constants: dict[str, Any],
) -> dict[str, Any] | None:
    if isinstance(node, ast.AsyncFunctionDef):
        return None
    nested_functions = [
        statement for statement in node.body if isinstance(statement, ast.FunctionDef)
    ]
    if len(nested_functions) != 1 or not _is_scaled_stake_helper(nested_functions[0]):
        return None
    statements = [
        statement for statement in node.body if not isinstance(statement, ast.FunctionDef)
    ]
    program = _compile_stake_statements(statements, constants=constants)
    if program is None:
        return None
    program_identity = {
        "opcode": "custom-stake-program-v1",
        "statements": program,
    }
    return {
        "backend": "rust-stake-vm",
        "executable_in_rust": True,
        "operation": program_identity,
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": "bounded-stake-ast-v1",
            "statement_count": len(program),
            "program_sha256": hashlib.sha256(
                json.dumps(
                    program_identity,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
    }


def _is_scaled_stake_helper(node: ast.FunctionDef) -> bool:
    if (
        node.name != "scaled_stake"
        or len(node.args.args) != 1
        or node.args.args[0].arg != "stake_multiplier"
        or len(node.body) != 2
        or not isinstance(node.body[0], ast.Assign)
        or not isinstance(node.body[1], ast.Return)
    ):
        return False
    assignment = node.body[0]
    if (
        len(assignment.targets) != 1
        or not isinstance(assignment.targets[0], ast.Name)
        or assignment.targets[0].id != "stake"
        or not isinstance(assignment.value, ast.BinOp)
        or not isinstance(assignment.value.op, ast.Mult)
        or not isinstance(assignment.value.left, ast.Name)
        or assignment.value.left.id != "proposed_stake"
        or not isinstance(assignment.value.right, ast.Name)
        or assignment.value.right.id != "stake_multiplier"
    ):
        return False
    value = node.body[1].value
    return (
        isinstance(value, ast.IfExp)
        and isinstance(value.test, ast.Compare)
        and len(value.test.ops) == 1
        and isinstance(value.test.ops[0], ast.Gt)
        and isinstance(value.test.left, ast.Name)
        and value.test.left.id == "stake"
        and len(value.test.comparators) == 1
        and isinstance(value.test.comparators[0], ast.Name)
        and value.test.comparators[0].id == "min_stake"
        and isinstance(value.body, ast.Name)
        and value.body.id == "stake"
        and isinstance(value.orelse, ast.Name)
        and value.orelse.id == "min_stake"
    )


def _compile_stake_statements(
    statements: list[ast.stmt],
    *,
    constants: dict[str, Any],
) -> list[dict[str, Any]] | None:
    compiled: list[dict[str, Any]] = []
    for statement in statements:
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                return None
            value = _compile_stake_expression(statement.value, constants=constants)
            if value is None:
                return None
            compiled.append(
                {
                    "op": "let",
                    "name": statement.targets[0].id,
                    "value": value,
                }
            )
        elif isinstance(statement, ast.If):
            condition = _compile_stake_expression(statement.test, constants=constants)
            body = _compile_stake_statements(statement.body, constants=constants)
            otherwise = _compile_stake_statements(statement.orelse, constants=constants)
            if condition is None or body is None or otherwise is None:
                return None
            compiled.append(
                {
                    "op": "if",
                    "condition": condition,
                    "then": body,
                    "otherwise": otherwise,
                }
            )
        elif isinstance(statement, ast.For):
            if (
                not isinstance(statement.target, ast.Name)
                or statement.orelse
                or statement.type_comment is not None
            ):
                return None
            iterable = _compile_stake_expression(statement.iter, constants=constants)
            body = _compile_stake_statements(statement.body, constants=constants)
            if iterable is None or body is None:
                return None
            compiled.append(
                {
                    "op": "for",
                    "name": statement.target.id,
                    "iterable": iterable,
                    "body": body,
                }
            )
        elif isinstance(statement, ast.Return):
            value = _compile_stake_expression(statement.value, constants=constants)
            if value is None:
                return None
            compiled.append({"op": "return", "value": value})
        else:
            return None
    return compiled


def _compile_stake_expression(
    node: ast.AST | None,
    *,
    constants: dict[str, Any],
) -> dict[str, Any] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool | int | float | str):
        return {"op": "literal", "value": node.value}
    if isinstance(node, ast.Name):
        return {"op": "variable", "name": node.id}
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in constants
    ):
        value = constants[node.attr]
        if _stake_literal(value):
            return {"op": "literal", "value": value}
        return None
    if isinstance(node, ast.List | ast.Tuple):
        values = []
        for item in node.elts:
            compiled = _compile_stake_expression(item, constants=constants)
            if compiled is None or compiled.get("op") != "literal":
                return None
            values.append(compiled["value"])
        if not _stake_literal(values):
            return None
        return {"op": "literal", "value": values}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left = _compile_stake_expression(node.left, constants=constants)
        right = _compile_stake_expression(node.right, constants=constants)
        return (
            {"op": "multiply", "left": left, "right": right}
            if left is not None and right is not None
            else None
        )
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And | ast.Or):
        values = [_compile_stake_expression(value, constants=constants) for value in node.values]
        if any(value is None for value in values):
            return None
        return {
            "op": "and" if isinstance(node.op, ast.And) else "or",
            "values": values,
        }
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left = _compile_stake_expression(node.left, constants=constants)
        right = _compile_stake_expression(node.comparators[0], constants=constants)
        compare_op = (
            "equal"
            if isinstance(node.ops[0], ast.Eq)
            else "greater"
            if isinstance(node.ops[0], ast.Gt)
            else None
        )
        if compare_op is None or left is None or right is None:
            return None
        return {"op": compare_op, "left": left, "right": right}
    if isinstance(node, ast.IfExp):
        condition = _compile_stake_expression(node.test, constants=constants)
        body = _compile_stake_expression(node.body, constants=constants)
        otherwise = _compile_stake_expression(node.orelse, constants=constants)
        if condition is None or body is None or otherwise is None:
            return None
        return {
            "op": "choose",
            "condition": condition,
            "then": body,
            "otherwise": otherwise,
        }
    if isinstance(node, ast.Subscript):
        value = _compile_stake_expression(node.value, constants=constants)
        index = _compile_stake_expression(node.slice, constants=constants)
        if value is None or index is None:
            return None
        return {"op": "index", "value": value, "index": index}
    if isinstance(node, ast.Call):
        name = _qualified_name(node.func)
        if name == "entry_tag.split" and not node.args and not node.keywords:
            return {
                "op": "split_words",
                "value": {"op": "variable", "name": "entry_tag"},
            }
        if name == "scaled_stake" and len(node.args) == 1 and not node.keywords:
            multiplier = _compile_stake_expression(node.args[0], constants=constants)
            return (
                {"op": "stake_clamp_min", "multiplier": multiplier}
                if multiplier is not None
                else None
            )
        if name in {"all", "any"} and len(node.args) == 1 and not node.keywords:
            membership = _compile_membership_generator(
                node.args[0],
                constants=constants,
            )
            if membership is None:
                return None
            return {
                "op": "all_in" if name == "all" else "any_in",
                **membership,
            }
    return None


def _compile_membership_generator(
    node: ast.AST,
    *,
    constants: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        not isinstance(node, ast.GeneratorExp)
        or len(node.generators) != 1
        or node.generators[0].ifs
        or node.generators[0].is_async
        or not isinstance(node.generators[0].target, ast.Name)
        or not isinstance(node.elt, ast.Compare)
        or len(node.elt.ops) != 1
        or not isinstance(node.elt.ops[0], ast.In)
        or len(node.elt.comparators) != 1
        or not isinstance(node.elt.left, ast.Name)
        or node.elt.left.id != node.generators[0].target.id
    ):
        return None
    items = _compile_stake_expression(
        node.generators[0].iter,
        constants=constants,
    )
    container = _compile_stake_expression(
        node.elt.comparators[0],
        constants=constants,
    )
    if items is None or container is None:
        return None
    return {"items": items, "container": container}


def _stake_literal(value: Any) -> bool:
    if isinstance(value, bool | int | float | str):
        return True
    return isinstance(value, list) and all(
        isinstance(item, bool | int | float | str) for item in value
    )


def _lower_confirm_trade_entry(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    constants: dict[str, Any],
    method_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> dict[str, Any] | None:
    if isinstance(node, ast.AsyncFunctionDef):
        return None
    helper_names = {
        "_handle_grind_mode",
        "_handle_top_coins_mode",
        "_handle_scalp_mode",
    }
    functions: dict[str, Any] = {}
    for name in sorted(helper_names):
        helper = method_nodes.get(name)
        if helper is None or isinstance(helper, ast.AsyncFunctionDef):
            return None
        statements = _compile_confirm_statements(helper.body, constants=constants)
        if statements is None:
            return None
        functions[name] = {
            "parameters": [argument.arg for argument in helper.args.args[1:]],
            "statements": statements,
        }
    statements = _compile_confirm_statements(node.body, constants=constants)
    if statements is None:
        return None
    operation = {
        "opcode": "entry-confirm-program-v1",
        "statements": statements,
        "functions": functions,
    }
    return {
        "backend": "rust-entry-confirm-vm",
        "executable_in_rust": True,
        "operation": operation,
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": "bounded-entry-confirm-ast-v1",
            "helper_functions": sorted(functions),
            "program_sha256": hashlib.sha256(
                json.dumps(
                    operation,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
    }


def _compile_confirm_statements(
    statements: list[ast.stmt],
    *,
    constants: dict[str, Any],
) -> list[dict[str, Any]] | None:
    compiled: list[dict[str, Any]] = []
    for statement in statements:
        if isinstance(statement, ast.Assign):
            if (
                len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Tuple)
                and isinstance(statement.value, ast.Call)
                and _qualified_name(statement.value.func) == "self.dp.get_analyzed_dataframe"
            ):
                targets = statement.targets[0].elts
                if (
                    len(targets) != 2
                    or not isinstance(targets[0], ast.Name)
                    or not isinstance(targets[1], ast.Name)
                ):
                    return None
                compiled.append(
                    {
                        "op": "let",
                        "name": targets[0].id,
                        "value": {"op": "analyzed_frame"},
                    }
                )
                continue
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                return None
            value = _compile_confirm_expression(statement.value, constants=constants)
            if value is None:
                return None
            compiled.append(
                {
                    "op": "let",
                    "name": statement.targets[0].id,
                    "value": value,
                }
            )
        elif isinstance(statement, ast.If):
            condition = _compile_confirm_expression(statement.test, constants=constants)
            body = _compile_confirm_statements(statement.body, constants=constants)
            otherwise = _compile_confirm_statements(statement.orelse, constants=constants)
            if condition is None or body is None or otherwise is None:
                return None
            compiled.append(
                {
                    "op": "if",
                    "condition": condition,
                    "then": body,
                    "otherwise": otherwise,
                }
            )
        elif isinstance(statement, ast.Return):
            value = _compile_confirm_expression(statement.value, constants=constants)
            if value is None:
                return None
            compiled.append({"op": "return", "value": value})
        elif isinstance(statement, ast.Expr) and _is_log_call(statement.value):
            compiled.append({"op": "log_noop"})
        elif (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and _qualified_name(statement.value.func) == "self._remove_profit_target"
            and len(statement.value.args) == 1
            and not statement.value.keywords
        ):
            pair = _compile_confirm_expression(
                statement.value.args[0],
                constants=constants,
            )
            if pair is None:
                return None
            compiled.append({"op": "clear_profit_target", "pair": pair})
        else:
            return None
    return compiled


def _compile_confirm_expression(
    node: ast.AST | None,
    *,
    constants: dict[str, Any],
) -> dict[str, Any] | None:
    if isinstance(node, ast.Constant) and (
        node.value is None or isinstance(node.value, bool | int | float | str)
    ):
        return {"op": "literal", "value": node.value}
    if isinstance(node, ast.Name):
        return {"op": "variable", "name": node.id}
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in constants
    ):
        value = constants[node.attr]
        return {"op": "literal", "value": value} if _confirm_literal(value) else None
    if isinstance(node, ast.Attribute):
        value = _compile_confirm_expression(node.value, constants=constants)
        if value is None:
            return None
        if node.attr == "iloc":
            return value
        return {"op": "field", "value": value, "name": node.attr}
    if isinstance(node, ast.List | ast.Tuple):
        values = []
        for item in node.elts:
            compiled = _compile_confirm_expression(item, constants=constants)
            if compiled is None or compiled.get("op") != "literal":
                return None
            values.append(compiled["value"])
        return {"op": "literal", "value": values}
    if isinstance(node, ast.Subscript):
        if (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and node.value.attr == "config"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            return {"op": "config_value", "name": node.slice.value}
        value = _compile_confirm_expression(node.value, constants=constants)
        index = _compile_confirm_expression(node.slice, constants=constants)
        if value is None or index is None:
            return None
        return {"op": "index", "value": value, "index": index}
    if isinstance(node, ast.UnaryOp):
        value = _compile_confirm_expression(node.operand, constants=constants)
        if value is None:
            return None
        if isinstance(node.op, ast.USub):
            return {"op": "negative", "value": value}
        if isinstance(node.op, ast.Not):
            return {"op": "not", "value": value}
        return None
    if isinstance(node, ast.BinOp):
        left = _compile_confirm_expression(node.left, constants=constants)
        right = _compile_confirm_expression(node.right, constants=constants)
        binary = (
            "add"
            if isinstance(node.op, ast.Add)
            else "subtract"
            if isinstance(node.op, ast.Sub)
            else "multiply"
            if isinstance(node.op, ast.Mult)
            else "divide"
            if isinstance(node.op, ast.Div)
            else None
        )
        if binary is None or left is None or right is None:
            return None
        return {"op": binary, "left": left, "right": right}
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And | ast.Or):
        values = [_compile_confirm_expression(value, constants=constants) for value in node.values]
        if any(value is None for value in values):
            return None
        return {
            "op": "and" if isinstance(node.op, ast.And) else "or",
            "values": values,
        }
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left = _compile_confirm_expression(node.left, constants=constants)
        right = _compile_confirm_expression(node.comparators[0], constants=constants)
        compare = (
            "equal"
            if isinstance(node.ops[0], ast.Eq | ast.Is)
            else "not_equal"
            if isinstance(node.ops[0], ast.NotEq | ast.IsNot)
            else "greater"
            if isinstance(node.ops[0], ast.Gt)
            else "greater_equal"
            if isinstance(node.ops[0], ast.GtE)
            else "less"
            if isinstance(node.ops[0], ast.Lt)
            else "less_equal"
            if isinstance(node.ops[0], ast.LtE)
            else "contains"
            if isinstance(node.ops[0], ast.In)
            else None
        )
        if compare is None or left is None or right is None:
            return None
        if compare == "contains":
            return {"op": "contains", "container": right, "value": left}
        return {"op": compare, "left": left, "right": right}
    if isinstance(node, ast.Call):
        name = _qualified_name(node.func)
        if name in {"all", "any"} and len(node.args) == 1 and not node.keywords:
            membership = _compile_confirm_membership_generator(
                node.args[0],
                constants=constants,
            )
            if membership is None:
                return None
            return {
                "op": "all_in" if name == "all" else "any_in",
                **membership,
            }
        if name == "len" and len(node.args) == 1 and not node.keywords:
            value = _compile_confirm_expression(node.args[0], constants=constants)
            return {"op": "length", "value": value} if value is not None else None
        if name == "sum" and len(node.args) == 1 and not node.keywords:
            return _compile_confirm_count_generator(
                node.args[0],
                constants=constants,
            )
        if name == "Trade.get_trades_proxy":
            if len(node.keywords) != 1 or node.keywords[0].arg != "is_open":
                return None
            return {"op": "open_trades"}
        if name == "Trade.get_open_trade_count" and not node.args and not node.keywords:
            return {"op": "open_trade_count"}
        if name == "self.dp.get_analyzed_dataframe":
            return {"op": "analyzed_frame"}
        if name == "trade.calc_profit_ratio" and len(node.args) == 1 and not node.keywords:
            rate = _compile_confirm_expression(node.args[0], constants=constants)
            return {"op": "trade_profit_ratio", "rate": rate} if rate is not None else None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "split":
            if node.args or node.keywords:
                return None
            value = _compile_confirm_expression(node.func.value, constants=constants)
            return {"op": "split_words", "value": value} if value is not None else None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "partition":
            if (
                len(node.args) != 1
                or node.keywords
                or not isinstance(node.args[0], ast.Constant)
                or not isinstance(node.args[0].value, str)
            ):
                return None
            value = _compile_confirm_expression(node.func.value, constants=constants)
            return (
                {
                    "op": "partition",
                    "value": value,
                    "separator": node.args[0].value,
                }
                if value is not None
                else None
            )
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr
            in {"_handle_grind_mode", "_handle_top_coins_mode", "_handle_scalp_mode"}
        ):
            arguments = [
                _compile_confirm_expression(argument, constants=constants) for argument in node.args
            ]
            if node.keywords or any(argument is None for argument in arguments):
                return None
            return {
                "op": "call",
                "name": node.func.attr,
                "arguments": arguments,
            }
    return None


def _compile_confirm_membership_generator(
    node: ast.AST,
    *,
    constants: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        not isinstance(node, ast.GeneratorExp)
        or len(node.generators) != 1
        or node.generators[0].ifs
        or node.generators[0].is_async
        or not isinstance(node.generators[0].target, ast.Name)
        or not isinstance(node.elt, ast.Compare)
        or len(node.elt.ops) != 1
        or not isinstance(node.elt.ops[0], ast.In)
        or len(node.elt.comparators) != 1
        or not isinstance(node.elt.left, ast.Name)
        or node.elt.left.id != node.generators[0].target.id
    ):
        return None
    items = _compile_confirm_expression(
        node.generators[0].iter,
        constants=constants,
    )
    container = _compile_confirm_expression(
        node.elt.comparators[0],
        constants=constants,
    )
    if items is None or container is None:
        return None
    return {"items": items, "container": container}


def _compile_confirm_count_generator(
    node: ast.AST,
    *,
    constants: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        not isinstance(node, ast.GeneratorExp)
        or len(node.generators) != 1
        or node.generators[0].is_async
        or not isinstance(node.generators[0].target, ast.Name)
        or not isinstance(node.elt, ast.Constant)
        or node.elt.value != 1
    ):
        return None
    iterable = _compile_confirm_expression(
        node.generators[0].iter,
        constants=constants,
    )
    filters = [
        _compile_confirm_expression(item, constants=constants) for item in node.generators[0].ifs
    ]
    if iterable is None or any(item is None for item in filters):
        return None
    return {
        "op": "count",
        "name": node.generators[0].target.id,
        "iterable": iterable,
        "filters": filters,
    }


def _is_log_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _qualified_name(node.func) in {
        "log.info",
        "log.warning",
    }


def _confirm_literal(value: Any) -> bool:
    if value is None or isinstance(value, bool | int | float | str):
        return True
    return isinstance(value, list) and all(
        isinstance(item, bool | int | float | str) for item in value
    )


def _lower_confirm_trade_exit(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    constants: dict[str, Any],
    method_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    run_mode: str | None,
) -> dict[str, Any] | None:
    if isinstance(node, ast.AsyncFunctionDef) or run_mode not in {"backtest", "hyperopt"}:
        return None
    backtest_helper = method_nodes.get("is_backtest_mode")
    hold_helper = method_nodes.get("_should_hold_trade")
    remove_helper = method_nodes.get("_remove_profit_target")
    if (
        backtest_helper is None
        or hold_helper is None
        or remove_helper is None
        or not _proves_backtest_mode(backtest_helper)
        or not _proves_backtest_hold_disabled(hold_helper, run_mode=run_mode)
        or not _proves_profit_target_remove(remove_helper)
    ):
        return None
    transformed = copy.deepcopy(node)
    transformed = _BacktestExitTransformer().visit(transformed)
    ast.fix_missing_locations(transformed)
    effective_constants = dict(constants)
    if effective_constants.get("exit_profit_only") is False:
        effective_constants.setdefault("exit_profit_offset", 0.0)
    statements = _compile_confirm_statements(
        _through_first_unconditional_return(transformed.body),
        constants=effective_constants,
    )
    if statements is None:
        return None
    operation = {
        "opcode": "exit-confirm-program-v1",
        "statements": statements,
        "functions": {},
    }
    return {
        "backend": "rust-exit-confirm-vm",
        "executable_in_rust": True,
        "operation": operation,
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": "bounded-backtest-exit-confirm-ast-v1",
            "backtest_hold_behavior": "disabled",
            "profit_target_effect": "clear-pair",
            "program_sha256": hashlib.sha256(
                json.dumps(
                    operation,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
    }


def _through_first_unconditional_return(statements: list[ast.stmt]) -> list[ast.stmt]:
    for index, statement in enumerate(statements):
        if isinstance(statement, ast.Return):
            return statements[: index + 1]
    return statements


class _BacktestExitTransformer(ast.NodeTransformer):
    def visit_Call(self, node: ast.Call) -> ast.AST:
        transformed = self.generic_visit(node)
        if not isinstance(transformed, ast.Call):
            return transformed
        name = _qualified_name(transformed.func)
        if name == "self.is_backtest_mode":
            return ast.copy_location(ast.Constant(value=True), transformed)
        if name == "self._should_hold_trade":
            return ast.copy_location(ast.Constant(value=False), transformed)
        return transformed


def _proves_backtest_mode(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    statements = [
        statement
        for statement in node.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    if len(statements) != 1 or not isinstance(statements[0], ast.Return):
        return False
    value = statements[0].value
    return (
        isinstance(value, ast.Compare)
        and len(value.ops) == 1
        and isinstance(value.ops[0], ast.In)
        and len(value.comparators) == 1
        and isinstance(value.left, ast.Attribute)
        and value.left.attr == "value"
        and _qualified_name(value.left.value) == "self.dp.runmode"
        and isinstance(value.comparators[0], ast.List | ast.Tuple)
        and {
            item.value
            for item in value.comparators[0].elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        == {"backtest", "hyperopt"}
    )


def _proves_backtest_hold_disabled(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    run_mode: str,
) -> bool:
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            if any(isinstance(item, ast.Call) for item in ast.walk(statement)):
                return False
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if not isinstance(statement, ast.If):
            return False
        excluded = _runmode_not_in_values(statement.test)
        return (
            excluded is not None
            and run_mode not in excluded
            and len(statement.body) == 1
            and isinstance(statement.body[0], ast.Return)
            and isinstance(statement.body[0].value, ast.Constant)
            and statement.body[0].value.value is False
        )
    return False


def _proves_profit_target_remove(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    calls = {_qualified_name(item.func) for item in ast.walk(node) if isinstance(item, ast.Call)}
    return calls == {"target_profit_cache.data.pop", "target_profit_cache.save"}
