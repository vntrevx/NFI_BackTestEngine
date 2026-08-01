"""Source-bound rebuy and managed position-adjustment descriptors."""

from __future__ import annotations

import ast
import math
import re
from typing import Any

from ..errors import StrategyAnalysisError
from .trade_manager import (
    _ADJUSTMENT_BOOL_CONSTANTS,
    _ADJUSTMENT_GRIND_FIELDS,
    _ADJUSTMENT_METHOD_SHA256,
    _ADJUSTMENT_NUMBER_CONSTANTS,
    _REBUY_ADJUSTMENT_LIST_CONSTANTS,
    _REBUY_ADJUSTMENT_NUMBER_CONSTANTS,
)


def _validate_adjustment_method_identity(
    methods: dict[str, dict[str, Any]],
) -> None:
    """Reject an unreviewed stateful adjustment implementation.

    Pure entry predicates are still compiled from the supplied strategy. The
    surrounding order walk and stake-return logic are handwritten because they
    mutate trade state; every method that can affect that contract is pinned.
    """
    changed = [
        name
        for name, expected in _ADJUSTMENT_METHOD_SHA256.items()
        if methods.get(name, {}).get("source_sha256") not in expected
    ]
    if changed:
        raise StrategyAnalysisError(
            "NFI X7 position adjustment changed; exact lowering requires review: "
            + ", ".join(changed)
        )


def _build_adjustment_constants(
    constants: dict[str, Any],
    method: ast.FunctionDef,
    *,
    side: str,
) -> dict[str, Any]:
    """Freeze one side's reachable system-v3.2 adjustment constants.

    Buyback and level-4 de-risk branches are deliberately required to be
    disabled. Supporting a disabled branch by omission is exact; accepting it
    after a strategy change would not be.
    """
    if side not in {"long", "short"}:
        raise StrategyAnalysisError(f"NFI adjustment side is invalid: {side}")
    for name in _ADJUSTMENT_BOOL_CONSTANTS:
        if not isinstance(constants.get(name), bool):
            raise StrategyAnalysisError(f"NFI adjustment constant {name} must be boolean")
    for name in _ADJUSTMENT_NUMBER_CONSTANTS:
        value = constants.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise StrategyAnalysisError(f"NFI adjustment constant {name} must be numeric")
    if not constants["position_adjustment_enable"]:
        raise StrategyAnalysisError("NFI position adjustment is disabled")
    if constants["system_v3_buyback_1_enable"]:
        raise StrategyAnalysisError("NFI buyback route is not lowered")

    grind_levels = _numbered_constant_levels(constants, r"system_v3_grind_(\d+)_enable")
    grinds: list[dict[str, Any]] = []
    for level in grind_levels:
        prefix = f"system_v3_grind_{level}_"
        record: dict[str, Any] = {
            "level": level,
            "enabled": _boolean_constant(constants, f"{prefix}enable"),
            "use_derisk": _boolean_constant(constants, f"{prefix}use_derisk"),
        }
        for field in _ADJUSTMENT_GRIND_FIELDS:
            name = f"{prefix}{field}"
            value = constants.get(name)
            if field.startswith(("stakes_", "thresholds_")):
                if (
                    not isinstance(value, list)
                    or not value
                    or any(
                        isinstance(item, bool) or not isinstance(item, int | float)
                        for item in value
                    )
                ):
                    raise StrategyAnalysisError(
                        f"NFI adjustment constant {name} must be a numeric list"
                    )
            elif isinstance(value, bool) or not isinstance(value, int | float):
                raise StrategyAnalysisError(f"NFI adjustment constant {name} must be numeric")
            record[field] = value
        for mode in ("futures", "spot"):
            if len(record[f"stakes_{mode}"]) != len(record[f"thresholds_{mode}"]):
                raise StrategyAnalysisError(
                    f"NFI grind {level} stake/threshold lengths differ for {mode}"
                )
        grinds.append(record)

    derisk_levels = _method_derisk_levels(method)
    derisk_records = []
    for level in derisk_levels:
        prefix = f"system_v3_2_derisk_level_{level}_"
        derisk_record: dict[str, Any] = {
            "level": level,
            "enabled": _boolean_constant(constants, f"{prefix}enable"),
        }
        for mode in ("futures", "spot"):
            pair_name = f"{prefix}{mode}"
            values = constants.get(pair_name)
            if (
                not isinstance(values, list)
                or len(values) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, int | float)
                    for value in values
                )
            ):
                raise StrategyAnalysisError(
                    f"NFI adjustment constant {pair_name} must be a numeric pair"
                )
            stake_name = f"{prefix}stake_{mode}"
            stake = constants.get(stake_name)
            if isinstance(stake, bool) or not isinstance(stake, int | float):
                raise StrategyAnalysisError(
                    f"NFI adjustment constant {stake_name} must be numeric"
                )
            derisk_record[f"threshold_{mode}"] = values[1]
            derisk_record[f"stake_{mode}"] = stake
        derisk_records.append(derisk_record)

    return {
        "derisk_enable": constants["derisk_enable"],
        "max_stake_multiplier": constants["system_v3_max_stake"],
        "rebuy_stake_multiplier": constants["system_v3_rebuy_mode_stake_multiplier"],
        "derisk_levels": derisk_records,
        "grinds": grinds,
        "policy": _adjustment_literal_policy(method, side=side, grind_levels=grind_levels),
    }


