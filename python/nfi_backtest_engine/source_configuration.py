"""Freeze the source's bounded NFI constructor parameter updates before lowering."""

from __future__ import annotations

import ast
import copy
import hashlib
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .constructor_scalars import MANAGED_SCALARS, constructor_scalar_stages
from .errors import StrategyAnalysisError
from .initialization_parameters import initialization_parameters
from .strategy_resolver_settings import (
    INHERITED_SETTINGS,
    RESOLVER_SETTINGS,
    resolver_overrides,
    valid_resolver_value,
)

_SIGNALS = ("long_entry_signal_params", "short_entry_signal_params")
_NESTED = """
nfi_parameters = strategy_config.get("nfi_parameters")
if type(nfi_parameters) is dict:
    for nfi_param in nfi_parameters:
        if nfi_param in ["long_entry_signal_params", "short_entry_signal_params"]:
            continue
        if ((nfi_param in NFI_SAFE_PARAMETERS or is_config_advanced_mode)
                and hasattr(self, nfi_param)):
            setattr(self, nfi_param, nfi_parameters[nfi_param])
        else:
            pass
    self.update_signals_from_config(nfi_parameters)
"""
_LEGACY = """
for nfi_param in NFI_SAFE_PARAMETERS:
    if (nfi_param in strategy_config) and hasattr(self, nfi_param):
        setattr(self, nfi_param, strategy_config[nfi_param])
"""


class _WithoutLogs(ast.NodeTransformer):
    def visit_Expr(self, node: ast.Expr) -> ast.AST | None:
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "log"
            and node.value.func.attr in {"info", "warning", "debug"}
        ):
            return None
        return node

    def visit_If(self, node: ast.If) -> ast.AST:
        had_else = bool(node.orelse)
        self.generic_visit(node)
        if not node.body:
            node.body = [ast.Pass()]
        if had_else and not node.orelse:
            node.orelse = [ast.Pass()]
        return node


