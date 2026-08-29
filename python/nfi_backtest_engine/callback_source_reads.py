"""Callback candle, wallet, trade, and order read extraction."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence

from .callback_source_identity import _append_unique
from .callback_source_routes import _ordered_nodes

_CANDLE_INPUTS = {
    "current_time",
    "current_rate",
    "current_profit",
    "current_entry_rate",
    "current_exit_rate",
    "current_entry_profit",
    "current_exit_profit",
}
_WALLET_INPUTS = {"min_stake", "max_stake", "proposed_stake"}


def _required_data(
    closure: Sequence[str],
    methods: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> tuple[list[tuple[str, str]], list[str]]:
    reads: list[tuple[str, str]] = []
    columns: list[str] = []
    for method_name in closure:
        method = methods[method_name]
        candle_aliases = _candle_aliases(method)
        called_attributes = {
            id(call.func)
            for call in _ordered_nodes(method, ast.Call)
            if isinstance(call.func, ast.Attribute)
        }
        for node in _ordered_nodes(method, ast.Attribute):
            if (
                id(node) not in called_attributes
                and isinstance(node.value, ast.Name)
                and node.value.id in {"trade", "order"}
            ):
                _append_unique(reads, (node.value.id, node.attr))
        for node in _ordered_nodes(method, ast.Name):
            if node.id in _CANDLE_INPUTS:
                _append_unique(reads, ("candle", node.id))
            elif node.id in _WALLET_INPUTS:
                _append_unique(reads, ("wallet", node.id))
        for call in _ordered_nodes(method, ast.Call):
            leaf = _call_leaf(call)
            if leaf == "get_custom_data" and call.args and _literal_string(call.args[0]):
                _append_unique(reads, ("custom_state", str(ast.literal_eval(call.args[0]))))
            elif leaf == "select_filled_orders":
                _append_unique(reads, ("orders", "filled"))
        for subscript in _ordered_nodes(method, ast.Subscript):
            if not _literal_string(subscript.slice):
                continue
            root = _root_name(subscript.value)
            if root in candle_aliases or (root is not None and "candle" in root.lower()):
                _append_unique(columns, str(ast.literal_eval(subscript.slice)))
    return reads, columns


def _candle_aliases(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    aliases = {
        argument.arg
        for argument in (*node.args.args, *node.args.kwonlyargs)
        if "candle" in argument.arg.lower()
    }
    for assignment in _ordered_nodes(node, (ast.Assign, ast.AnnAssign)):
        target: ast.AST | None
        value: ast.AST | None
        if isinstance(assignment, ast.Assign):
            target = assignment.targets[0] if len(assignment.targets) == 1 else None
            value = assignment.value
        else:
            target = assignment.target
            value = assignment.value
        if isinstance(target, ast.Name) and value is not None and ".iloc[" in ast.unparse(value):
            aliases.add(target.id)
    return aliases


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [
        argument.arg
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if argument.arg != "self"
    ]


def _call_leaf(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _literal_string(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)
