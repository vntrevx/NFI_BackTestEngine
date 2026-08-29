"""Order-filled custom-data state-machine lowering."""

from __future__ import annotations

import ast
import hashlib
import json

from .callback_ast import _is_none_expression, _qualified_name
from .callback_contract import CALLBACK_LOWERING_VERSION, JsonObject
from .callback_order_state_values import (
    _extract_order_tag_actions,
    _is_first_successful_entry_test,
    _is_set_custom_data_call,
    _literal_write_block,
    _record_static_alias,
    _select_static_if,
    _static_bool,
)


def _lower_x7_order_filled(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    constants: JsonObject,
) -> JsonObject | None:
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
    environment: JsonObject = {}
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
    tag_actions: dict[str, list[JsonObject]] = {}
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