def _same(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def resolve_source_parameter_overrides(
    class_node: ast.ClassDef,
    constants: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not config:
        return {}
    methods = {node.name: node for node in class_node.body if isinstance(node, ast.FunctionDef)}
    initializer = methods.get("__init__")
    if initializer is None:
        return {}
    declarations = [
        node
        for node in initializer.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "NFI_SAFE_PARAMETERS"
    ]
    if not declarations:
        return {}
    if len(declarations) != 1:
        raise StrategyAnalysisError("source parameter allowlist is ambiguous")
    try:
        allowed = ast.literal_eval(declarations[0].value)
    except (ValueError, TypeError) as exc:
        raise StrategyAnalysisError("source parameter allowlist is not literal") from exc
    if not isinstance(allowed, list) or not all(isinstance(key, str) for key in allowed):
        raise StrategyAnalysisError("source parameter allowlist is invalid")
    nested = config.get("nfi_parameters")
    nested = nested if type(nested) is dict else {}
    _prove_update_order(initializer, methods)
    source_scalar_stages = "system_v4_name" in constants or bool(
        (MANAGED_SCALARS | {"sell_profit_only"}) & config.keys() or MANAGED_SCALARS & nested.keys()
    )
    overrides, late = (
        constructor_scalar_stages(initializer, config) if source_scalar_stages else ({}, {})
    )
    advanced = config.get("nfi_advanced_mode") == True  # noqa: E712 - source equality, including 1.
    _prove_parameter_bindings(initializer, set(allowed) | set(_SIGNALS))
    advanced_state = _advanced_parameter_states(class_node, initializer) if advanced else None
    if advanced_state is not None and source_scalar_stages:
        advanced_state = (advanced_state[0] - MANAGED_SCALARS, advanced_state[1] - MANAGED_SCALARS)
    parameter_defaults = dict(constants)
    parameter_defaults.update(_missing_container_parameters(class_node, parameter_defaults))
    if any(isinstance(base, ast.Name) and base.id == "IStrategy" for base in class_node.bases):
        parameter_defaults = INHERITED_SETTINGS | parameter_defaults
    initialized = (
        initialization_parameters(class_node, initializer, parameter_defaults, config, nested)
        if advanced
        else {}
    )
    for key, value in nested.items():
        if key in _SIGNALS:
            continue
        if key in initialized:
            continue
        if key in parameter_defaults and (key in allowed or advanced):
            if key not in allowed:
                assert advanced_state is not None
                _prove_advanced_parameter(advanced_state, key, parameter_defaults[key], value)
            overrides[key] = copy.deepcopy(value)
        elif advanced:
            raise StrategyAnalysisError(
                f"advanced source parameter {key!r} has no reviewed initialization contract"
            )
    _update_signals(overrides, constants, nested)
    for key in allowed:
        if key in config and key in constants:
            overrides[key] = copy.deepcopy(config[key])
    _update_signals(overrides, constants, config)
    overrides.update(late)
    overrides.update(initialized)
    # The manager separately proves the source's virtual-fee calculation.
    for key in ("custom_fee_open_rate", "custom_fee_close_rate"):
        value = overrides.get(key)
        if value is not None and (
            type(value) not in (int, float) or not 0 <= value < 1 or not math.isfinite(value)
        ):
            raise StrategyAnalysisError(f"source virtual fee parameter {key!r} must be in [0, 1)")
    # Freqtrade applies standard resolver attributes after construction.
    if RESOLVER_SETTINGS & methods.keys() & config.keys():
        raise StrategyAnalysisError("strategy resolver cannot replace a source method")
    overrides.update(resolver_overrides(dict(constants) | overrides, config))
    return overrides


def _missing_container_parameters(
    class_node: ast.ClassDef,
    constants: Mapping[str, Any],
) -> dict[str, Any]:
    """Recover literal container defaults excluded from the JSON class inventory."""
    values = {}
    for node in class_node.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id not in constants
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in {"frozenset", "set", "tuple", "list"}
            and len(node.value.args) == 1
            and not node.value.keywords
        ):
            continue
        try:
            literal = ast.literal_eval(node.value.args[0])
        except (ValueError, TypeError):
            continue
        if isinstance(literal, (list, tuple)):
            values[node.targets[0].id] = list(literal)
    return values


def _advanced_parameter_states(
    class_node: ast.ClassDef,
    initializer: ast.FunctionDef,
) -> tuple[set[str], set[str]]:
    def attributes(node: ast.AST):
        return (
            item
            for item in ast.walk(node)
            if isinstance(item, ast.Attribute)
            and isinstance(item.value, ast.Name)
            and item.value.id == "self"
        )

    return (
        {node.attr for node in attributes(initializer)},
        {node.attr for node in attributes(class_node) if not isinstance(node.ctx, ast.Load)},
    )


def _prove_advanced_parameter(
    state: tuple[set[str], set[str]],
    name: str,
    default: Any,
    value: Any,
) -> None:
    """Freeze data attributes whose only instance write is the proven update loop.

    Class-body aliases keep their original values: assigning an instance attribute
    does not rerun Python's class body. Initialization-managed attributes and mutable
    resources require their own lifecycle contracts, rather than scalar substitution.
    """
    if name in state[0]:
        raise StrategyAnalysisError(
            f"advanced source parameter {name!r} is consumed during initialization"
        )
    if name in state[1]:
        raise StrategyAnalysisError(f"advanced source parameter {name!r} is mutable runtime state")
    compatible = (
        valid_resolver_value(name, value)
        if name in RESOLVER_SETTINGS
        else _compatible_parameter_value(default, value)
    )
    if not compatible:
        raise StrategyAnalysisError(f"advanced source parameter {name!r} has an invalid value")


def _compatible_parameter_value(default: Any, value: Any) -> bool:
    if type(default) is bool:
        return type(value) is bool
    if type(default) in (int, float):
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            return False
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, (list, tuple)):
        return isinstance(value, list) and all(
            any(_compatible_parameter_value(example, item) for example in default) for item in value
        )
    if isinstance(default, dict):
        return isinstance(value, dict) and all(
            key in default and _compatible_parameter_value(default[key], item)
            for key, item in value.items()
        )
    return default is None and value is None


