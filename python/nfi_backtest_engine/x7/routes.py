"""Managed X7 route/tag inventory and rebuy terminal-exit validation."""

from __future__ import annotations

import ast
import copy
import hashlib
import math
from typing import Any, cast

from ..errors import StrategyAnalysisError
from .trade_manager import (
    _LONG_EXIT_REBUY_BASE_AST_SHA256,
    _MANAGED_LONG_METHOD_SHA256,
    _MANAGED_LONG_ROUTE_SPECS,
    _MANAGED_LONG_STATEFUL_FEATURES,
    _MANAGED_SHORT_METHOD_SHA256,
    _MANAGED_SHORT_ROUTE_SPECS,
    _QUICK_RAPID_STATEFUL_FEATURES,
    _ROUTE_STOP_CONSTANTS,
)


def _validate_managed_long_method_identity(
    methods: dict[str, ast.FunctionDef],
    method_records: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Reject a missing or changed stateful managed-long callback.

    Scalar predicates remain source-compiled, but routing, target-cache writes,
    stop order, and the quick/rapid inline predicates are implemented directly
    in Rust. All of those observable bodies must match the reviewed snapshot.
    """
    missing = [name for name in _MANAGED_LONG_METHOD_SHA256 if name not in methods]
    if missing:
        raise StrategyAnalysisError(
            "NFI X7 managed-long state machine is missing: " + ", ".join(missing)
        )
    changed = [
        name
        for name, expected in _MANAGED_LONG_METHOD_SHA256.items()
        if name != "long_exit_rebuy"
        if method_records.get(name, {}).get("source_sha256") != expected
    ]
    rebuy_terminal_exit, terminal_index = _extract_rebuy_terminal_exit(methods["long_exit_rebuy"])
    if (
        _method_ast_sha256(
            methods["long_exit_rebuy"],
            remove_statement_index=terminal_index,
        )
        != _LONG_EXIT_REBUY_BASE_AST_SHA256
    ):
        changed.append("long_exit_rebuy")
    if changed:
        raise StrategyAnalysisError(
            "NFI X7 managed-long route changed; exact lowering requires review: "
            + ", ".join(changed)
        )
    return rebuy_terminal_exit


def _extract_rebuy_terminal_exit(
    method: ast.FunctionDef,
) -> tuple[dict[str, Any] | None, int | None]:
    """Extract the optional pure terminal exit appended to `long_exit_rebuy`.

    The stateful portion of the method remains pinned by its masked AST hash.
    Only this closed expression shape is dynamic: exact entry tags, elapsed
    trade age, initial-basis profit, and a literal exit reason.
    """

    if len(method.body) < 2:
        return None, None
    statement_index = len(method.body) - 2
    statement = method.body[statement_index]
    if (
        not isinstance(statement, ast.If)
        or statement.orelse
        or len(statement.body) != 1
        or not isinstance(statement.test, ast.BoolOp)
        or not isinstance(statement.test.op, ast.And)
        or len(statement.test.values) != 3
    ):
        return None, None
    returned = statement.body[0]
    if (
        not isinstance(returned, ast.Return)
        or not isinstance(returned.value, ast.Tuple)
        or len(returned.value.elts) != 2
        or not isinstance(returned.value.elts[0], ast.Constant)
        or returned.value.elts[0].value is not True
        or not isinstance(returned.value.elts[1], ast.Constant)
        or not isinstance(returned.value.elts[1].value, str)
        or not returned.value.elts[1].value
    ):
        return None, None

    entry_tags: list[str] | None = None
    minimum_age_seconds: float | None = None
    minimum_profit_ratio: float | None = None
    for condition in statement.test.values:
        tags = _exact_string_list_comparison(condition, "enter_tags")
        if tags is not None:
            entry_tags = tags
            continue
        age = _elapsed_trade_seconds_comparison(condition)
        if age is not None:
            minimum_age_seconds = age
            continue
        profit = _minimum_number_comparison(condition, "profit_init_ratio")
        if profit is not None:
            minimum_profit_ratio = profit

    age_ms = minimum_age_seconds * 1_000.0 if minimum_age_seconds is not None else math.nan
    if (
        entry_tags is None
        or not entry_tags
        or len(entry_tags) != len(set(entry_tags))
        or not all(entry_tags)
        or not math.isfinite(age_ms)
        or age_ms <= 0.0
        or not age_ms.is_integer()
        or minimum_profit_ratio is None
        or not math.isfinite(minimum_profit_ratio)
    ):
        return None, None
    return (
        {
            "entry_tags": entry_tags,
            "minimum_age_ms": int(age_ms),
            "minimum_profit_ratio": minimum_profit_ratio,
            "reason": returned.value.elts[1].value,
        },
        statement_index,
    )


def _exact_string_list_comparison(node: ast.AST, name: str) -> list[str] | None:
    if (
        not isinstance(node, ast.Compare)
        or not isinstance(node.left, ast.Name)
        or node.left.id != name
        or len(node.ops) != 1
        or not isinstance(node.ops[0], ast.Eq)
        or len(node.comparators) != 1
        or not isinstance(node.comparators[0], ast.List | ast.Tuple)
    ):
        return None
    values = node.comparators[0].elts
    if not all(
        isinstance(value, ast.Constant) and isinstance(value.value, str) for value in values
    ):
        return None
    return [cast(str, cast(ast.Constant, value).value) for value in values]


def _elapsed_trade_seconds_comparison(node: ast.AST) -> float | None:
    if (
        not isinstance(node, ast.Compare)
        or len(node.ops) != 1
        or not isinstance(node.ops[0], ast.GtE)
        or len(node.comparators) != 1
        or not isinstance(node.left, ast.Call)
        or node.left.args
        or node.left.keywords
        or not isinstance(node.left.func, ast.Attribute)
        or node.left.func.attr != "total_seconds"
        or not isinstance(node.left.func.value, ast.BinOp)
        or not isinstance(node.left.func.value.op, ast.Sub)
        or not isinstance(node.left.func.value.left, ast.Name)
        or node.left.func.value.left.id != "current_time"
        or not isinstance(node.left.func.value.right, ast.Attribute)
        or node.left.func.value.right.attr != "open_date_utc"
        or not isinstance(node.left.func.value.right.value, ast.Name)
        or node.left.func.value.right.value.id != "trade"
    ):
        return None
    return _numeric_expression(node.comparators[0])


def _minimum_number_comparison(node: ast.AST, name: str) -> float | None:
    if (
        not isinstance(node, ast.Compare)
        or not isinstance(node.left, ast.Name)
        or node.left.id != name
        or len(node.ops) != 1
        or not isinstance(node.ops[0], ast.GtE)
        or len(node.comparators) != 1
    ):
        return None
    return _numeric_expression(node.comparators[0])


def _numeric_expression(node: ast.AST) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        value = _numeric_expression(node.operand)
        if value is None:
            return None
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and isinstance(
        node.op,
        ast.Add | ast.Sub | ast.Mult | ast.Div,
    ):
        left = _numeric_expression(node.left)
        right = _numeric_expression(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0.0:
            return None
        return left / right
    return None


def _method_ast_sha256(
    method: ast.FunctionDef,
    *,
    remove_statement_index: int | None,
) -> str:
    normalized = copy.deepcopy(method)
    if remove_statement_index is not None:
        del normalized.body[remove_statement_index]
    # Python 3.13 changed ``ast.dump`` to omit empty optional fields by
    # default.  The fields are still part of the same AST, so relying on that
    # default made a reviewed callback appear different solely because the
    # user ran the engine on a newer supported Python version.
    #
    # ``show_empty`` does not exist on Python 3.12.  Calling through ``Any``
    # keeps the compatibility branch explicit without weakening the public
    # package's 3.12 type-checking target.
    dump = cast(Any, ast.dump)
    try:
        serialized = dump(normalized, include_attributes=False, show_empty=True)
    except TypeError:
        serialized = ast.dump(normalized, include_attributes=False)
    encoded = serialized.encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_managed_short_method_identity(
    methods: dict[str, ast.FunctionDef],
    method_records: dict[str, dict[str, Any]],
) -> None:
    """Pin the stateful wrapper for the first executable short route."""
    missing = [name for name in _MANAGED_SHORT_METHOD_SHA256 if name not in methods]
    if missing:
        raise StrategyAnalysisError(
            "NFI X7 managed-short state machine is missing: " + ", ".join(missing)
        )
    changed = [
        name
        for name, expected in _MANAGED_SHORT_METHOD_SHA256.items()
        if method_records.get(name, {}).get("source_sha256") != expected
    ]
    if changed:
        raise StrategyAnalysisError(
            "NFI X7 managed-short route changed; exact lowering requires review: "
            + ", ".join(changed)
        )


def _build_managed_short_routes(constants: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Freeze every reviewed short exit route in upstream source order.

    The profiles reuse the typed exit/target policy shape, but they remain
    separate descriptors with disjoint source tags. In particular,
    ``short_top_coins_fallback`` deliberately names ``short_normal`` because
    that is the fallback callback upstream actually executes.
    """
    routes: dict[str, dict[str, Any]] = {}
    claimed_tags: set[str] = set()
    for spec in _MANAGED_SHORT_ROUTE_SPECS:
        mode_name = constants.get(spec.mode_constant)
        entry_tags = constants.get(spec.tags_constant)
        if not isinstance(mode_name, str) or not mode_name:
            raise StrategyAnalysisError(f"NFI {spec.key} mode name must be frozen")
        if (
            not isinstance(entry_tags, list)
            or not entry_tags
            or not all(isinstance(tag, str) and tag for tag in entry_tags)
        ):
            raise StrategyAnalysisError(f"NFI {spec.key} entry tags must be frozen strings")
        unique_tags = sorted(set(entry_tags))
        overlap = claimed_tags.intersection(unique_tags)
        if overlap:
            raise StrategyAnalysisError(
                f"NFI managed-short entry tags overlap at {', '.join(sorted(overlap))}"
            )
        claimed_tags.update(unique_tags)

        indexed_fields = {
            name: list(fields) for name, fields in _MANAGED_LONG_STATEFUL_FEATURES.items()
        }
        if spec.profile in {"quick", "rapid"}:
            for name, fields in _QUICK_RAPID_STATEFUL_FEATURES.items():
                indexed_fields.setdefault(name, []).extend(fields)
                indexed_fields[name] = sorted(set(indexed_fields[name]))
        route: dict[str, Any] = {
            "profile": spec.profile,
            "mode_name": mode_name,
            "entry_tags": unique_tags,
            "decision_program_order": list(spec.program_order),
            "stateful_order": [
                "decision_programs",
                "profile_inline_exit",
                "profile_stoploss",
                "exit_profit_target",
                "profit_target_update",
                "ignored_signal_filter",
            ],
            "stateful_input_contract": {"indexed_fields": indexed_fields},
        }
        stop_constants = _ROUTE_STOP_CONSTANTS.get(spec.profile)
        if stop_constants is not None:
            futures_name, spot_name = stop_constants
            futures = constants.get(futures_name)
            spot = constants.get(spot_name)
            if any(
                isinstance(value, bool) or not isinstance(value, int | float)
                for value in (futures, spot)
            ):
                raise StrategyAnalysisError(
                    f"NFI {spec.key} system-v3.2 stop thresholds must be numeric"
                )
            route["stop_threshold_futures"] = futures
            route["stop_threshold_spot"] = spot
        routes[spec.key] = route

    expected_adjustment_tags = constants.get("short_adjust_mode_tags")
    actual_adjustment_tags = sorted(
        tag for key, route in routes.items() if key != "short_rebuy" for tag in route["entry_tags"]
    )
    if (
        not isinstance(expected_adjustment_tags, list)
        or sorted(set(expected_adjustment_tags)) != actual_adjustment_tags
    ):
        raise StrategyAnalysisError("NFI short adjustment tag routing changed")
    return routes


def _build_managed_long_routes(constants: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Freeze the eight system-v3.2 managed-long routes.

    The route profile is deliberately closed. It tells Rust which reviewed
    branch order to execute; arbitrary thresholds or callback names cannot be
    injected through a manifest. Rebuy has a dedicated first-stage adjustment
    evaluator, then joins the shared grind-v3 state machine after de-risking.
    """
    routes: dict[str, dict[str, Any]] = {}
    claimed_tags: set[str] = set()
    for spec in _MANAGED_LONG_ROUTE_SPECS:
        mode_name = constants.get(spec.mode_constant)
        entry_tags = constants.get(spec.tags_constant)
        if not isinstance(mode_name, str) or not mode_name:
            raise StrategyAnalysisError(f"NFI {spec.key} mode name must be frozen")
        if (
            not isinstance(entry_tags, list)
            or not entry_tags
            or not all(isinstance(tag, str) and tag for tag in entry_tags)
        ):
            raise StrategyAnalysisError(f"NFI {spec.key} entry tags must be frozen strings")
        unique_tags = sorted(set(entry_tags))
        overlap = claimed_tags.intersection(unique_tags)
        if overlap:
            raise StrategyAnalysisError(
                f"NFI managed-long entry tags overlap at {', '.join(sorted(overlap))}"
            )
        claimed_tags.update(unique_tags)

        indexed_fields = {
            name: list(fields) for name, fields in _MANAGED_LONG_STATEFUL_FEATURES.items()
        }
        if spec.profile in {"quick", "rapid"}:
            for name, fields in _QUICK_RAPID_STATEFUL_FEATURES.items():
                indexed_fields.setdefault(name, []).extend(fields)
                indexed_fields[name] = sorted(set(indexed_fields[name]))

        route: dict[str, Any] = {
            "profile": spec.profile,
            "mode_name": mode_name,
            "entry_tags": unique_tags,
            "decision_program_order": list(spec.program_order),
            "stateful_order": [
                "decision_programs",
                "profile_inline_exit",
                "profile_stoploss",
                "exit_profit_target",
                "profit_target_update",
                "ignored_signal_filter",
            ],
            "stateful_input_contract": {"indexed_fields": indexed_fields},
        }
        stop_constants = _ROUTE_STOP_CONSTANTS.get(spec.profile)
        if stop_constants is not None:
            futures_name, spot_name = stop_constants
            futures = constants.get(futures_name)
            spot = constants.get(spot_name)
            if any(
                isinstance(value, bool) or not isinstance(value, int | float)
                for value in (futures, spot)
            ):
                raise StrategyAnalysisError(
                    f"NFI {spec.key} system-v3.2 stop thresholds must be numeric"
                )
            route["stop_threshold_futures"] = futures
            route["stop_threshold_spot"] = spot
        routes[spec.key] = route
    return routes


def _top_coins_program_order(node: ast.FunctionDef) -> tuple[str, ...] | None:
    """Read the literal callback order from ``for exit_func in (...)``."""
    for item in ast.walk(node):
        if (
            not isinstance(item, ast.For)
            or not isinstance(item.target, ast.Name)
            or item.target.id != "exit_func"
            or not isinstance(item.iter, ast.Tuple)
        ):
            continue
        names: list[str] = []
        for element in item.iter.elts:
            if (
                not isinstance(element, ast.Attribute)
                or not isinstance(element.value, ast.Name)
                or element.value.id != "self"
            ):
                return None
            names.append(element.attr)
        return tuple(names)
    return None
