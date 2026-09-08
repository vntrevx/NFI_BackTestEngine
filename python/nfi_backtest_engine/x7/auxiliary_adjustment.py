"""Source proofs for counted entry groups outside the managed grind ladder."""

from __future__ import annotations

import ast
from typing import Any

from ..errors import StrategyAnalysisError


def same(left: ast.AST, expression: str) -> bool:
    right = ast.parse(expression, mode="eval").body
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def if_test(node: ast.AST, expression: str) -> bool:
    return isinstance(node, ast.If) and same(node.test, expression)


def stores(method: ast.FunctionDef, name: str) -> list[ast.Name]:
    return [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Name) and node.id == name and not isinstance(node.ctx, ast.Load)
    ]


def _tag_chain(
    branch: ast.If, prefixes: list[str], *, exiting: bool, derisk_levels: int
) -> list[ast.If]:
    tag_value = "order.ft_order_tag.partition(' ')[0]" if exiting else "order.ft_order_tag"
    tag_source = ast.parse(
        "order_tag = ''\n"
        "if has_order_tags:\n"
        "    if order.ft_order_tag is not None:\n"
        f"        order_tag = {tag_value}\n"
    ).body
    offset = 1 if exiting else 0
    if len(branch.body) != offset + 3 or any(
        ast.dump(actual, include_attributes=False) != ast.dump(expected, include_attributes=False)
        for actual, expected in zip(branch.body[offset : offset + 2], tag_source, strict=True)
    ):
        raise StrategyAnalysisError("source counted-group tag extraction changed")
    expected_tests = (
        [f"order_tag == 'derisk_level_{level}'" for level in range(1, derisk_levels + 1)]
        if exiting
        else []
    )
    expected_tests += [
        f"not {prefix}_is_exit_found and order_tag in {[prefix + '_exit', prefix + '_derisk']!r}"
        if exiting
        else f"not {prefix}_is_exit_found and order_tag == {prefix + '_entry'!r}"
        for prefix in prefixes
    ]
    if exiting:
        expected_tests.append("order_tag == 'derisk_global'")
    chain: list[ast.If] = []
    remaining = branch.body[-1:]
    for expected in expected_tests:
        if len(remaining) != 1 or not isinstance(remaining[0], ast.If):
            raise StrategyAnalysisError("source counted-group tag chain changed")
        current = remaining[0]
        if not same(current.test, expected):
            raise StrategyAnalysisError("source counted-group tag guard changed")
        chain.append(current)
        remaining = current.orelse
    if remaining:
        raise StrategyAnalysisError("source counted-group tag fallback changed")
    return chain


