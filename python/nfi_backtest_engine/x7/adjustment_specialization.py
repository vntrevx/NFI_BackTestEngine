"""Prove local adjustment switches before replacing their runtime bindings."""

from __future__ import annotations

import ast
import copy
import re
from collections.abc import Mapping
from typing import Any

from ..errors import StrategyAnalysisError
from .system_exit_ir import ActiveSystemLowerer


def source_derisk_switches(
    method: ast.FunctionDef,
    constants: Mapping[str, Any],
) -> dict[str, bool]:
    specialized = copy.deepcopy(method)
    ActiveSystemLowerer(constants).visit(specialized)
    result = {}
    for statement in specialized.body:
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            continue
        name = statement.targets[0].id
        if re.fullmatch(r"derisk_\d+_enable", name) is None:
            continue
        writes = [
            node
            for node in ast.walk(specialized)
            if isinstance(node, ast.Name) and node.id == name and not isinstance(node.ctx, ast.Load)
        ]
        if len(writes) != 1:
            raise StrategyAnalysisError(f"adjustment switch {name!r} is reassigned")
        expression = statement.value
        if isinstance(expression, ast.Constant):
            value = expression.value
        elif (
            isinstance(expression, ast.Attribute)
            and isinstance(expression.value, ast.Name)
            and expression.value.id == "self"
        ):
            value = constants.get(expression.attr)
        else:
            raise StrategyAnalysisError(f"adjustment switch {name!r} is not static")
        if type(value) is not bool:
            raise StrategyAnalysisError(f"adjustment switch {name!r} is not boolean")
        result[name] = value
    return result