def _numbered_constant_levels(constants: dict[str, Any], pattern: str) -> list[int]:
    matcher = re.compile(f"^{pattern}$")
    levels = sorted(
        {
            int(match.group(1))
            for name in constants
            if (match := matcher.fullmatch(name)) is not None
        }
    )
    if not levels or levels != list(range(levels[0], levels[-1] + 1)) or levels[0] != 1:
        raise StrategyAnalysisError(f"NFI adjustment levels are not contiguous: {pattern}")
    return levels


def _method_derisk_levels(method: ast.FunctionDef) -> list[int]:
    levels = sorted(
        {
            int(match.group(1))
            for node in ast.walk(method)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Tuple)
            and len(node.value.elts) == 2
            and isinstance(node.value.elts[1], ast.Constant)
            and isinstance(node.value.elts[1].value, str)
            and (
                match := re.fullmatch(r"derisk_level_(\d+)", node.value.elts[1].value)
            )
            is not None
        }
    )
    if not levels or levels != list(range(1, levels[-1] + 1)):
        raise StrategyAnalysisError("NFI adjustment de-risk action levels are not contiguous")
    return levels


def _boolean_constant(constants: dict[str, Any], name: str) -> bool:
    value = constants.get(name)
    if not isinstance(value, bool):
        raise StrategyAnalysisError(f"NFI adjustment constant {name} must be boolean")
    return value


def _adjustment_literal_policy(
    method: ast.FunctionDef,
    *,
    side: str = "long",
    grind_levels: list[int] | None = None,
) -> dict[str, Any]:
    """Extract non-constant entry gates from the reviewed stateful callback.

    The grind stake tables are class constants, but X7 keeps retry windows and
    two late-level fallback predicates as literals inside the method. Encoding
    those expressions as typed operands keeps Rust strategy-version agnostic:
    a reviewed upstream change updates this IR instead of requiring a hidden
    engine constant.
    """

    retry = _named_assignment(method, "grind_entry_retry_time")
    retry_ms = _duration_subtraction_ms(retry, unit="minutes")
    extra = _named_assignment(method, f"is_{side}_extra_checks_entry")
    stale_durations = _timedelta_values_ms(extra, unit="hours")
    extra_profit_conditions = [
        descriptor
        for node in ast.walk(extra)
        if isinstance(node, ast.Compare)
        and (descriptor := _adjustment_comparison(node)) is not None
        and descriptor["left"] == {"kind": "variable", "name": "slice_profit"}
    ]
    extra_derisk_levels = sorted(
        {
            int(node.id.removeprefix("is_derisk_"))
            for node in ast.walk(extra)
            if isinstance(node, ast.Name)
            and node.id.startswith("is_derisk_")
            and node.id.removeprefix("is_derisk_").isdecimal()
        }
    )
    if (
        retry_ms <= 0
        or len(stale_durations) != 1
        or len(extra_profit_conditions) != 1
        or not extra_derisk_levels
    ):
        raise StrategyAnalysisError(
            "NFI adjustment extra-entry policy changed; exact lowering requires review"
        )

    if grind_levels is None:
        grind_levels = sorted(
            {
                int(match.group(1))
                for node in ast.walk(method)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and (
                    match := re.fullmatch(r"system_v3_grind_(\d+)_enable", node.attr)
                )
                is not None
            }
        )
    if not grind_levels:
        raise StrategyAnalysisError("NFI adjustment has no source-defined Grind levels")
    fallback_records = [
        _grind_entry_fallbacks(method, level=level, side=side) for level in grind_levels
    ]
    return {
        "entry_retry_ms": retry_ms,
        "stale_order_ms": stale_durations[0],
        "extra_entry_profit_condition": extra_profit_conditions[0],
        "extra_entry_derisk_levels": extra_derisk_levels,
        "grind_entry_fallbacks": fallback_records,
    }


