"""Declarative route contracts for the source-bound X7 trade manager."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MANAGED_LONG_PROGRAM_ORDER = (
    "long_exit_signals",
    "long_exit_main",
    "long_exit_williams_r",
    "long_exit_dec",
)
MANAGED_SHORT_PROGRAM_ORDER = (
    "short_exit_signals",
    "short_exit_main",
    "short_exit_williams_r",
    "short_exit_dec",
)
MANAGED_LONG_STATEFUL_STEPS = (
    "long_exit_stoploss",
    "exit_profit_target",
    "mark_profit_target",
    "_set_profit_target",
    "_remove_profit_target",
)
MANAGED_SHORT_STATEFUL_STEPS = ("short_exit_stoploss",)
MANAGED_LONG_STATEFUL_FEATURES = {
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
QUICK_RAPID_STATEFUL_FEATURES = {
    "last_candle": ["MFI_14", "RSI_3", "RSI_3_15m", "WILLR_14"],
    "previous_candle_1": [],
}
ROUTE_STOP_CONSTANTS = {
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


@dataclass(frozen=True)
class ManagedRouteSpec:
    """One reviewed branch in X7's ordered ``custom_exit`` router."""

    key: str
    side: Literal["long", "short"]
    profile: str
    mode_constant: str
    tags_constant: str
    method: str
    program_order: tuple[str, ...]


def _route(
    key: str,
    side: Literal["long", "short"],
    profile: str,
    method: str,
    program_order: tuple[str, ...],
    *,
    mode: str | None = None,
    tags: str | None = None,
) -> ManagedRouteSpec:
    stem = key.removesuffix("_fallback")
    return ManagedRouteSpec(
        key=key,
        side=side,
        profile=profile,
        mode_constant=mode or f"{stem}_mode_name",
        tags_constant=tags or f"{stem}_mode_tags",
        method=method,
        program_order=program_order,
    )


MANAGED_LONG_ROUTE_SPECS = (
    _route("long_normal", "long", "normal", "long_exit_normal", MANAGED_LONG_PROGRAM_ORDER),
    _route("long_pump", "long", "pump", "long_exit_pump", MANAGED_LONG_PROGRAM_ORDER),
    _route("long_quick", "long", "quick", "long_exit_quick", MANAGED_LONG_PROGRAM_ORDER),
    _route("long_rebuy", "long", "rebuy", "long_exit_rebuy", MANAGED_LONG_PROGRAM_ORDER),
    _route(
        "long_high_profit",
        "long",
        "high-profit",
        "long_exit_high_profit",
        MANAGED_LONG_PROGRAM_ORDER[:3],
    ),
    _route("long_rapid", "long", "rapid", "long_exit_rapid", MANAGED_LONG_PROGRAM_ORDER),
    _route(
        "long_top_coins",
        "long",
        "top-coins",
        "long_exit_top_coins",
        MANAGED_LONG_PROGRAM_ORDER,
    ),
    _route("long_scalp", "long", "scalp", "long_exit_scalp", MANAGED_LONG_PROGRAM_ORDER),
)

# Upstream has no explicit short top-coins dispatch block. Those tags execute
# the normal fallback, so the contract names the callback that actually runs.
MANAGED_SHORT_ROUTE_SPECS = (
    _route("short_normal", "short", "normal", "short_exit_normal", MANAGED_SHORT_PROGRAM_ORDER),
    _route("short_pump", "short", "pump", "short_exit_pump", MANAGED_SHORT_PROGRAM_ORDER),
    _route("short_quick", "short", "quick", "short_exit_quick", MANAGED_SHORT_PROGRAM_ORDER),
    _route("short_rebuy", "short", "rebuy", "short_exit_rebuy", MANAGED_SHORT_PROGRAM_ORDER),
    _route(
        "short_high_profit",
        "short",
        "high-profit",
        "short_exit_high_profit",
        MANAGED_SHORT_PROGRAM_ORDER[:3],
    ),
    _route("short_rapid", "short", "rapid", "short_exit_rapid", MANAGED_SHORT_PROGRAM_ORDER),
    _route("short_scalp", "short", "scalp", "short_exit_scalp", MANAGED_SHORT_PROGRAM_ORDER),
    _route(
        "short_top_coins_fallback",
        "short",
        "normal",
        "short_exit_normal",
        MANAGED_SHORT_PROGRAM_ORDER,
        mode="short_normal_mode_name",
        tags="short_top_coins_mode_tags",
    ),
)
