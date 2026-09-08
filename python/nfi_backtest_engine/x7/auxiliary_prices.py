"""Prove weighted entry prices and source exit anchors for auxiliary groups."""

from __future__ import annotations

import ast
import re

from ..errors import StrategyAnalysisError
from .auxiliary_adjustment import if_test, same, stores
from .auxiliary_entry import _assignment


def prove_group_prices(method: ast.FunctionDef, prefix: str) -> str:
    parents = {child: node for node in ast.walk(method) for child in ast.iter_child_nodes(node)}

    loops = [
        node
        for node in method.body
        if isinstance(node, ast.For) and same(node.iter, "reversed(filled_orders)")
    ]
    actions = [
        node
        for node in method.body
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Assign)
            and len(child.targets) == 1
            and isinstance(child.targets[0], ast.Name)
            and child.targets[0].id == "order_tag"
            and same(child.value, repr(prefix + "_entry"))
            for child in ast.walk(node)
        )
    ]
    if len(loops) != 1 or len(actions) != 1:
        raise StrategyAnalysisError("auxiliary group scan/action identity changed")
    scan_index = method.body.index(loops[0])
    action_index = method.body.index(actions[0])

    def assignments(name: str) -> list[ast.Assign | ast.AugAssign]:
        found = [
            node
            for node in ast.walk(method)
            if isinstance(node, (ast.Assign, ast.AugAssign))
            and (
                (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == name
                )
                or (
                    isinstance(node, ast.AugAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == name
                )
            )
        ]
        if len(found) != len(stores(method, name)):
            raise StrategyAnalysisError("auxiliary group write form changed")
        return found

    def initialized(name: str, value: str, count: int):
        writes = assignments(name)
        initial = [
            node
            for node in writes
            if node in method.body and isinstance(node, ast.Assign) and same(node.value, value)
        ]
        if len(writes) != count or len(initial) != 1 or method.body.index(initial[0]) >= scan_index:
            raise StrategyAnalysisError("auxiliary group initialization changed")
        return initial[0], [node for node in writes if node is not initial[0]]

    count = prefix + "_sub_grind_count"
    found = prefix + "_is_exit_found"
    order = prefix + "_exit_order"
    for suffix, value in [
        ("total_amount", "order.safe_filled"),
        ("total_cost", "order.safe_filled * order.safe_price"),
    ]:
        initial, writes = initialized(prefix + "_" + suffix, "0.0", 2)
        addition = writes[0]
        if not (
            isinstance(addition, ast.AugAssign)
            and isinstance(addition.op, ast.Add)
            and same(addition.value, value)
            and if_test(parents[addition], f"not {found} and order_tag == {prefix + '_entry'!r}")
        ):
            raise StrategyAnalysisError("auxiliary group accumulation changed")
        loop = parents[parents[addition]]
        while not isinstance(loop, ast.For):
            loop = parents[loop]
        if loop not in method.body or method.body.index(initial) >= method.body.index(loop):
            raise StrategyAnalysisError("auxiliary group initialization order changed")
    initial, writes = initialized(prefix + "_current_open_rate", "0.0", 2)
    calculation = writes[0]
    guard = parents[calculation]
    if not (
        isinstance(calculation, ast.Assign)
        and same(calculation.value, f"{prefix}_total_cost / {prefix}_total_amount")
        and isinstance(guard, ast.If)
        and if_test(guard, f"{count} > 0")
        and guard in method.body
        and not guard.orelse
        and scan_index < method.body.index(guard) < action_index
    ):
        raise StrategyAnalysisError("auxiliary group weighted rate changed")
    _assignment(method, "trade_fee_close", "trade.fee_close")
    fee_assignment = next(
        node
        for node in method.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "trade_fee_close"
    )
    if method.body.index(fee_assignment) >= method.body.index(guard):
        raise StrategyAnalysisError("auxiliary group fee initialization order changed")
    last_calculation = calculation
    for suffix, expression in [
        ("current_grind_stake", f"{prefix}_total_amount * exit_rate * (1 - trade_fee_close)"),
        ("current_grind_stake_profit", f"{prefix}_current_grind_stake - {prefix}_total_cost"),
    ]:
        initial, writes = initialized(prefix + "_" + suffix, "0.0", 2)
        write = writes[0]
        if not (
            isinstance(write, ast.Assign)
            and same(write.value, expression)
            and parents[write] is guard
            and guard.body.index(write) > guard.body.index(last_calculation)
        ):
            raise StrategyAnalysisError("auxiliary group raw-fee profit calculation changed")
        last_calculation = write
    initial, writes = initialized(order, "None", 3)
    for write in writes:
        parent = parents[write]
        normal = if_test(
            parent, f"not {found} and order_tag in {[prefix + '_exit', prefix + '_derisk']!r}"
        )
        global_reset = if_test(parent, f"not {found}") and if_test(
            parents[parent], "order_tag == 'derisk_global'"
        )
        if not (
            isinstance(write, ast.Assign)
            and same(write.value, "order")
            and isinstance(parent, ast.If)
            and (normal or global_reset)
            and len(parent.body) == 2
            and parent.body[1] is write
        ):
            raise StrategyAnalysisError("auxiliary group latest exit anchor changed")
    initial, writes = initialized(prefix + "_exit_distance_ratio", "0.0", 3)
    if not all(isinstance(write, ast.Assign) for write in writes):
        raise StrategyAnalysisError("auxiliary group distance write changed")
    normal, fallback = writes
    normal_guard = parents[normal]
    fallback_guard = parents[fallback]
    if not (
        isinstance(normal_guard, ast.If)
        and isinstance(fallback_guard, ast.If)
        and if_test(normal_guard, found)
        and normal_guard in method.body
        and scan_index < method.body.index(normal_guard) < action_index
        and normal_guard.body == [normal]
        and normal_guard.orelse == [fallback_guard]
        and fallback_guard.body == [fallback]
        and not fallback_guard.orelse
        and same(normal.value, f"(exit_rate - {order}.safe_price) / {order}.safe_price")
        and isinstance(fallback_guard.test, ast.Name)
    ):
        raise StrategyAnalysisError("auxiliary group exit distance precedence changed")
    match = re.fullmatch(r"is_derisk_(\d+)_found", fallback_guard.test.id)
    if not match:
        raise StrategyAnalysisError("auxiliary group fallback marker changed")
    level = match[1]
    anchor = f"derisk_{level}_order"
    marker = f"is_derisk_{level}_found"
    if not same(fallback.value, f"(exit_rate - {anchor}.safe_price) / {anchor}.safe_price"):
        raise StrategyAnalysisError("auxiliary group fallback price changed")
    initial, writes = initialized(anchor, "None", 2)
    write = writes[0]
    guard = parents[write]
    if not (
        isinstance(write, ast.Assign)
        and same(write.value, "order")
        and if_test(guard, f"not {marker}")
        and if_test(parents[guard], f"order_tag == {'derisk_level_' + level!r}")
    ):
        raise StrategyAnalysisError("auxiliary group latest fallback order changed")
    return "derisk_level_" + level


def prove_group_exit_ids(method: ast.FunctionDef, prefix: str, action: ast.If) -> None:
    """Retain the source's reverse-order entry IDs on partial-exit tags."""
    name = prefix + "_buy_orders"
    parents = {child: node for node in ast.walk(method) for child in ast.iter_child_nodes(node)}
    initial = [
        node
        for node in method.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
        and same(node.value, "[]")
    ]
    loops = [node for node in ast.walk(action) if isinstance(node, ast.For)]
    expected = ast.parse(
        f"for grind_entry_id in {name}:\n    order_tag += ' ' + str(grind_entry_id)"
    ).body[0]
    if (
        len(initial) != 1
        or len(stores(method, name)) != 1
        or len(loops) != 1
        or ast.dump(loops[0], include_attributes=False)
        != ast.dump(expected, include_attributes=False)
    ):
        raise StrategyAnalysisError("auxiliary group exit ID rendering changed")
    loads = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
    ]
    if len(loads) != 2:
        raise StrategyAnalysisError("auxiliary group entry ID access changed")
    additions = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Expr) and same(node.value, f"{name}.append(order.id)")
    ]
    if len(additions) != 1 or not if_test(
        parents[additions[0]], f"not {prefix}_is_exit_found and order_tag == {prefix + '_entry'!r}"
    ):
        raise StrategyAnalysisError("auxiliary group entry ID collection changed")
    scan: ast.AST = additions[0]
    while scan in parents and not isinstance(scan, ast.For):
        scan = parents[scan]
    if (
        not isinstance(scan, ast.For)
        or scan not in method.body
        or method.body.index(initial[0]) >= method.body.index(scan)
    ):
        raise StrategyAnalysisError("auxiliary group entry ID initialization order changed")