def _prove_update_order(initializer: ast.FunctionDef, methods: dict[str, ast.FunctionDef]) -> None:
    clean = _WithoutLogs().visit(copy.deepcopy(initializer))
    assert isinstance(clean, ast.FunctionDef)
    expected_nested = ast.parse(_NESTED).body
    indices = [
        i
        for i in range(len(clean.body) - 1)
        if all(_same(a, b) for a, b in zip(clean.body[i : i + 2], expected_nested, strict=True))
    ]
    legacy = ast.parse(_LEGACY).body[0]
    legacy_indices = [i for i, node in enumerate(clean.body) if _same(node, legacy)]
    signal_call = ast.parse("self.update_signals_from_config(strategy_config)").body[0]
    signal_indices = [i for i, node in enumerate(clean.body) if _same(node, signal_call)]
    if (
        len(indices) != 1
        or len(legacy_indices) != 1
        or len(signal_indices) != 1
        or not indices[0] < legacy_indices[0] < signal_indices[0]
    ):
        raise StrategyAnalysisError("source parameter precedence or update loops changed")
    binding = ast.parse("strategy_config = self.config").body[0]
    if not any(_same(node, binding) for node in clean.body[: indices[0]]):
        raise StrategyAnalysisError("source parameter configuration binding changed")
    advanced = ast.parse(
        'is_config_advanced_mode = "nfi_advanced_mode" in strategy_config '
        'and strategy_config["nfi_advanced_mode"] == True'
    ).body[0]
    if not any(_same(node, advanced) for node in clean.body[: indices[0]]):
        raise StrategyAnalysisError("source advanced-mode predicate changed")
    if (
        sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            for node in ast.walk(clean)
        )
        != 2
    ):
        raise StrategyAnalysisError("source constructor has additional dynamic attribute writes")
    helper = methods.get("update_signals_from_config")
    if helper is None:
        raise StrategyAnalysisError("source signal configuration helper is missing")
    expected = []
    for side in ("long", "short"):
        key = f"{side}_entry_signal_params"
        expected.extend(
            ast.parse(f'''
if hasattr(self, "{key}") and "{key}" in config:
    {key} = self.{key}
    config_{key} = config["{key}"]
    for condition_key in {key}:
        if condition_key in config_{key}:
            {key}[condition_key] = config_{key}[condition_key]
''').body
        )
    if not _same(
        ast.Module(body=helper.body, type_ignores=[]), ast.Module(body=expected, type_ignores=[])
    ):
        raise StrategyAnalysisError("source signal configuration update semantics changed")


def _prove_parameter_bindings(initializer: ast.FunctionDef, parameters: set[str]) -> None:
    nodes = list(ast.walk(initializer))
    for name in ("NFI_SAFE_PARAMETERS", "strategy_config", "is_config_advanced_mode"):
        writes = [
            node
            for node in nodes
            if isinstance(node, ast.Name) and node.id == name and not isinstance(node.ctx, ast.Load)
        ]
        if len(writes) != 1:
            raise StrategyAnalysisError(f"source parameter binding {name!r} is reassigned")
    reads = [
        node
        for node in nodes
        if isinstance(node, ast.Name)
        and node.id == "NFI_SAFE_PARAMETERS"
        and isinstance(node.ctx, ast.Load)
    ]
    if len(reads) != 2:
        raise StrategyAnalysisError("source parameter allowlist has additional uses")
    for node in nodes:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in parameters
            and not isinstance(node.ctx, ast.Load)
        ):
            raise StrategyAnalysisError("source constructor has an additional parameter write")
    calls = [
        node
        for node in nodes
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "update_signals_from_config"
    ]
    if len(calls) != 2:
        raise StrategyAnalysisError("source constructor has additional signal updates")


def _update_signals(
    overrides: dict[str, Any],
    constants: Mapping[str, Any],
    config: Mapping[str, Any],
) -> None:
    for key in _SIGNALS:
        if key not in config or key not in constants:
            continue
        original = overrides.get(key, constants[key])
        configured = config[key]
        if not isinstance(original, dict) or not isinstance(configured, dict):
            raise StrategyAnalysisError(f"source signal parameter {key!r} must be a mapping")
        overrides[key] = {
            name: copy.deepcopy(configured.get(name, value)) for name, value in original.items()
        }


def configured_analysis(
    analysis: dict[str, Any],
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not config:
        return analysis
    strategy = analysis["strategies"][0]
    source = analysis["source"]
    data = Path(source["path"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise StrategyAnalysisError("source configuration hash differs from analysis")
    class_node = next(
        node
        for node in ast.parse(data).body
        if isinstance(node, ast.ClassDef) and node.name == strategy["name"]
    )
    overrides = resolve_source_parameter_overrides(class_node, strategy["constants"], config)
    if not overrides:
        return analysis
    result = copy.deepcopy(analysis)
    result["strategies"][0]["constants"].update(overrides)
    result["source_configuration"] = {"overrides": overrides}
    return result
