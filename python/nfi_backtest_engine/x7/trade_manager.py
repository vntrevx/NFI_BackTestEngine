"""Source-bound trade-manager IR for the constrained NFI X7 adapter.

The generic scalar compiler can already lower NFI's large, pure exit
predicates.  The public ``custom_exit`` callback remains stateful because it
routes by entry tag and maintains a per-pair profit target.  This module joins
the proven pure programs with explicit descriptions of the reviewed stateful
routes. The adapter inspects every executable vector signal and fails before
simulation when a tag or side falls outside that scope.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import StrategyAnalysisError
from ..trade_ir import build_trade_dependency_ir

NFI_TRADE_MANAGER_IR_VERSION = "0.27.0"

_MANAGED_LONG_PROGRAM_ORDER = (
    "long_exit_signals",
    "long_exit_main",
    "long_exit_williams_r",
    "long_exit_dec",
)
_MANAGED_SHORT_PROGRAM_ORDER = (
    "short_exit_signals",
    "short_exit_main",
    "short_exit_williams_r",
    "short_exit_dec",
)
_MANAGED_LONG_ADJUSTMENT_PROGRAM = "long_grind_entry_v3"
_MANAGED_SHORT_ADJUSTMENT_PROGRAM = "short_grind_entry_v3"
_LONG_REGULAR_ADJUSTMENT_PROGRAM = "long_grind_entry"
_MANAGED_LONG_STATEFUL_STEPS = (
    "long_exit_stoploss",
    "exit_profit_target",
    "mark_profit_target",
    "_set_profit_target",
    "_remove_profit_target",
)
_MANAGED_LONG_FROZEN_CONSTANTS = (
    "derisk_enable",
    "stops_enable",
    "stop_threshold_futures",
    "stop_threshold_spot",
    "system_name_use",
    "system_v3_2_name",
    "system_v3_2_stop_threshold_doom_futures",
    "system_v3_2_stop_threshold_doom_spot",
    "system_v3_2_stops_enable",
    "u_e_stops_enable",
)
_MANAGED_LONG_STATEFUL_FEATURES = {
    "last_candle": [
        "CMF_20",
        "CMF_20_1h",
        "CMF_20_4h",
        "EMA_200",
        "ROC_9_4h",
        "RSI_14",
        "RSI_14_1h",
        "close",
    ],
    "previous_candle_1": ["RSI_14"],
}


@dataclass(frozen=True)
class _ManagedLongRouteSpec:
    """One reviewed branch in X7's ordered long-side ``custom_exit`` router.

    ``profile`` selects a fixed Rust policy; it is not an open-ended strategy
    option. ``program_order`` records which source-compiled pure decisions run
    before the handwritten state machine. Keeping this table declarative makes
    the differences between modes visible without duplicating seven callbacks.
    """

    key: str
    profile: str
    mode_constant: str
    tags_constant: str
    method: str
    program_order: tuple[str, ...]


def _adjustment_program_order(constants: dict[str, Any]) -> list[str]:
    return [
        *(f"derisk_level_{record['level']}" for record in constants["derisk_levels"]),
        *(
            f"grind_{record['level']}_{action}"
            for record in constants["grinds"]
            for action in ("entry", "exit", "derisk")
        ),
    ]


_MANAGED_LONG_ROUTE_SPECS = (
    _ManagedLongRouteSpec(
        "long_normal",
        "normal",
        "long_normal_mode_name",
        "long_normal_mode_tags",
        "long_exit_normal",
        _MANAGED_LONG_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "long_pump",
        "pump",
        "long_pump_mode_name",
        "long_pump_mode_tags",
        "long_exit_pump",
        _MANAGED_LONG_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "long_quick",
        "quick",
        "long_quick_mode_name",
        "long_quick_mode_tags",
        "long_exit_quick",
        _MANAGED_LONG_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "long_rebuy",
        "rebuy",
        "long_rebuy_mode_name",
        "long_rebuy_mode_tags",
        "long_exit_rebuy",
        _MANAGED_LONG_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "long_high_profit",
        "high-profit",
        "long_high_profit_mode_name",
        "long_high_profit_mode_tags",
        "long_exit_high_profit",
        _MANAGED_LONG_PROGRAM_ORDER[:3],
    ),
    _ManagedLongRouteSpec(
        "long_rapid",
        "rapid",
        "long_rapid_mode_name",
        "long_rapid_mode_tags",
        "long_exit_rapid",
        _MANAGED_LONG_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "long_top_coins",
        "top-coins",
        "long_top_coins_mode_name",
        "long_top_coins_mode_tags",
        "long_exit_top_coins",
        _MANAGED_LONG_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "long_scalp",
        "scalp",
        "long_scalp_mode_name",
        "long_scalp_mode_tags",
        "long_exit_scalp",
        _MANAGED_LONG_PROGRAM_ORDER,
    ),
)

# Short top-coins tags intentionally use the normal fallback at the end of
# ``custom_exit``: upstream excludes those tags from
# ``short_exit_known_mode_tags`` and has no explicit top-coins dispatch block.
# Describing that fallback as a route keeps the source behavior visible and
# avoids pretending the otherwise-unused ``short_exit_top_coins`` method ran.
_MANAGED_SHORT_ROUTE_SPECS = (
    _ManagedLongRouteSpec(
        "short_normal",
        "normal",
        "short_normal_mode_name",
        "short_normal_mode_tags",
        "short_exit_normal",
        _MANAGED_SHORT_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "short_pump",
        "pump",
        "short_pump_mode_name",
        "short_pump_mode_tags",
        "short_exit_pump",
        _MANAGED_SHORT_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "short_quick",
        "quick",
        "short_quick_mode_name",
        "short_quick_mode_tags",
        "short_exit_quick",
        _MANAGED_SHORT_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "short_rebuy",
        "rebuy",
        "short_rebuy_mode_name",
        "short_rebuy_mode_tags",
        "short_exit_rebuy",
        _MANAGED_SHORT_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "short_high_profit",
        "high-profit",
        "short_high_profit_mode_name",
        "short_high_profit_mode_tags",
        "short_exit_high_profit",
        _MANAGED_SHORT_PROGRAM_ORDER[:3],
    ),
    _ManagedLongRouteSpec(
        "short_rapid",
        "rapid",
        "short_rapid_mode_name",
        "short_rapid_mode_tags",
        "short_exit_rapid",
        _MANAGED_SHORT_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "short_scalp",
        "scalp",
        "short_scalp_mode_name",
        "short_scalp_mode_tags",
        "short_exit_scalp",
        _MANAGED_SHORT_PROGRAM_ORDER,
    ),
    _ManagedLongRouteSpec(
        "short_top_coins_fallback",
        "normal",
        "short_normal_mode_name",
        "short_top_coins_mode_tags",
        "short_exit_normal",
        _MANAGED_SHORT_PROGRAM_ORDER,
    ),
)

# The long-side order is compiled from ``custom_exit``. Rebuy remains a
# separate adjustment payload even though its exit policy belongs in that
# source-ordered router.
_MANAGED_SHORT_ROUTE_ORDER = tuple(spec.key for spec in _MANAGED_SHORT_ROUTE_SPECS)

# These residual helpers are not yet wholly represented by managed-exit IR.
# Keep their identities fail-closed until their semantics move into generic
# opcodes. Route wrappers and custom_exit are deliberately absent: their
# executable behavior is now selected structurally by the source compiler.
_MANAGED_LONG_METHOD_SHA256 = {
    "long_exit_stoploss": ("d7eb62382e5caff15dc9e12531cbcda0968b48b0e4db8d410a32ef9c19b197e7"),
    "exit_profit_target": ("6125c745a6f30ea67b68e17c49f8cd937eb3607c8fd4d719618ffe140793d67c"),
    "mark_profit_target": ("d1e956d0d1cb9ab3540aa4fd5288ff8c78d873f50241a9cc502b3279c59b994f"),
    "_set_profit_target": ("76aafad6b88f7843cc701ddabcbef129e5c5a4d90a1def70e30600456a16f86f"),
    "_remove_profit_target": ("4fe333ab59e962f743375ddba0b6289233b8b40adc71ac8404d0b944ea1f3210"),
}
_MANAGED_SHORT_METHOD_SHA256 = {
    "short_exit_stoploss": ("172808fcb8ebf05ed0c0689fc46672e78b76084cb5420f014fef4d169076e113"),
}

_QUICK_RAPID_STATEFUL_FEATURES = {
    "last_candle": ["MFI_14", "RSI_3", "RSI_3_15m", "WILLR_14"],
    "previous_candle_1": [],
}

_ROUTE_STOP_CONSTANTS = {
    "rebuy": (
        "system_v3_2_stop_threshold_futures_rebuy",
        "system_v3_2_stop_threshold_spot_rebuy",
    ),
    "rapid": (
        "system_v3_2_stop_threshold_rapid_futures",
        "system_v3_2_stop_threshold_rapid_spot",
    ),
    "scalp": (
        "system_v3_2_stop_threshold_scalp_futures",
        "system_v3_2_stop_threshold_scalp_spot",
    ),
}

_REBUY_ADJUSTMENT_LIST_CONSTANTS = (
    "system_v3_rebuy_mode_stakes_futures",
    "system_v3_rebuy_mode_stakes_spot",
    "system_v3_rebuy_mode_thresholds_futures",
    "system_v3_rebuy_mode_thresholds_spot",
)
_REBUY_ADJUSTMENT_NUMBER_CONSTANTS = (
    "system_v3_rebuy_mode_derisk_futures",
    "system_v3_rebuy_mode_derisk_spot",
)
# X7 routes tag 120 through the independent grinding state machine.
# Both of its backtest market-mode branches are lowered from the same reviewed
# callback and source constants as one order-history state machine:
# first-entry recovery, two post-de-risk clusters, six grind clusters, their
# profit exits/stops, and the level-1 de-risk re-entry. Live partial-fill retry
# remains outside the simulator because a Freqtrade backtest exposes filled
# orders with ``safe_remaining == 0`` and cannot execute that branch.
_LONG_GRIND_ADJUSTMENT_SCOPE = "grind-backtest-v2"
_LONG_BTC_ADJUSTMENT_SCOPE = "regular-backtest-v2"
_LONG_GRIND_STATEFUL_METHODS = (
    "long_exit_grind",
    "long_grind_adjust_trade_position",
)
_LONG_GRIND_METHOD_SHA256 = {
    "long_exit_grind": ("1256bbece5361bf924b7fc78e8ee5073d48c3d4441908fd2f5e691a5aacaddb1"),
    "long_grind_adjust_trade_position": (
        "f989ea57b2fe8c654d78a58bc45c0bd76a57aa41f4703440db98bc727e408cc9"
    ),
}
_LONG_BTC_STATEFUL_METHODS = (
    "long_exit_btc",
    "long_grind_adjust_trade_position",
    "long_adjust_trade_position_no_derisk",
)
_LONG_BTC_METHOD_SHA256 = {
    "long_exit_btc": "bcd170a5a79176914aafd2f026d7483b8c9607367953a8d947093aba92a606af",
    "long_grind_adjust_trade_position": (
        "f989ea57b2fe8c654d78a58bc45c0bd76a57aa41f4703440db98bc727e408cc9"
    ),
    "long_adjust_trade_position_no_derisk": (
        "bada72d3886558cab169526a4a7033fe7dab033dc578d3a1af266f012b0026e1"
    ),
}
_LONG_GRIND_IMPLEMENTED_STEPS = (
    "legacy first-entry recovery",
    "legacy order-history reconstruction",
    "legacy post-de-risk grind levels 1-2",
    "legacy grind levels 1-6",
    "legacy futures drawdown entry fallback",
    "legacy grind profit exits and stops",
    "legacy de-risk level-1 re-entry",
)
_LONG_BTC_IMPLEMENTED_STEPS = (
    "tag-121 regular-mode order-history reconstruction",
    "tag-121 regular-mode rebuy",
    "tag-121 regular-mode grind levels 1-6",
    "tag-121 regular-mode grind profit exits and stops",
    "tag-121 regular-mode de-risk levels",
    "tag-121 post-de-risk legacy grind continuation",
)
_LONG_GRIND_REMAINING_STEPS = (
    "live partial-fill retry",
    "legacy futures adjustment",
)

# These methods contain the stateful part of the handwritten Rust lowering.
# A whole-file source hash alone proves identity, but it would also let a new
# X7 version silently enter an old state machine after the descriptor rebuilt.
# Pinning the normalized method hashes makes a strategy change fail closed and
# forces a deliberate review of order classification, branch order, and stake
# arithmetic.
_ADJUSTMENT_METHOD_SHA256: dict[str, frozenset[str]] = {
    "adjust_trade_position": frozenset(
        {"64d19512c5968f3cc4e329a8a7b33eb93dc8ce9debbf39c4d8c70c09529dfd1a"}
    ),
    "calc_total_profit": frozenset(
        {"ba0fc031f36140bbb3b5ae5feffa70ea7a5943e0315ff630407f2f92cdd9f70b"}
    ),
    # ``long_grind_entry_v3`` is intentionally absent. Its boolean behavior is
    # compiled from the supplied source, and its only write is proven
    # observability-only by trade_ir before this stateful router can use it.
    "profit_or_order_snapshot": frozenset(
        {"d3460303e0dd66274f8e02782818bac8b910220c1947178f8d20836dd0217add"}
    ),
    "scale_stakes_for_min_stake": frozenset(
        {"9c08fcc82d086ee776962060bb55719db939a89137e106d458ebf030c666316c"}
    ),
}

_ADJUSTMENT_BOOL_CONSTANTS = (
    "derisk_enable",
    "position_adjustment_enable",
    "system_v3_buyback_1_enable",
)
_ADJUSTMENT_NUMBER_CONSTANTS = (
    "system_v3_max_stake",
    # Rebuy entries deliberately start below the normal slot size. After the
    # level-3 de-risk, X7 transfers those trades into the shared grind-v3
    # state machine and restores the normal slice by dividing by this source
    # constant. Omitting it changes every subsequent grind order.
    "system_v3_rebuy_mode_stake_multiplier",
)
_ADJUSTMENT_GRIND_FIELDS = (
    "derisk_futures",
    "derisk_spot",
    "profit_threshold_futures",
    "profit_threshold_spot",
    "stakes_futures",
    "stakes_spot",
    "thresholds_futures",
    "thresholds_spot",
)


def build_nfi_trade_manager_ir(
    analysis: dict[str, Any],
    trade_dependency_ir: dict[str, Any],
) -> dict[str, Any] | None:
    """Build a scope-limited executable X7 route when the source proves it.

    ``None`` means that the selected strategy is not the X7 shape understood
    by this adapter.  Once the strategy name matches X7, malformed identity or
    a changed top-coins route is an error rather than a best-effort match.  This
    keeps a future NFI refactor from silently inheriting stale semantics.
    """
    from .adjustment_ir import compile_system_adjustment_ir
    from .adjustments import (
        _build_adjustment_constants,
        _build_rebuy_adjustment_constants,
        _validate_adjustment_method_identity,
    )
    from .legacy import _build_long_btc_route, _build_long_grind_route
    from .managed_exit_ir import compile_managed_exit_ir
    from .managed_short_exit_ir import compile_managed_short_exit_ir
    from .rebuy_ir import compile_rebuy_transition_ir
    from .routes import (
        _build_managed_long_routes,
        _build_managed_short_routes,
        _extract_rebuy_terminal_exit,
        _require_managed_long_methods,
        _top_coins_program_order,
        _validate_managed_long_method_identity,
        _validate_managed_short_method_identity,
    )

    strategies = analysis.get("strategies")
    source = analysis.get("source")
    if not isinstance(strategies, list) or len(strategies) != 1:
        raise StrategyAnalysisError("NFI trade manager requires one selected strategy")
    strategy = strategies[0]
    if not isinstance(strategy, dict):
        raise StrategyAnalysisError("NFI trade manager strategy record is invalid")
    strategy_name = strategy.get("name")
    if not isinstance(strategy_name, str):
        raise StrategyAnalysisError("NFI trade manager strategy name is invalid")
    if not strategy_name.startswith("NostalgiaForInfinityX7"):
        return None
    if not isinstance(source, dict):
        raise StrategyAnalysisError("NFI trade manager requires hash-bound source")
    source_path = source.get("path")
    source_sha256 = source.get("sha256")
    if not isinstance(source_path, str) or not isinstance(source_sha256, str):
        raise StrategyAnalysisError("NFI trade manager source identity is invalid")

    path = Path(source_path).resolve()
    try:
        # Hash the sealed file bytes before decoding.  Text-mode reads perform
        # universal-newline conversion and would reject valid CRLF strategies
        # on Windows even though the file had not changed after analysis.
        source_bytes = path.read_bytes()
        text = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StrategyAnalysisError(f"NFI trade manager source cannot be read: {path}") from exc
    if hashlib.sha256(source_bytes).hexdigest() != source_sha256:
        raise StrategyAnalysisError("NFI trade manager source hash differs from analysis")
    tree = ast.parse(text, filename=str(path), type_comments=True)
    class_node = next(
        (
            item
            for item in tree.body
            if isinstance(item, ast.ClassDef) and item.name == strategy_name
        ),
        None,
    )
    if class_node is None:
        raise StrategyAnalysisError("NFI trade manager strategy class disappeared")
    methods = {item.name: item for item in class_node.body if isinstance(item, ast.FunctionDef)}
    method_records = {
        method["name"]: method
        for method in strategy.get("methods", [])
        if isinstance(method, dict) and isinstance(method.get("name"), str)
    }
    constants = strategy.get("constants")
    if not isinstance(constants, dict):
        raise StrategyAnalysisError("NFI trade manager constants are invalid")
    _require_managed_long_methods(methods)
    rebuy_terminal_exit, _ = _extract_rebuy_terminal_exit(methods["long_exit_rebuy"])
    managed_exit_compilation = compile_managed_exit_ir(
        methods,
        constants,
        _MANAGED_LONG_ROUTE_SPECS,
        legacy_route_methods={
            "long_exit_grind": "long_grind",
            "long_exit_btc": "long_btc",
        },
        terminal_exits=(
            {"long_rebuy": rebuy_terminal_exit}
            if rebuy_terminal_exit is not None
            else None
        ),
    )
    managed_short_exit_compilation = compile_managed_short_exit_ir(
        methods,
        constants,
        _MANAGED_SHORT_ROUTE_SPECS,
    )
    _validate_managed_long_method_identity(methods, method_records)
    _validate_managed_short_method_identity(methods, method_records)

    # The top-coins route uses a literal tuple, so keep its pure call order as
    # an explicit structural invariant alongside the generic compiler.
    top_coins_router = methods["long_exit_top_coins"]
    actual_order = _top_coins_program_order(top_coins_router)
    if actual_order != _MANAGED_LONG_PROGRAM_ORDER:
        raise StrategyAnalysisError(
            "NFI X7 top-coins pure exit order changed; exact lowering must be reviewed"
        )

    long_grind_route, long_grind_method_identity = _build_long_grind_route(
        strategy.get("constants"),
        methods,
        strategy.get("methods"),
    )
    long_btc_route, long_btc_method_identity = _build_long_btc_route(
        strategy.get("constants"),
        methods,
        strategy.get("methods"),
    )

    managed_routes = _build_managed_long_routes(constants)
    if rebuy_terminal_exit is not None:
        managed_routes["long_rebuy"]["terminal_exit"] = rebuy_terminal_exit
    managed_short_routes = _build_managed_short_routes(constants)
    managed_entry_tags = sorted(
        {tag for route in managed_routes.values() for tag in route["entry_tags"]}
    )
    managed_short_adjustment_tags = sorted(
        {
            tag
            for key, route in managed_short_routes.items()
            if key != "short_rebuy"
            for tag in route["entry_tags"]
        }
    )
    frozen_constants = {name: constants.get(name) for name in _MANAGED_LONG_FROZEN_CONSTANTS}
    if not all(
        isinstance(frozen_constants[name], bool)
        for name in (
            "derisk_enable",
            "stops_enable",
            "system_v3_2_stops_enable",
            "u_e_stops_enable",
        )
    ):
        raise StrategyAnalysisError("NFI top-coins boolean constants are invalid")
    if not all(
        isinstance(frozen_constants[name], int | float)
        and not isinstance(frozen_constants[name], bool)
        for name in (
            "stop_threshold_futures",
            "stop_threshold_spot",
            "system_v3_2_stop_threshold_doom_futures",
            "system_v3_2_stop_threshold_doom_spot",
        )
    ):
        raise StrategyAnalysisError("NFI top-coins numeric stop constants are invalid")
    if (
        not isinstance(frozen_constants["system_name_use"], str)
        or frozen_constants["system_name_use"] != frozen_constants["system_v3_2_name"]
    ):
        raise StrategyAnalysisError(
            "NFI top-coins lowering currently requires frozen system_v3_2 routing"
        )

    has_position_adjustment = (
        "adjust_trade_position" in method_records
        and constants.get("position_adjustment_enable") is True
    )
    adjustment_constants: dict[str, Any] | None = None
    adjustment_program: dict[str, Any] | None = None
    short_adjustment_constants: dict[str, Any] | None = None
    short_adjustment_program: dict[str, Any] | None = None
    rebuy_adjustment_constants: dict[str, Any] | None = None
    rebuy_transition_program: dict[str, Any] | None = None
    short_rebuy_transition_program: dict[str, Any] | None = None
    if has_position_adjustment:
        _validate_adjustment_method_identity(method_records)
        adjustment_constants = _build_adjustment_constants(
            constants,
            methods["long_grind_adjust_trade_position_v3"],
            side="long",
        )
        short_adjustment_constants = _build_adjustment_constants(
            constants,
            methods["short_grind_adjust_trade_position_v3"],
            side="short",
        )
        rebuy_adjustment_constants = _build_rebuy_adjustment_constants(constants)
        long_policy = adjustment_constants.get("policy")
        short_policy = short_adjustment_constants.get("policy")
        if not isinstance(long_policy, dict) or not isinstance(short_policy, dict):
            raise StrategyAnalysisError("NFI rebuy delegate policy is unavailable")
        adjustment_program = compile_system_adjustment_ir(
            methods["long_grind_adjust_trade_position_v3"],
            methods["long_grind_exit_v3"],
            constants,
            side="long",
            retry_policy=long_policy,
        )
        short_adjustment_program = compile_system_adjustment_ir(
            methods["short_grind_adjust_trade_position_v3"],
            methods["short_grind_exit_v3"],
            constants,
            side="short",
            retry_policy=short_policy,
        )
        rebuy_transition_program = compile_rebuy_transition_ir(
            methods["long_rebuy_adjust_trade_position_v3"],
            constants,
            delegate_retry_ms=int(long_policy["entry_retry_ms"]),
        )
        short_rebuy_transition_program = compile_rebuy_transition_ir(
            methods["short_rebuy_adjust_trade_position_v3"],
            constants,
            delegate_retry_ms=int(short_policy["entry_retry_ms"]),
        )

    # The stateful router calls its decisions through a tuple variable
    # (``exit_func``), so ordinary call-graph discovery cannot infer those
    # targets. Compile the structurally proven literal tuple as explicit roots.
    basic_decision_roots = tuple(
        dict.fromkeys(
            program
            for route in managed_exit_compilation.program["routes"]
            for program in route["decision_program_order"]
        )
    )
    short_decision_roots = tuple(
        dict.fromkeys(
            program
            for route in managed_short_exit_compilation.program["routes"]
            for program in route["decision_program_order"]
        )
    )
    decision_roots = (
        *basic_decision_roots,
        *short_decision_roots,
        *((_MANAGED_LONG_ADJUSTMENT_PROGRAM,) if has_position_adjustment else ()),
        *((_MANAGED_SHORT_ADJUSTMENT_PROGRAM,) if has_position_adjustment else ()),
        *((_LONG_REGULAR_ADJUSTMENT_PROGRAM,) if long_btc_route is not None else ()),
    )
    decision_report = build_trade_dependency_ir(analysis, roots=decision_roots)
    compiled = decision_report.get("compiled_scalar_methods")
    if not isinstance(compiled, dict):
        raise StrategyAnalysisError("NFI trade dependency programs are invalid")
    programs: dict[str, Any] = {}
    program_proof: dict[str, Any] = {}
    for name in decision_roots:
        record = compiled.get(name)
        if not isinstance(record, dict) or not isinstance(record.get("program"), dict):
            raise StrategyAnalysisError(f"NFI top-coins decision {name} is not scalar-pure")
        programs[name] = record["program"]
        program_proof[name] = {
            "line": record["line"],
            "end_line": record["end_line"],
            "node_count": record["node_count"],
            "input_contract": record["input_contract"],
        }

    managed_exit_proof_methods = dict.fromkeys(
        [
            "custom_exit",
            *(spec.method for spec in _MANAGED_LONG_ROUTE_SPECS),
            *(spec.method for spec in _MANAGED_SHORT_ROUTE_SPECS),
            *_MANAGED_LONG_METHOD_SHA256,
            *_MANAGED_SHORT_METHOD_SHA256,
        ]
    )
    method_identity = {
        name: {
            "source_sha256": method_records[name]["source_sha256"],
            "location": method_records[name]["location"],
        }
        for name in managed_exit_proof_methods
    }
    method_identity.update(long_grind_method_identity)
    method_identity.update(long_btc_method_identity)
    supported_routes: dict[str, Any] = dict(managed_routes)
    if long_grind_route is not None:
        supported_routes["long_grind"] = long_grind_route
    if long_btc_route is not None:
        supported_routes["long_btc"] = long_btc_route
    route_order = [
        name
        for name in managed_exit_compilation.long_route_order
        if name in supported_routes
    ]
    if set(route_order) != set(supported_routes):
        raise StrategyAnalysisError("NFI custom_exit long route inventory is incomplete")
    operation = {
        "opcode": "nfi-x7-trade-manager-v1",
        "schema_version": NFI_TRADE_MANAGER_IR_VERSION,
        "source_sha256": source_sha256,
        "supported_routes": supported_routes,
        "route_order": route_order,
        "managed_exit_program": managed_exit_compilation.program,
        "managed_short_exit_program": managed_short_exit_compilation.program,
        "supported_short_routes": managed_short_routes,
        "short_route_order": list(managed_short_exit_compilation.short_route_order),
        "constants": frozen_constants,
        "programs": {name: programs[name] for name in decision_roots},
    }
    if adjustment_constants is not None and adjustment_program is not None:
        operation["position_adjustment"] = {
            "enabled": constants["position_adjustment_enable"],
            # These are exactly X7's ``long_adjust_mode_tags`` for the
            # supported source snapshot. Rebuy/grind/BTC tags use different
            # adjustment callbacks and are deliberately excluded.
            "entry_tags": managed_entry_tags,
            "system_version": frozen_constants["system_v3_2_name"],
            "source_callback": methods["long_grind_adjust_trade_position_v3"].name,
            "decision_program": _MANAGED_LONG_ADJUSTMENT_PROGRAM,
            "program_order": _adjustment_program_order(adjustment_constants),
            "stateful_input_contract": adjustment_program["input_contract"],
            "constants": adjustment_constants,
            "program": adjustment_program,
        }
    if short_adjustment_constants is not None and short_adjustment_program is not None:
        operation["short_position_adjustment"] = {
            "enabled": constants["position_adjustment_enable"],
            # Exact upstream ``short_adjust_mode_tags``. Rebuy uses its
            # dedicated ladder first; tag 620 remains fail-closed because it
            # routes into the independent legacy short-grind callback.
            "entry_tags": managed_short_adjustment_tags,
            "system_version": frozen_constants["system_v3_2_name"],
            "source_callback": methods["short_grind_adjust_trade_position_v3"].name,
            "decision_program": _MANAGED_SHORT_ADJUSTMENT_PROGRAM,
            "program_order": _adjustment_program_order(short_adjustment_constants),
            "stateful_input_contract": short_adjustment_program["input_contract"],
            "constants": short_adjustment_constants,
            "program": short_adjustment_program,
        }
    if (
        rebuy_adjustment_constants is not None
        and rebuy_transition_program is not None
        and short_rebuy_transition_program is not None
    ):
        rebuy_route = managed_routes["long_rebuy"]
        operation["rebuy_adjustment"] = {
            "enabled": constants["position_adjustment_enable"],
            "entry_tags": rebuy_route["entry_tags"],
            "system_version": frozen_constants["system_v3_2_name"],
            "stateful_input_contract": rebuy_transition_program["input_contract"],
            "constants": rebuy_adjustment_constants,
            "program": rebuy_transition_program,
        }
        short_rebuy_route = managed_short_routes["short_rebuy"]
        operation["short_rebuy_adjustment"] = {
            "enabled": constants["position_adjustment_enable"],
            "entry_tags": short_rebuy_route["entry_tags"],
            "system_version": frozen_constants["system_v3_2_name"],
            "execution_scope": "rebuy-and-grind-v2",
            # After the first level-3 de-risk, X7 delegates to the same
            # source-bound short grind-v3 descriptor used by ordinary shorts.
            "post_derisk_action": "short-position-adjustment",
            "stateful_input_contract": short_rebuy_transition_program["input_contract"],
            "constants": rebuy_adjustment_constants,
            "program": short_rebuy_transition_program,
        }
    encoded = json.dumps(
        operation,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "schema_version": NFI_TRADE_MANAGER_IR_VERSION,
        "backend": "rust-nfi-x7-trade-manager",
        "executable_in_rust": True,
        "execution_scope": {
            "sides": ["long", "short"],
            "entry_tag_match": "any",
            "unsupported_action": "fail-before-simulation",
        },
        "operation": operation,
        "proof": {
            "matcher": "nfi-x7-managed-long-short-router-v2",
            "source_sha256": source_sha256,
            "trade_ir_fingerprint": trade_dependency_ir["fingerprint"],
            "decision_ir_fingerprint": decision_report["fingerprint"],
            "managed_exit_ir_fingerprint": managed_exit_compilation.program["fingerprint"],
            "managed_short_exit_ir_fingerprint": managed_short_exit_compilation.program[
                "fingerprint"
            ],
            "rebuy_transition_ir_fingerprint": (
                rebuy_transition_program["fingerprint"]
                if rebuy_transition_program is not None
                else None
            ),
            "short_rebuy_transition_ir_fingerprint": (
                short_rebuy_transition_program["fingerprint"]
                if short_rebuy_transition_program is not None
                else None
            ),
            "system_adjustment_ir_fingerprint": (
                adjustment_program["fingerprint"] if adjustment_program is not None else None
            ),
            "short_system_adjustment_ir_fingerprint": (
                short_adjustment_program["fingerprint"]
                if short_adjustment_program is not None
                else None
            ),
            "legacy_grind_ir_fingerprint": (
                long_grind_route["program"]["fingerprint"]
                if long_grind_route is not None
                else None
            ),
            "operation_sha256": hashlib.sha256(encoded).hexdigest(),
            "programs": program_proof,
            "stateful_methods": method_identity,
        },
        "implemented_steps": [
            "ordered managed-long route dispatch",
            *_MANAGED_LONG_STATEFUL_STEPS,
            "ordered managed-short route dispatch",
            "managed-short stop and target state",
            "short system-v3.2 grind position adjustment",
            "short-rebuy ladder and post-derisk grind transfer",
            *(_LONG_GRIND_IMPLEMENTED_STEPS if long_grind_route is not None else ()),
            *(_LONG_BTC_IMPLEMENTED_STEPS if long_btc_route is not None else ()),
        ],
        "remaining_steps": (
            [*_LONG_GRIND_REMAINING_STEPS, "legacy short-grind tag 620"]
            if long_grind_route is not None or long_btc_route is not None
            else ["legacy short-grind tag 620"]
        ),
    }
