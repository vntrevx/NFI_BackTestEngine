"""Legacy grind and tag-121 regular-adjustment route descriptors."""

from __future__ import annotations

import ast
import re
from typing import Any

from ..errors import StrategyAnalysisError
from .legacy_grind_ir import compile_legacy_grind_ir, extract_legacy_futures_fallback
from .trade_manager import (
    _LONG_BTC_ADJUSTMENT_SCOPE,
    _LONG_BTC_METHOD_SHA256,
    _LONG_BTC_STATEFUL_METHODS,
    _LONG_GRIND_ADJUSTMENT_SCOPE,
    _LONG_GRIND_METHOD_SHA256,
    _LONG_GRIND_STATEFUL_METHODS,
    _LONG_REGULAR_ADJUSTMENT_PROGRAM,
    _MANAGED_LONG_ADJUSTMENT_PROGRAM,
)


def _build_long_grind_route(
    constants: Any,
    methods: dict[str, ast.FunctionDef],
    method_records: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Describe the reviewed tag-120 branch without widening its proof.

    X7 keeps this route beside the system-v3.2 adjustment machinery and selects
    its stake, threshold, and precision behavior from ``is_futures_mode``.
    We publish the dual-mode scope only when both stateful methods and every
    spot and futures constant are present. Once the shape is recognizable, a
    changed method hash is a hard error: silently falling back to a handwritten
    state machine would turn a source update into an undetected parity bug.
    """
    return _build_legacy_grind_route(
        constants,
        methods,
        method_records,
        route_name="long-grind",
        mode_constant="long_grind_mode_name",
        tags_constant="long_grind_mode_tags",
        stateful_methods=_LONG_GRIND_STATEFUL_METHODS,
        method_sha256=_LONG_GRIND_METHOD_SHA256,
        adjustment_scope=_LONG_GRIND_ADJUSTMENT_SCOPE,
        grind_mode=True,
    )


def _build_long_btc_route(
    constants: Any,
    methods: dict[str, ast.FunctionDef],
    method_records: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Describe tag 121's regular prelude and legacy post-de-risk continuation."""
    route, identity = _build_legacy_grind_route(
        constants,
        methods,
        method_records,
        route_name="long-btc",
        mode_constant="long_btc_mode_name",
        tags_constant="long_btc_mode_tags",
        stateful_methods=_LONG_BTC_STATEFUL_METHODS,
        method_sha256=_LONG_BTC_METHOD_SHA256,
        adjustment_scope=_LONG_BTC_ADJUSTMENT_SCOPE,
        grind_mode=False,
        regular_mode=True,
    )
    if route is not None:
        route["regular_decision_program"] = _LONG_REGULAR_ADJUSTMENT_PROGRAM
        route["regular_constants"] = _build_regular_adjustment_constants(
            constants,
            methods["long_adjust_trade_position_no_derisk"],
        )
    return route, identity


def _build_legacy_grind_route(
    constants: Any,
    methods: dict[str, ast.FunctionDef],
    method_records: Any,
    *,
    route_name: str,
    mode_constant: str,
    tags_constant: str,
    stateful_methods: tuple[str, ...],
    method_sha256: dict[str, str],
    adjustment_scope: str,
    grind_mode: bool,
    regular_mode: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Validate one source-pinned route into X7's legacy grind state machine."""
    if not isinstance(constants, dict):
        return None, {}
    mode_name = constants.get(mode_constant)
    entry_tags = constants.get(tags_constant)
    route_declared = isinstance(mode_name, str) or isinstance(entry_tags, list)
    if not route_declared:
        return None, {}
    if not isinstance(mode_name, str) or not mode_name:
        raise StrategyAnalysisError(f"NFI {route_name} mode name must be frozen")
    if (
        not isinstance(entry_tags, list)
        or not entry_tags
        or not all(isinstance(tag, str) and tag for tag in entry_tags)
    ):
        raise StrategyAnalysisError(f"NFI {route_name} entry tags must be frozen strings")

    missing = [name for name in stateful_methods if name not in methods]
    if missing:
        raise StrategyAnalysisError(
            f"NFI {route_name} state machine is missing: " + ", ".join(missing)
        )
    records = (
        {
            record["name"]: record
            for record in method_records
            if isinstance(record, dict) and isinstance(record.get("name"), str)
        }
        if isinstance(method_records, list)
        else {}
    )
    changed = [
        name
        for name, expected in method_sha256.items()
        if records.get(name, {}).get("source_sha256") != expected
    ]
    if changed:
        raise StrategyAnalysisError(
            f"NFI X7 {route_name} route changed; exact lowering requires review: "
            + ", ".join(changed)
        )

    numeric_names = (
        "grind_mode_first_entry_profit_threshold_spot",
        "grind_mode_first_entry_stop_threshold_spot",
    )
    for name in numeric_names:
        value = constants.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise StrategyAnalysisError(f"NFI {route_name} constant {name} must be numeric")
    derisk = constants.get("derisk_use_grind_stops")
    if not isinstance(derisk, bool):
        raise StrategyAnalysisError(
            f"NFI {route_name} constant derisk_use_grind_stops must be boolean"
        )

    identity = {
        name: {
            "source_sha256": records[name]["source_sha256"],
            "location": records[name]["location"],
        }
        for name in stateful_methods
    }
    legacy_constants = _build_legacy_grind_constants(
        constants,
        regular_stake_multiplier=regular_mode,
    )
    route = {
        "mode_name": mode_name,
        "entry_tags": sorted(set(entry_tags)),
        # The 25% literal is protected by the route-specific exit-method hash.
        "exit_profit_threshold": 0.25,
        "adjustment_scope": adjustment_scope,
        "grind_mode": grind_mode,
        "decision_program": _MANAGED_LONG_ADJUSTMENT_PROGRAM,
        "first_entry_profit_threshold_spot": constants[
            "grind_mode_first_entry_profit_threshold_spot"
        ],
        "first_entry_stop_threshold_spot": constants["grind_mode_first_entry_stop_threshold_spot"],
        "futures_fallback_loss_threshold": _legacy_futures_fallback_loss_threshold(
            methods["long_grind_adjust_trade_position"]
        ),
        "derisk_use_grind_stops": derisk,
        "stateful_input_contract": {
            "indexed_fields": {"last_candle": [], "previous_candle": []}
        },
        "constants": legacy_constants,
    }
    route["program"] = compile_legacy_grind_ir(
        methods["long_grind_adjust_trade_position"],
        {
            **legacy_constants,
            "first_entry_profit_threshold_spot": route[
                "first_entry_profit_threshold_spot"
            ],
            "first_entry_stop_threshold_spot": route[
                "first_entry_stop_threshold_spot"
            ],
        },
    )
    buyback = next(
        transition
        for transition in route["program"]["source_order"]
        if transition["kind"] == "derisk-buyback"
    )
    route["stateful_input_contract"]["indexed_fields"]["last_candle"] = buyback[
        "entry_feature_columns"
    ]
    return route, identity


def _legacy_futures_fallback_loss_threshold(method: ast.FunctionDef) -> float:
    """Return the compiler-extracted leverage-scaled Futures loss threshold."""

    return float(extract_legacy_futures_fallback(method)["loss_threshold"])


def _build_legacy_grind_constants(
    constants: dict[str, Any],
    *,
    regular_stake_multiplier: bool = False,
) -> dict[str, Any]:
    """Freeze the repeated constants read by the legacy grind callback.

    The source names clusters separately. The IR discovers their numbered
    constant families and stores them without imposing an engine-side ceiling.
    """

    def number(name: str) -> int | float:
        value = constants.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise StrategyAnalysisError(f"NFI legacy grind constant {name} must be numeric")
        return value

    def number_list(name: str) -> list[int | float]:
        value = constants.get(name)
        if (
            not isinstance(value, list)
            or not value
            or any(isinstance(item, bool) or not isinstance(item, int | float) for item in value)
        ):
            raise StrategyAnalysisError(
                f"NFI legacy grind constant {name} must be a non-empty numeric list"
            )
        return value

    clusters: list[dict[str, Any]] = []
    discovered: list[tuple[int, int | None, str]] = []
    pattern = re.compile(r"^(grind_(\d+)(?:_derisk_(\d+))?)_stakes_futures$")
    for name in constants:
        match = pattern.fullmatch(name)
        if match is None:
            continue
        level = int(match.group(2))
        derisk_level = int(match.group(3)) if match.group(3) is not None else None
        discovered.append((level, derisk_level, match.group(1)))
    discovered.sort(key=lambda item: (item[1] is not None, item[0], item[1] or 0))
    if not discovered:
        raise StrategyAnalysisError("NFI legacy grind constants contain no cluster families")
    cluster_specs = []
    for level, derisk_level, prefix in discovered:
        if derisk_level is None:
            cluster_specs.append((f"gd{level}", f"dd{level}", prefix, False))
        elif derisk_level == 1:
            cluster_specs.append((f"dl{level}", f"ddl{level}", prefix, True))
        else:
            raise StrategyAnalysisError(
                "NFI legacy post-de-risk tag family changed; exact lowering requires review"
            )
    for entry_tag, stop_tag, prefix, post_derisk in cluster_specs:
        record: dict[str, Any] = {
            "entry_tag": entry_tag,
            "stop_tag": stop_tag,
            "post_derisk": post_derisk,
        }
        for mode in ("futures", "spot"):
            stakes = number_list(f"{prefix}_stakes_{mode}")
            thresholds = number_list(f"{prefix}_sub_thresholds_{mode}")
            if len(stakes) != len(thresholds):
                raise StrategyAnalysisError(
                    f"NFI legacy grind {prefix} stake/threshold lengths differ for {mode}"
                )
            record[f"stakes_{mode}"] = stakes
            record[f"thresholds_{mode}"] = thresholds
            record[f"stop_threshold_{mode}"] = number(f"{prefix}_stop_grinds_{mode}")
            record[f"profit_threshold_{mode}"] = number(f"{prefix}_profit_threshold_{mode}")
        clusters.append(record)

    return {
        "max_stake_multiplier": number("grinding_v1_max_stake"),
        "stake_multipliers_futures": number_list(
            "regular_mode_stake_multiplier_futures"
            if regular_stake_multiplier
            else "grind_mode_stake_multiplier_futures"
        ),
        "stake_multipliers_spot": number_list(
            "regular_mode_stake_multiplier_spot"
            if regular_stake_multiplier
            else "grind_mode_stake_multiplier_spot"
        ),
        "derisk_1_reentry_futures": number("regular_mode_derisk_1_reentry_futures"),
        "derisk_1_reentry_spot": number("regular_mode_derisk_1_reentry_spot"),
        "clusters": clusters,
    }


def _build_regular_adjustment_constants(
    constants: dict[str, Any],
    method: ast.FunctionDef,
) -> dict[str, Any]:
    """Freeze both market-mode branches before tag 121 reaches legacy grind.

    The source spells out numbered ``gN`` branches.
    The IR stores them in callback order. Rust may then share arithmetic while
    preserving every strict comparison and early return from the Python body.
    Futures and spot values remain separate source-derived fields; the runtime
    selects one complete mode and never substitutes values between them.
    """

    def number(name: str) -> int | float:
        value = constants.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise StrategyAnalysisError(f"NFI regular adjustment constant {name} must be numeric")
        return value

    def number_list(name: str) -> list[int | float]:
        value = constants.get(name)
        if (
            not isinstance(value, list)
            or not value
            or any(isinstance(item, bool) or not isinstance(item, int | float) for item in value)
        ):
            raise StrategyAnalysisError(
                f"NFI regular adjustment constant {name} must be a non-empty numeric list"
            )
        return value

    use_grind_stops = constants.get("regular_mode_use_grind_stops")
    derisk_enable = constants.get("derisk_enable")
    if not isinstance(use_grind_stops, bool) or not isinstance(derisk_enable, bool):
        raise StrategyAnalysisError("NFI regular adjustment switches must be boolean")

    rebuy: dict[str, tuple[list[int | float], list[int | float]]] = {}
    for mode in ("futures", "spot"):
        stakes = number_list(f"regular_mode_rebuy_stakes_{mode}")
        thresholds = number_list(f"regular_mode_rebuy_thresholds_{mode}")
        if len(stakes) != len(thresholds):
            raise StrategyAnalysisError(
                f"NFI regular adjustment rebuy stake/threshold lengths differ for {mode}"
            )
        rebuy[mode] = (stakes, thresholds)

    grinds: list[dict[str, Any]] = []
    levels = sorted(
        {
            int(match.group(1))
            for name in constants
            if (
                match := re.fullmatch(
                    r"regular_mode_grind_(\d+)_stakes_futures",
                    name,
                )
            )
            is not None
        }
    )
    if not levels:
        raise StrategyAnalysisError("NFI regular adjustment contains no grind levels")
    for level in levels:
        prefix = f"regular_mode_grind_{level}"
        grind: dict[str, Any] = {
            "entry_tag": f"g{level}",
            "stop_tag": f"sg{level}",
        }
        for mode in ("futures", "spot"):
            stakes = number_list(f"{prefix}_stakes_{mode}")
            thresholds = number_list(f"{prefix}_thresholds_{mode}")
            if len(stakes) != len(thresholds):
                raise StrategyAnalysisError(
                    f"NFI regular adjustment g{level} stake/threshold lengths differ for {mode}"
                )
            grind[f"stakes_{mode}"] = stakes
            grind[f"thresholds_{mode}"] = thresholds
            grind[f"stop_threshold_{mode}"] = number(f"{prefix}_stop_grinds_{mode}")
            grind[f"profit_threshold_{mode}"] = number(f"{prefix}_profit_threshold_{mode}")
        grinds.append(grind)

    return {
        "use_grind_stops": use_grind_stops,
        "derisk_enable": derisk_enable,
        "rebuy_stakes_futures": rebuy["futures"][0],
        "rebuy_thresholds_futures": rebuy["futures"][1],
        "rebuy_stakes_spot": rebuy["spot"][0],
        "rebuy_thresholds_spot": rebuy["spot"][1],
        "derisk_threshold_futures": number("regular_mode_derisk_futures"),
        "derisk_threshold_spot": number("regular_mode_derisk_spot"),
        "derisk_level_1_threshold_futures": number("regular_mode_derisk_1_futures"),
        "derisk_level_1_threshold_spot": number("regular_mode_derisk_1_spot"),
        "grinds": grinds,
        "policy": _regular_adjustment_literal_policy(method),
    }


def _regular_adjustment_literal_policy(method: ast.FunctionDef) -> dict[str, int | float]:
    """Extract callback literals that NFI does not expose as class constants.

    These values are part of the reviewed Python method, not engine tuning.
    Extracting them into the typed IR keeps Rust free of strategy-version
    literals and makes the effective policy visible in a certification bundle.
    The surrounding method hash still rejects a structural source change.
    """

    def numeric(node: ast.AST) -> float | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return float(node.value)
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, int | float)
        ):
            return -float(node.operand.value)
        return None

    named_gates: dict[str, float] = {}
    durations: dict[str, set[float]] = {"minutes": set(), "hours": set()}
    minimum_multipliers: set[float] = set()
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.startswith("slice_profit_lt_neg_")
            and isinstance(node.value, ast.Compare)
            and len(node.value.comparators) == 1
            and (value := numeric(node.value.comparators[0])) is not None
        ):
            named_gates[node.targets[0].id] = value
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "timedelta"
        ):
            for keyword in node.keywords:
                if (
                    keyword.arg in durations
                    and (value := numeric(keyword.value)) is not None
                    and value > 0.0
                ):
                    durations[keyword.arg].add(value)
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mult)
            and isinstance(node.left, ast.Name)
            and node.left.id == "min_stake"
            and (value := numeric(node.right)) is not None
            and value > 1.0
        ):
            minimum_multipliers.add(value)

    gate_names = (
        "slice_profit_lt_neg_0_02",
        "slice_profit_lt_neg_0_03",
        "slice_profit_lt_neg_0_06",
    )
    hours = sorted(durations["hours"])
    minutes = sorted(durations["minutes"])
    multipliers = sorted(minimum_multipliers)
    if (
        any(name not in named_gates for name in gate_names)
        or len(minutes) != 1
        or len(hours) != 3
        or len(multipliers) != 2
    ):
        raise StrategyAnalysisError(
            "NFI regular adjustment literal policy changed; exact lowering requires review"
        )
    return {
        "entry_retry_ms": int(minutes[0] * 60_000),
        "grind_force_order_age_ms": int(hours[0] * 3_600_000),
        "grind_order_age_ms": int(hours[1] * 3_600_000),
        "rebuy_order_age_ms": int(hours[2] * 3_600_000),
        "grind_entry_profit_gate": named_gates["slice_profit_lt_neg_0_02"],
        "additional_grind_profit_gate": named_gates["slice_profit_lt_neg_0_03"],
        "forced_age_profit_gate": named_gates["slice_profit_lt_neg_0_06"],
        "minimum_entry_multiplier": multipliers[0],
        "minimum_remaining_multiplier": multipliers[1],
    }
