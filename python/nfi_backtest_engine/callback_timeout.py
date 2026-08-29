"""Immediate-fill timeout callback lowering."""

from __future__ import annotations

import ast
import hashlib

from .callback_contract import CALLBACK_LOWERING_VERSION, JsonObject
from .callback_timeout_tags import _is_boolean_return, _timeout_skip_order_tag


def _lower_immediate_fill_timeout(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    run_mode: str | None,
    method_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> JsonObject | None:
    """Bind X7's orderbook timeout policy to the immediate-fill backtest model.

    The native engine does not retain open limit orders: ordinary entries,
    exits, and position adjustments fill at their already validated candle
    price. Freqtrade can therefore call these callbacks only when another
    strategy callback first creates or reprices an unfilled order. Keep the
    proof narrow by rejecting that wider callback surface and by matching the
    complete X7 timeout body, including side, orderbook level, and thresholds.
    """
    if run_mode != "backtest" or isinstance(node, ast.AsyncFunctionDef):
        return None
    price_callbacks = {
        "adjust_entry_price",
        "adjust_exit_price",
        "adjust_order_price",
        "custom_entry_price",
        "custom_exit_price",
    }
    if price_callbacks & method_nodes.keys():
        return None
    body = node.body
    skip_order_tags: list[str] = []
    if len(body) == 6:
        skip_order_tag = _timeout_skip_order_tag(body[0])
        if node.name != "check_exit_timeout" or skip_order_tag is None:
            return None
        skip_order_tags.append(skip_order_tag)
        body = body[1:]
    if len(body) != 5:
        return None
    if not _is_orderbook_assignment(body[0]):
        return None
    if not _is_orderbook_price_assignment(body[1], target="bids", side="bids"):
        return None
    if not _is_orderbook_price_assignment(body[2], target="asks", side="asks"):
        return None
    branch = body[3]
    if (
        not isinstance(branch, ast.If)
        or not _is_trade_short_attribute(branch.test)
        or len(branch.body) != 1
        or len(branch.orelse) != 1
        or not isinstance(branch.body[0], ast.If)
        or not isinstance(branch.orelse[0], ast.If)
        or not _is_boolean_return(body[4], value=False)
    ):
        return None
    expected = {
        "check_entry_timeout": (
            ("asks", ast.Lt, 0.97),
            ("bids", ast.Gt, 1.03),
        ),
        "check_exit_timeout": (
            ("bids", ast.Gt, 1.03),
            ("asks", ast.Lt, 0.97),
        ),
    }.get(node.name)
    if expected is None:
        return None
    if not _is_timeout_branch(branch.body[0], *expected[0]):
        return None
    if not _is_timeout_branch(branch.orelse[0], *expected[1]):
        return None
    operation = {
        "opcode": "open-order-timeout-policy-v1",
        "execution_scope": "unreachable-immediate-fill-backtest-v1",
        "orderbook_depth": 1,
        "skip_order_tags": skip_order_tags,
        "short": {
            "price": expected[0][0],
            "comparison": "less-than" if expected[0][1] is ast.Lt else "greater-than",
            "order_price_multiplier": expected[0][2],
        },
        "long": {
            "price": expected[1][0],
            "comparison": "less-than" if expected[1][1] is ast.Lt else "greater-than",
            "order_price_multiplier": expected[1][2],
        },
    }
    return {
        "backend": "rust-immediate-fill-open-order-proof",
        "executable_in_rust": True,
        "operation": operation,
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": f"nfi-x7-{node.name.replace('_', '-')}-v1",
            "ast_sha256": hashlib.sha256(
                ast.dump(node, annotate_fields=True, include_attributes=False).encode()
            ).hexdigest(),
            "excluded_price_callbacks": sorted(price_callbacks),
        },
    }


def _is_orderbook_assignment(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "ob"
        and isinstance(statement.value, ast.Call)
        and not statement.value.keywords
        and len(statement.value.args) == 2
        and isinstance(statement.value.func, ast.Attribute)
        and statement.value.func.attr == "orderbook"
        and isinstance(statement.value.func.value, ast.Attribute)
        and statement.value.func.value.attr == "dp"
        and isinstance(statement.value.func.value.value, ast.Name)
        and statement.value.func.value.value.id == "self"
        and isinstance(statement.value.args[0], ast.Name)
        and statement.value.args[0].id == "pair"
        and isinstance(statement.value.args[1], ast.Constant)
        and statement.value.args[1].value == 1
    )


def _is_orderbook_price_assignment(
    statement: ast.stmt,
    *,
    target: str,
    side: str,
) -> bool:
    if (
        not isinstance(statement, ast.Assign)
        or len(statement.targets) != 1
        or not isinstance(statement.targets[0], ast.Name)
        or statement.targets[0].id != target
    ):
        return False
    value: ast.expr = statement.value
    for expected_index in (0, 0, side):
        if not isinstance(value, ast.Subscript):
            return False
        slice_value = value.slice
        if not isinstance(slice_value, ast.Constant) or slice_value.value != expected_index:
            return False
        value = value.value
    return isinstance(value, ast.Name) and value.id == "ob"


def _is_trade_short_attribute(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Attribute)
        and expression.attr == "is_short"
        and isinstance(expression.value, ast.Name)
        and expression.value.id == "trade"
    )


def _is_timeout_branch(
    branch: ast.If,
    price_name: str,
    comparison_type: type[ast.cmpop],
    multiplier: float,
) -> bool:
    test = branch.test
    if (
        len(branch.body) != 1
        or branch.orelse
        or not _is_boolean_return(branch.body[0], value=True)
        or not isinstance(test, ast.Compare)
        or len(test.ops) != 1
        or not isinstance(test.ops[0], comparison_type)
        or len(test.comparators) != 1
        or not isinstance(test.left, ast.Name)
        or test.left.id != price_name
    ):
        return False
    right = test.comparators[0]
    return (
        isinstance(right, ast.BinOp)
        and isinstance(right.op, ast.Mult)
        and isinstance(right.left, ast.Attribute)
        and right.left.attr == "price"
        and isinstance(right.left.value, ast.Name)
        and right.left.value.id == "order"
        and isinstance(right.right, ast.Constant)
        and isinstance(right.right.value, int | float)
        and not isinstance(right.right.value, bool)
        and float(right.right.value) == multiplier
    )
