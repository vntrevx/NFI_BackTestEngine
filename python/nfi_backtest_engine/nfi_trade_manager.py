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

from .errors import StrategyAnalysisError
from .trade_ir import build_trade_dependency_ir

NFI_TRADE_MANAGER_IR_VERSION = "0.8.0"

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

# This order is copied from ``custom_exit`` after removing unsupported short
# routes. Rebuy remains a separate adjustment payload even though its exit
# policy belongs in the same ordered long-side router.
_MANAGED_LONG_ROUTE_ORDER = (
    "long_normal",
    "long_pump",
    "long_quick",
    "long_rebuy",
    "long_high_profit",
    "long_rapid",
    "long_grind",
    "long_btc",
    "long_top_coins",
    "long_scalp",
)

# Stateful callback bodies are handwritten in Rust and therefore require a
# stronger boundary than the whole strategy SHA. A same-file update can change
# one branch while leaving all scalar programs compilable; these method hashes
# make that update fail closed until the ordered policy is reviewed again.
_MANAGED_LONG_METHOD_SHA256 = {
    "custom_exit": "a0bb3c1d5bf6ab5dedfa96928e3dd52c714a53b489ff59d0183038a9207de497",
    "long_exit_normal": "c6e0aea5dc4009a736315bf7944fef537d2cbffa1ffc29f9d903e86c4c0a7bd3",
    "long_exit_pump": "fb87913b8abdc1d711ea1d7a0a70543a382d93263e3f7c1932e342b04ad5e8ea",
    "long_exit_quick": "ba1996d0493c711e1bd591c0840c1674ba6695b0f45648f45531163593966374",
    "long_exit_rebuy": "03e0f0ad6cdaac21bdb211a393508d517c0ff1034606eb0acdcd863efc5ffe60",
    "long_exit_high_profit": ("b0fc7d0c36f7fa18b74392b686097e9f8ffe2f06429173ad82d321459f660d2b"),
    "long_exit_rapid": "95f96395151ba41c5cf17afacafc44e40dade5828211353fc51ce92fcbd61b53",
    "long_exit_top_coins": ("0fdf487ced648d2ccc8e790b98b85becebf29e1d4314687f552d365559e42153"),
    "long_exit_scalp": "b2a6cf02e277f63147e99e912f9e545232dc6705f89ee68dc240bd141bea4ab7",
    "long_exit_stoploss": ("d7eb62382e5caff15dc9e12531cbcda0968b48b0e4db8d410a32ef9c19b197e7"),
    "exit_profit_target": ("6125c745a6f30ea67b68e17c49f8cd937eb3607c8fd4d719618ffe140793d67c"),
    "mark_profit_target": ("d1e956d0d1cb9ab3540aa4fd5288ff8c78d873f50241a9cc502b3279c59b994f"),
    "_set_profit_target": ("76aafad6b88f7843cc701ddabcbef129e5c5a4d90a1def70e30600456a16f86f"),
    "_remove_profit_target": ("4fe333ab59e962f743375ddba0b6289233b8b40adc71ac8404d0b944ea1f3210"),
    "long_rebuy_adjust_trade_position_v3": (
        "c57bef2165c41fc9f3e9c1b90c92a1cd39323796d8f35d818b97856593f9cdf0"
    ),
}
_MANAGED_SHORT_METHOD_SHA256 = {
    # The pure predicates below are source-compiled. The wrapper is pinned
    # because its call order, stop boundary, and target-cache mutations are
    # executed by the handwritten Rust route.
    "short_exit_rebuy": (
        "bce3263e3df13f9f2873949631b1813d573aeb7e1beb48302409e466d9cdad1a"
    ),
}

