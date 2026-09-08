"""Prove fresh-instance overrides of initialization-managed source attributes."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any

from .errors import StrategyAnalysisError

INITIALIZATION_PARAMETERS = frozenset(
    {"is_futures_mode", "can_short", "target_profit_cache", "hold_trades_cache"}
)
_MODE = """
if ('trading_mode' in strategy_config) and (
    strategy_config['trading_mode'] in ['futures', 'margin']
):
    self.is_futures_mode = True
    self.can_short = True
"""


def _same(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def _false_source_default(cls: ast.ClassDef, name: str) -> bool:
    declarations = [
        node
        for node in cls.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
    ]
    if not declarations:
        return name == "can_short"  # Inherited from the proved, pinned IStrategy base.
    return (
        len(declarations) == 1
        and isinstance(declarations[0].value, ast.Constant)
        and declarations[0].value.value is False
    )


def initialization_parameters(
    cls: ast.ClassDef,
    initializer: ast.FunctionDef,
    defaults: Mapping[str, Any],
    config: Mapping[str, Any],
    nested: Mapping[str, Any],
) -> dict[str, Any]:
    requested = INITIALIZATION_PARAMETERS & nested.keys()
    if not requested:
        return {}
    if (
        len(cls.bases) != 1
        or not isinstance(cls.bases[0], ast.Name)
        or cls.bases[0].id != "IStrategy"
    ):
        raise StrategyAnalysisError("initialization overrides require the pinned strategy base")
    parameter_assignments = [
        node
        for node in initializer.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "nfi_parameters"
    ]
    if len(parameter_assignments) != 1:
        raise StrategyAnalysisError("initialization parameter stage changed")
    boundary = initializer.body.index(parameter_assignments[0])
    prefix = ast.Module(body=initializer.body[:boundary], type_ignores=[])
    expected_super = ast.parse("super().__init__(config)").body[0]
    if sum(_same(node, expected_super) for node in initializer.body[:boundary]) != 1:
        raise StrategyAnalysisError("initialization base constructor changed")
    super_call = ast.parse("super().__init__(config)", mode="eval").body
    super_name = ast.parse("super()", mode="eval").body
    if sum(_same(node, super_call) for node in ast.walk(prefix)) != 1:
        raise StrategyAnalysisError("initialization repeats its base constructor")
    for node in ast.walk(prefix):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in requested
        ):
            raise StrategyAnalysisError(
                "initialization override observes pre-existing instance state"
            )
        if isinstance(node, ast.Call):
            logging_call = (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "log"
                and node.func.attr in {"info", "warning", "debug"}
            )
            if not (logging_call or _same(node, super_call) or _same(node, super_name)):
                raise StrategyAnalysisError("initialization prefix calls an unproven function")
    mode = config.get("trading_mode", "spot")
    if mode not in {"spot", "futures"}:
        raise StrategyAnalysisError("initialization trading mode is unsupported")
    result = {}
    bool_names = requested & {"is_futures_mode", "can_short"}
    if bool_names:
        expected_mode = ast.parse(_MODE).body[0]
        matching = [node for node in initializer.body if _same(node, expected_mode)]
        if len(matching) != 1 or initializer.body.index(matching[0]) <= boundary + 1:
            raise StrategyAnalysisError("initialization mode stage changed")
        covered = {id(node) for node in ast.walk(matching[0])}
        initialization_nodes = {id(node) for node in ast.walk(initializer)}
        for node in ast.walk(cls):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in bool_names
                and id(node) not in covered
                and (id(node) in initialization_nodes or not isinstance(node.ctx, ast.Load))
            ):
                raise StrategyAnalysisError("initialization mode has an additional dependency")
    for name in requested:
        value = nested[name]
        if name in bool_names:
            if type(value) is not bool or not _false_source_default(cls, name):
                raise StrategyAnalysisError("initialization mode default or override is invalid")
            effective = True if mode == "futures" else value
            if effective != (mode == "futures"):
                raise StrategyAnalysisError(
                    "initialization source mode differs from the exchange mode"
                )
            result[name] = effective
        else:
            declarations = [
                node
                for node in cls.body
                if isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name
            ]
            if (
                value is not None
                or defaults.get(name, False) is not None
                or len(declarations) != 1
                or not isinstance(declarations[0].value, ast.Constant)
                or declarations[0].value.value is not None
            ):
                raise StrategyAnalysisError(
                    "initialization cache override must preserve the fresh None default"
                )
            result[name] = None
    return result