def _grind_entry_fallbacks(
    method: ast.FunctionDef,
    *,
    level: int,
    side: str,
) -> dict[str, Any]:
    tag = f"grind_{level}_entry"
    candidates = [
        node
        for node in method.body
        if isinstance(node, ast.If)
        and any(isinstance(value, ast.Constant) and value.value == tag for value in ast.walk(node))
        and any(
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "self"
            and value.attr == f"system_v3_grind_{level}_enable"
            for value in ast.walk(node.test)
        )
    ]
    if len(candidates) != 1 or not isinstance(candidates[0].test, ast.BoolOp):
        raise StrategyAnalysisError(f"NFI adjustment grind {level} entry structure changed")
    signal_terms = [
        value
        for value in candidates[0].test.values
        if any(
            isinstance(node, ast.Name) and node.id == f"is_{side}_grind_entry"
            for node in ast.walk(value)
        )
    ]
    if len(signal_terms) != 1:
        raise StrategyAnalysisError(f"NFI adjustment grind {level} signal gate changed")
    signal = signal_terms[0]
    signal_name = f"is_{side}_grind_entry"
    if isinstance(signal, ast.Name) and signal.id == signal_name:
        predicates: list[dict[str, Any]] = []
    elif isinstance(signal, ast.BoolOp) and isinstance(signal.op, ast.Or):
        if (
            not signal.values
            or not isinstance(signal.values[0], ast.Name)
            or signal.values[0].id != signal_name
        ):
            raise StrategyAnalysisError(f"NFI adjustment grind {level} primary signal changed")
        predicates = [_adjustment_predicate(value, level=level) for value in signal.values[1:]]
    else:
        raise StrategyAnalysisError(f"NFI adjustment grind {level} signal expression changed")
    return {"level": level, "predicates": predicates}


def _adjustment_predicate(node: ast.AST, *, level: int) -> dict[str, Any]:
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.And):
        raise StrategyAnalysisError(
            f"NFI adjustment grind {level} fallback must be an AND expression"
        )
    any_derisk_levels: list[int] = []
    conditions: list[dict[str, Any]] = []
    for value in node.values:
        if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
            levels = sorted(
                {
                    int(item.id.removeprefix("is_derisk_").removesuffix("_found"))
                    for item in value.values
                    if isinstance(item, ast.Name)
                    and item.id.startswith("is_derisk_")
                    and item.id.endswith("_found")
                    and item.id.removeprefix("is_derisk_").removesuffix("_found").isdecimal()
                }
            )
            if levels and len(levels) == len(value.values):
                any_derisk_levels.extend(levels)
                continue
        condition = _adjustment_comparison(value)
        if condition is None:
            raise StrategyAnalysisError(f"NFI adjustment grind {level} fallback condition changed")
        conditions.append(condition)
    if not conditions:
        raise StrategyAnalysisError(
            f"NFI adjustment grind {level} fallback has no numeric conditions"
        )
    return {
        "any_derisk_levels": sorted(set(any_derisk_levels)),
        "conditions": conditions,
    }


def _adjustment_comparison(node: ast.AST) -> dict[str, Any] | None:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
        return None
    operator = {
        ast.Lt: "lt",
        ast.Gt: "gt",
        ast.Eq: "eq",
    }.get(type(node.ops[0]))
    left = _adjustment_operand(node.left)
    right = _adjustment_operand(node.comparators[0])
    if operator is None or left is None or right is None:
        return None
    return {
        "left": left,
        "operator": operator,
        "right": right,
    }


