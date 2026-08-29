"""Bounded tag-to-leverage callback lowering."""

from __future__ import annotations

import ast
import hashlib
import math

from .callback_contract import CALLBACK_LOWERING_VERSION, JsonObject


def _lower_x7_leverage(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    constants: JsonObject,
) -> JsonObject | None:
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
        tag_lists[name] = list(dict.fromkeys(tag for tag in raw if isinstance(tag, str)))

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