def prove_counted_entry_groups(
    method: ast.FunctionDef, levels: list[int], side: str
) -> list[dict[str, Any]]:
    if side not in {"long", "short"}:
        raise StrategyAnalysisError("counted entry group direction is unsupported")
    sums = [
        node
        for node in method.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "num_open_grinds_and_buybacks"
    ]
    if len(sums) != 1 or len(stores(method, "num_open_grinds_and_buybacks")) != 1:
        raise StrategyAnalysisError("source open-cluster count assignment changed")

    def terms(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return terms(node.left) + terms(node.right)
        raise StrategyAnalysisError("source open-cluster sum changed")

    names = terms(sums[0].value)
    grind_names = [f"grind_{level}_sub_grind_count" for level in levels]
    if names[: len(grind_names)] != grind_names or len(set(names)) != len(names):
        raise StrategyAnalysisError("source open-cluster count order changed")
    extra = names[len(grind_names) :]
    if any(not name.endswith("_sub_grind_count") for name in extra):
        raise StrategyAnalysisError("source counted entry group is unsupported")
    if not extra:
        return []
    loops = [
        node
        for node in method.body
        if isinstance(node, ast.For)
        and same(node.iter, "reversed(filled_orders)")
        and isinstance(node.target, ast.Name)
        and node.target.id == "order"
    ]
    if len(loops) != 1 or len(loops[0].body) != 1 or not isinstance(loops[0].body[0], ast.If):
        raise StrategyAnalysisError("source counted-group traversal changed")
    entry = loops[0].body[0]
    entry_side, exit_side = ("buy", "sell") if side == "long" else ("sell", "buy")
    if not same(
        entry.test, f"order.ft_order_side == {entry_side!r} and order is not filled_orders[0]"
    ):
        raise StrategyAnalysisError("source counted-group first-entry exclusion changed")
    if len(entry.orelse) != 1 or not isinstance(entry.orelse[0], ast.If):
        raise StrategyAnalysisError("source counted-group exit traversal changed")
    exit_branch = entry.orelse[0]
    if not same(exit_branch.test, f"order.ft_order_side == {exit_side!r}"):
        raise StrategyAnalysisError("source counted-group exit direction changed")
    if (
        loops[0].orelse
        or exit_branch.orelse
        or any(
            isinstance(node, (ast.Break, ast.Continue, ast.Return, ast.Raise))
            for node in ast.walk(loops[0])
        )
    ):
        raise StrategyAnalysisError("source counted-group traversal control changed")
    prefixes = [name.removesuffix("_sub_grind_count") for name in names]
    derisk_levels = 4 if side == "long" else 3
    entry_chain = _tag_chain(entry, prefixes, exiting=False, derisk_levels=derisk_levels)
    exit_chain = _tag_chain(exit_branch, prefixes, exiting=True, derisk_levels=derisk_levels)
    parents = {
        child: parent for parent in ast.walk(method) for child in ast.iter_child_nodes(parent)
    }
    groups = []
    for count in extra:
        prefix = count.removesuffix("_sub_grind_count")
        found = prefix + "_is_exit_found"
        # Compare assignment targets by identifier; their AST contexts are Store.
        initial = [
            node
            for node in method.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == count
        ]
        initial_found = [
            node
            for node in method.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == found
        ]
        if len(initial) != 1 or not same(initial[0].value, "0") or len(stores(method, count)) != 2:
            raise StrategyAnalysisError("source counted-group initialization changed")
        if (
            len(initial_found) != 1
            or not same(initial_found[0].value, "False")
            or len(stores(method, found)) != 3
        ):
            raise StrategyAnalysisError("source counted-group exit marker changed")
        if not (
            method.body.index(initial[0]) < method.body.index(loops[0])
            and method.body.index(initial_found[0])
            < method.body.index(loops[0])
            < method.body.index(sums[0])
        ):
            raise StrategyAnalysisError("source counted-group initialization order changed")
        additions = [
            node
            for node in ast.walk(entry)
            if isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == count
        ]
        if (
            len(additions) != 1
            or not isinstance(additions[0].op, ast.Add)
            or not same(additions[0].value, "1")
        ):
            raise StrategyAnalysisError("source counted-group increment changed")
        guard = parents[additions[0]]
        tag = prefix + "_entry"
        if (
            guard not in entry_chain
            or not isinstance(guard, ast.If)
            or not same(guard.test, f"not {found} and order_tag == {tag!r}")
        ):
            raise StrategyAnalysisError("source counted-group entry guard changed")
        resets = [
            node
            for node in ast.walk(exit_branch)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == found
        ]
        if len(resets) != 2 or any(not same(node.value, "True") for node in resets):
            raise StrategyAnalysisError("source counted-group reset changed")
        closing = [prefix + "_exit", prefix + "_derisk"]
        normal = [
            node
            for node in resets
            if parents[node] in exit_chain
            and if_test(parents[node], f"not {found} and order_tag in {closing!r}")
        ]
        global_reset = [
            node
            for node in resets
            if if_test(parents[node], f"not {found}")
            and parents[parents[node]] in exit_chain
            and if_test(parents[parents[node]], "order_tag == 'derisk_global'")
        ]
        if len(normal) != 1 or len(global_reset) != 1:
            raise StrategyAnalysisError("source counted-group exit tags changed")
        groups.append(
            {"count_variable": count, "entry_tag": tag, "exit_tags": closing + ["derisk_global"]}
        )
    return groups