def _adjustment_operand(node: ast.AST) -> dict[str, Any] | None:
    number = _ast_number(node)
    if number is not None:
        return {"kind": "literal", "value": number}
    if isinstance(node, ast.Name) and node.id in {
        "slice_profit",
        "slice_profit_entry",
        "num_open_grinds_and_buybacks",
    }:
        return {"kind": "variable", "name": node.id}
    feature = _last_candle_feature(node)
    if feature is not None:
        return {"kind": "feature", "name": feature, "multiplier": 1.0}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        feature = _last_candle_feature(node.left)
        multiplier = _ast_number(node.right)
        if feature is None or multiplier is None or not math.isfinite(multiplier):
            return None
        return {
            "kind": "feature",
            "name": feature,
            "multiplier": multiplier,
        }
    return None


def _last_candle_feature(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "last_candle"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
        and node.slice.value
    ):
        return node.slice.value
    return None


def _named_assignment(method: ast.FunctionDef, name: str) -> ast.AST:
    values = [
        node.value
        for node in method.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
    ]
    if len(values) != 1:
        raise StrategyAnalysisError(f"NFI adjustment assignment changed: {name}")
    return values[0]


def _duration_subtraction_ms(node: ast.AST, *, unit: str) -> int:
    if (
        not isinstance(node, ast.BinOp)
        or not isinstance(node.op, ast.Sub)
        or not isinstance(node.left, ast.Name)
        or node.left.id != "current_time"
    ):
        raise StrategyAnalysisError("NFI adjustment retry timestamp changed")
    values = _timedelta_values_ms(node.right, unit=unit)
    if len(values) != 1:
        raise StrategyAnalysisError("NFI adjustment retry duration changed")
    return values[0]


def _timedelta_values_ms(node: ast.AST, *, unit: str) -> list[int]:
    unit_ms = {"minutes": 60_000, "hours": 3_600_000}[unit]
    result: list[int] = []
    for call in ast.walk(node):
        if (
            not isinstance(call, ast.Call)
            or not isinstance(call.func, ast.Name)
            or call.func.id != "timedelta"
        ):
            continue
        for keyword in call.keywords:
            value = _ast_number(keyword.value)
            milliseconds = value * unit_ms if value is not None else math.nan
            if (
                keyword.arg == unit
                and math.isfinite(milliseconds)
                and milliseconds > 0
                and milliseconds.is_integer()
            ):
                result.append(int(milliseconds))
    return sorted(set(result))


def _ast_number(node: ast.AST) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int | float)
        and not isinstance(node.operand.value, bool)
    ):
        return -float(node.operand.value)
    return None


def _build_rebuy_adjustment_constants(constants: dict[str, Any]) -> dict[str, Any]:
    """Freeze the separate system-v3 rebuy ladder used by tags 61-65."""
    lists: dict[str, list[int | float]] = {}
    for name in _REBUY_ADJUSTMENT_LIST_CONSTANTS:
        value = constants.get(name)
        if (
            not isinstance(value, list)
            or not value
            or any(isinstance(item, bool) or not isinstance(item, int | float) for item in value)
        ):
            raise StrategyAnalysisError(
                f"NFI rebuy adjustment constant {name} must be a numeric list"
            )
        lists[name] = value
    for mode in ("futures", "spot"):
        if len(lists[f"system_v3_rebuy_mode_stakes_{mode}"]) != len(
            lists[f"system_v3_rebuy_mode_thresholds_{mode}"]
        ):
            raise StrategyAnalysisError(f"NFI rebuy stake/threshold lengths differ for {mode}")
    numbers: dict[str, int | float] = {}
    for name in _REBUY_ADJUSTMENT_NUMBER_CONSTANTS:
        value = constants.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise StrategyAnalysisError(f"NFI rebuy adjustment constant {name} must be numeric")
        numbers[name] = value
    return {
        "derisk_enable": constants["derisk_enable"],
        "stakes_futures": lists["system_v3_rebuy_mode_stakes_futures"],
        "stakes_spot": lists["system_v3_rebuy_mode_stakes_spot"],
        "thresholds_futures": lists["system_v3_rebuy_mode_thresholds_futures"],
        "thresholds_spot": lists["system_v3_rebuy_mode_thresholds_spot"],
        "derisk_futures": numbers["system_v3_rebuy_mode_derisk_futures"],
        "derisk_spot": numbers["system_v3_rebuy_mode_derisk_spot"],
    }