_QUICK_RAPID_STATEFUL_FEATURES = {
    "last_candle": ["MFI_14", "RSI_3", "RSI_3_15m"],
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

_REBUY_ADJUSTMENT_FEATURES = {
    "last_candle": [
        "AROONU_14",
        "AROONU_14_15m",
        "EMA_26",
        "RSI_3",
        "RSI_3_15m",
        "close",
        "protections_long_global",
    ],
    "previous_candle_1": [],
}
_SHORT_REBUY_ADJUSTMENT_FEATURES = {
    "last_candle": [
        "AROOND_14",
        "AROOND_14_15m",
        "EMA_26",
        "RSI_3",
        "RSI_3_15m",
        "close",
        # This looks surprising for a short route, but it is the exact column
        # read by X7 v17.4.413. Renaming it would change strategy behavior.
        "protections_long_global",
    ],
    "previous_candle_1": [],
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
_MANAGED_LONG_ADJUSTMENT_FEATURES = {
    "last_candle": [
        "AROONU_14",
        "BBU_20_2.0",
        "BTC_RSI_14_4h",
        "EMA_20",
        "ROC_9_1d",
        "RSI_3",
        "RSI_3_15m",
        "RSI_3_1h",
        "RSI_3_4h",
        "RSI_14",
        "STOCHRSIk_14_14_3_3",
        "WILLR_14",
        "close",
    ],
    "previous_candle_1": [],
}

# X7 routes tag 120 through the older, independent grinding state machine.
# Its spot/backtest route is now lowered as one order-history state machine:
# first-entry recovery, two post-de-risk clusters, six grind clusters, their
# profit exits/stops, and the level-1 de-risk re-entry. Live partial-fill retry
# remains outside the simulator because a Freqtrade backtest exposes filled
# orders with ``safe_remaining == 0`` and cannot execute that branch.
_LONG_GRIND_ADJUSTMENT_SCOPE = "spot-grind-backtest-v1"
_LONG_BTC_ADJUSTMENT_SCOPE = "exit-only-v1"
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
)
_LONG_BTC_METHOD_SHA256 = {
    "long_exit_btc": "bcd170a5a79176914aafd2f026d7483b8c9607367953a8d947093aba92a606af",
    "long_grind_adjust_trade_position": (
        "f989ea57b2fe8c654d78a58bc45c0bd76a57aa41f4703440db98bc727e408cc9"
    ),
}
_LONG_GRIND_IMPLEMENTED_STEPS = (
    "legacy first-entry recovery",
    "legacy order-history reconstruction",
    "legacy post-de-risk grind levels 1-2",
    "legacy grind levels 1-6",
    "legacy grind profit exits and stops",
    "legacy de-risk level-1 re-entry",
)
_LONG_GRIND_REMAINING_STEPS = (
    "live partial-fill retry",
    "long-btc regular-mode adjustment",
    "legacy futures adjustment",
)

# These methods contain the stateful part of the handwritten Rust lowering.
# A whole-file source hash alone proves identity, but it would also let a new
# X7 version silently enter an old state machine after the descriptor rebuilt.
# Pinning the normalized method hashes makes a strategy change fail closed and
# forces a deliberate review of order classification, branch order, and stake
# arithmetic.
_ADJUSTMENT_METHOD_SHA256 = {
    "adjust_trade_position": ("64d19512c5968f3cc4e329a8a7b33eb93dc8ce9debbf39c4d8c70c09529dfd1a"),
    "calc_total_profit": ("ba0fc031f36140bbb3b5ae5feffa70ea7a5943e0315ff630407f2f92cdd9f70b"),
    "long_grind_adjust_trade_position_v3": (
        "ce49efa4449cf42610238f37456be6e5f5aac76e33af1fd244c3c8dd66ce03a8"
    ),
    "short_rebuy_adjust_trade_position_v3": (
        "539eb5c23f52650df0fc40474d0890aafe87df6830157197935f723e360fe801"
    ),
    "long_grind_entry_v3": ("717efe7dac38b6483391aa23974179f345b3bbe37b2822721cb9d97e1d1e8374"),
    "long_grind_exit_v3": ("48dde430a4d4607444af697ce3708089656f99cc1470450e53bdf1b2de8c5af4"),
    "profit_or_order_snapshot": (
        "d3460303e0dd66274f8e02782818bac8b910220c1947178f8d20836dd0217add"
    ),
    "scale_stakes_for_min_stake": (
        "9c08fcc82d086ee776962060bb55719db939a89137e106d458ebf030c666316c"
    ),
}

_ADJUSTMENT_BOOL_CONSTANTS = (
    "derisk_enable",
    "position_adjustment_enable",
    "system_v3_2_derisk_level_1_enable",
    "system_v3_2_derisk_level_2_enable",
    "system_v3_2_derisk_level_3_enable",
    "system_v3_2_derisk_level_4_enable",
    "system_v3_buyback_1_enable",
    "system_v3_grind_1_enable",
    "system_v3_grind_1_use_derisk",
    "system_v3_grind_2_enable",
    "system_v3_grind_2_use_derisk",
    "system_v3_grind_3_enable",
    "system_v3_grind_3_use_derisk",
    "system_v3_grind_4_enable",
    "system_v3_grind_4_use_derisk",
    "system_v3_grind_5_enable",
    "system_v3_grind_5_use_derisk",
)
_ADJUSTMENT_NUMBER_CONSTANTS = (
    "system_v3_max_stake",
    "system_v3_2_derisk_level_1_stake_futures",
    "system_v3_2_derisk_level_1_stake_spot",
    "system_v3_2_derisk_level_2_stake_futures",
    "system_v3_2_derisk_level_2_stake_spot",
    "system_v3_2_derisk_level_3_stake_futures",
    "system_v3_2_derisk_level_3_stake_spot",
)
_ADJUSTMENT_PAIR_CONSTANTS = (
    "system_v3_2_derisk_level_1_futures",
    "system_v3_2_derisk_level_1_spot",
    "system_v3_2_derisk_level_2_futures",
    "system_v3_2_derisk_level_2_spot",
    "system_v3_2_derisk_level_3_futures",
    "system_v3_2_derisk_level_3_spot",
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
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StrategyAnalysisError(f"NFI trade manager source cannot be read: {path}") from exc
    if hashlib.sha256(text.encode()).hexdigest() != source_sha256:
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
    _validate_managed_long_method_identity(methods, method_records)
    _validate_managed_short_method_identity(methods, method_records)

    # The top-coins route uses a literal tuple that can be checked
    # structurally in addition to its method hash. Other routes have equivalent
    # source order expressed through loops or sequential ``if not sell``
    # blocks, so their complete callback hash is the fail-closed boundary.
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

    constants = strategy.get("constants")
    if not isinstance(constants, dict):
        raise StrategyAnalysisError("NFI trade manager constants are invalid")
    managed_routes = _build_managed_long_routes(constants)
    managed_short_routes = _build_managed_short_routes(constants)
    managed_entry_tags = sorted(
        {tag for route in managed_routes.values() for tag in route["entry_tags"]}
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
    rebuy_adjustment_constants: dict[str, Any] | None = None
    if has_position_adjustment:
        _validate_adjustment_method_identity(method_records)
        adjustment_constants = _build_adjustment_constants(constants)
        rebuy_adjustment_constants = _build_rebuy_adjustment_constants(constants)

    # The stateful router calls its decisions through a tuple variable
    # (``exit_func``), so ordinary call-graph discovery cannot infer those
    # targets. Compile the structurally proven literal tuple as explicit roots.
    decision_roots = (
        (
            *_MANAGED_LONG_PROGRAM_ORDER,
            *_MANAGED_SHORT_PROGRAM_ORDER,
            _MANAGED_LONG_ADJUSTMENT_PROGRAM,
        )
        if has_position_adjustment
        else (*_MANAGED_LONG_PROGRAM_ORDER, *_MANAGED_SHORT_PROGRAM_ORDER)
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

    method_identity = {
        name: {
            "source_sha256": method_records[name]["source_sha256"],
            "location": method_records[name]["location"],
        }
        for name in _MANAGED_LONG_METHOD_SHA256
    }
    method_identity.update(
        {
            name: {
                "source_sha256": method_records[name]["source_sha256"],
                "location": method_records[name]["location"],
            }
            for name in _MANAGED_SHORT_METHOD_SHA256
        }
    )
    method_identity.update(long_grind_method_identity)
    method_identity.update(long_btc_method_identity)
    supported_routes: dict[str, Any] = dict(managed_routes)
    if long_grind_route is not None:
        supported_routes["long_grind"] = long_grind_route
    if long_btc_route is not None:
        supported_routes["long_btc"] = long_btc_route
    route_order = [name for name in _MANAGED_LONG_ROUTE_ORDER if name in supported_routes]
    operation = {
        "opcode": "nfi-x7-trade-manager-v1",
        "schema_version": NFI_TRADE_MANAGER_IR_VERSION,
        "source_sha256": source_sha256,
        "supported_routes": supported_routes,
        "route_order": route_order,
        "supported_short_routes": managed_short_routes,
        "short_route_order": ["short_rebuy"],
        "constants": frozen_constants,
        "programs": {name: programs[name] for name in decision_roots},
    }
    if adjustment_constants is not None:
        operation["position_adjustment"] = {
            "enabled": constants["position_adjustment_enable"],
            # These are exactly X7's ``long_adjust_mode_tags`` for the
            # supported source snapshot. Rebuy/grind/BTC tags use different
            # adjustment callbacks and are deliberately excluded.
            "entry_tags": managed_entry_tags,
            "system_version": frozen_constants["system_v3_2_name"],
            "decision_program": _MANAGED_LONG_ADJUSTMENT_PROGRAM,
            "program_order": [
                "derisk_level_1",
                "derisk_level_2",
                "derisk_level_3",
                "grind_1_entry",
                "grind_1_exit",
                "grind_1_derisk",
                "grind_2_entry",
                "grind_2_exit",
                "grind_2_derisk",
                "grind_3_entry",
                "grind_3_exit",
                "grind_3_derisk",
                "grind_4_entry",
                "grind_4_exit",
                "grind_4_derisk",
                "grind_5_entry",
                "grind_5_exit",
                "grind_5_derisk",
            ],
            "stateful_input_contract": {
                "indexed_fields": _MANAGED_LONG_ADJUSTMENT_FEATURES,
            },
            "constants": adjustment_constants,
        }
    if rebuy_adjustment_constants is not None:
        rebuy_route = managed_routes["long_rebuy"]
        operation["rebuy_adjustment"] = {
            "enabled": constants["position_adjustment_enable"],
            "entry_tags": rebuy_route["entry_tags"],
            "system_version": frozen_constants["system_v3_2_name"],
            "stateful_input_contract": {
                "indexed_fields": _REBUY_ADJUSTMENT_FEATURES,
            },
            "constants": rebuy_adjustment_constants,
        }
        short_rebuy_route = managed_short_routes["short_rebuy"]
        operation["short_rebuy_adjustment"] = {
            "enabled": constants["position_adjustment_enable"],
            "entry_tags": short_rebuy_route["entry_tags"],
            "system_version": frozen_constants["system_v3_2_name"],
            "execution_scope": "pre-derisk-only-v1",
            # After the first level-3 de-risk, X7 delegates to the independent
            # short-grind state machine. The simulator must reject that
            # transition until the whole downstream method is lowered.
            "post_derisk_action": "fail-simulation",
            "stateful_input_contract": {
                "indexed_fields": _SHORT_REBUY_ADJUSTMENT_FEATURES,
            },
            "constants": rebuy_adjustment_constants,
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
            "matcher": "nfi-x7-managed-long-short-rebuy-router-v1",
            "source_sha256": source_sha256,
            "trade_ir_fingerprint": trade_dependency_ir["fingerprint"],
            "decision_ir_fingerprint": decision_report["fingerprint"],
            "operation_sha256": hashlib.sha256(encoded).hexdigest(),
            "programs": program_proof,
            "stateful_methods": method_identity,
        },
        "implemented_steps": [
            "ordered managed-long route dispatch",
            *_MANAGED_LONG_STATEFUL_STEPS,
            "ordered short-rebuy pure exit dispatch",
            "short-rebuy stop and target state",
            "short-rebuy pre-derisk position adjustment",
            *(_LONG_GRIND_IMPLEMENTED_STEPS if long_grind_route is not None else ()),
        ],
        "remaining_steps": (
            [*_LONG_GRIND_REMAINING_STEPS, "short-rebuy post-derisk grind adjustment"]
            if long_grind_route is not None or long_btc_route is not None
            else ["short-rebuy post-derisk grind adjustment"]
        ),
    }


def _validate_managed_long_method_identity(
    methods: dict[str, ast.FunctionDef],
    method_records: dict[str, dict[str, Any]],
) -> None:
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
        if method_records.get(name, {}).get("source_sha256") != expected
    ]
    if changed:
        raise StrategyAnalysisError(
            "NFI X7 managed-long route changed; exact lowering requires review: "
            + ", ".join(changed)
        )


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
    """Freeze only X7's short-rebuy route.

    Other short families remain outside the executable scope. Keeping a single
    explicit descriptor makes tag 562 support auditable and prevents a shared
    "short profile" abstraction from accidentally enabling unreviewed routes.
    """
    mode_name = constants.get("short_rebuy_mode_name")
    entry_tags = constants.get("short_rebuy_mode_tags")
    if not isinstance(mode_name, str) or not mode_name:
        raise StrategyAnalysisError("NFI short_rebuy mode name must be frozen")
    if (
        not isinstance(entry_tags, list)
        or not entry_tags
        or not all(isinstance(tag, str) and tag for tag in entry_tags)
    ):
        raise StrategyAnalysisError("NFI short_rebuy entry tags must be frozen strings")
    stop_names = _ROUTE_STOP_CONSTANTS["rebuy"]
    stop_values = [constants.get(name) for name in stop_names]
    if any(
        isinstance(value, bool) or not isinstance(value, int | float)
        for value in stop_values
    ):
        raise StrategyAnalysisError("NFI short_rebuy stop thresholds must be numeric")
    return {
        "short_rebuy": {
            "profile": "rebuy",
            "mode_name": mode_name,
            "entry_tags": list(dict.fromkeys(entry_tags)),
            "stop_threshold_futures": stop_values[0],
            "stop_threshold_spot": stop_values[1],
            "program_order": list(_MANAGED_SHORT_PROGRAM_ORDER),
            "stateful_input_contract": {
                "indexed_fields": {
                    name: list(fields)
                    for name, fields in _MANAGED_LONG_STATEFUL_FEATURES.items()
                }
            },
        }
    }


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


def _build_long_grind_route(
    constants: Any,
    methods: dict[str, ast.FunctionDef],
    method_records: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Describe the reviewed tag-120 branch without widening its proof.

    X7 keeps this legacy route beside the system-v3.2 adjustment machinery.
    We only publish it when both stateful methods and every required constant
    are present. Once the shape is recognizable, a changed method hash is a
    hard error: silently falling back to an older handwritten state machine
    would turn a source update into an undetected parity bug.
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
    """Describe tag-121, which X7 sends through the same legacy grind callback."""
    return _build_legacy_grind_route(
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
    )


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
        "derisk_use_grind_stops": derisk,
        "stateful_input_contract": {
            "indexed_fields": {
                "last_candle": [
                    "global_protections_long_dump",
                    "global_protections_long_pump",
                ],
                "previous_candle": [],
            }
        },
        "constants": _build_legacy_grind_constants(constants),
    }
    return route, identity


def _build_legacy_grind_constants(constants: dict[str, Any]) -> dict[str, Any]:
    """Freeze the repeated constants read by the legacy grind callback.

    The source names eight clusters separately. The IR stores them in source
    execution order so Rust can use one reviewed evaluator without duplicating
    the same entry/exit/stop arithmetic eight times.
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
    cluster_specs = [
        ("gd1", "dd1", "grind_1"),
        ("gd2", "dd2", "grind_2"),
        ("gd3", "dd3", "grind_3"),
        ("gd4", "dd4", "grind_4"),
        ("gd5", "dd5", "grind_5"),
        ("gd6", "dd6", "grind_6"),
        ("dl1", "ddl1", "grind_1_derisk_1"),
        ("dl2", "ddl2", "grind_2_derisk_1"),
    ]
    for entry_tag, stop_tag, prefix in cluster_specs:
        record: dict[str, Any] = {
            "entry_tag": entry_tag,
            "stop_tag": stop_tag,
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
        "stake_multipliers_futures": number_list("grind_mode_stake_multiplier_futures"),
        "stake_multipliers_spot": number_list("grind_mode_stake_multiplier_spot"),
        "derisk_1_reentry_futures": number("regular_mode_derisk_1_reentry_futures"),
        "derisk_1_reentry_spot": number("regular_mode_derisk_1_reentry_spot"),
        "clusters": clusters,
    }


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
        if methods.get(name, {}).get("source_sha256") != expected
    ]
    if changed:
        raise StrategyAnalysisError(
            "NFI X7 position adjustment changed; exact lowering requires review: "
            + ", ".join(changed)
        )


def _build_adjustment_constants(constants: dict[str, Any]) -> dict[str, Any]:
    """Freeze the reachable system-v3.2 adjustment constants.

    Buyback and level-4 de-risk branches are deliberately required to be
    disabled. Supporting a disabled branch by omission is exact; accepting it
    after a strategy change would not be.
    """
    for name in _ADJUSTMENT_BOOL_CONSTANTS:
        if not isinstance(constants.get(name), bool):
            raise StrategyAnalysisError(f"NFI adjustment constant {name} must be boolean")
    for name in _ADJUSTMENT_NUMBER_CONSTANTS:
        value = constants.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise StrategyAnalysisError(f"NFI adjustment constant {name} must be numeric")
    for name in _ADJUSTMENT_PAIR_CONSTANTS:
        values = constants.get(name)
        if (
            not isinstance(values, list)
            or len(values) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int | float) for value in values
            )
        ):
            raise StrategyAnalysisError(f"NFI adjustment constant {name} must be a numeric pair")
    if not constants["position_adjustment_enable"]:
        raise StrategyAnalysisError("NFI position adjustment is disabled")
    if constants["system_v3_2_derisk_level_4_enable"]:
        raise StrategyAnalysisError("NFI de-risk level 4 is not lowered")
    if constants["system_v3_buyback_1_enable"]:
        raise StrategyAnalysisError("NFI buyback route is not lowered")

    grinds: list[dict[str, Any]] = []
    for level in range(1, 6):
        prefix = f"system_v3_grind_{level}_"
        record: dict[str, Any] = {
            "level": level,
            "enabled": constants[f"{prefix}enable"],
            "use_derisk": constants[f"{prefix}use_derisk"],
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

    return {
        "derisk_enable": constants["derisk_enable"],
        "max_stake_multiplier": constants["system_v3_max_stake"],
        "derisk_levels": [
            {
                "level": level,
                "enabled": constants[f"system_v3_2_derisk_level_{level}_enable"],
                "threshold_futures": constants[f"system_v3_2_derisk_level_{level}_futures"][1],
                "threshold_spot": constants[f"system_v3_2_derisk_level_{level}_spot"][1],
                "stake_futures": constants[f"system_v3_2_derisk_level_{level}_stake_futures"],
                "stake_spot": constants[f"system_v3_2_derisk_level_{level}_stake_spot"],
            }
            for level in range(1, 4)
        ],
        "grinds": grinds,
    }


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
