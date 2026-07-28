"""Compatibility facade for the X7 adapter public and tested private surface."""

from .x7.adapter import (
    X7_ADAPTER_VERSION,
    build_x7_simulation_input,
    build_x7_vector_manifest,
)
from .x7.contracts import x7_adapter_blockers
from .x7.serialization import _nfi_trade_manager_config
from .x7.vectors import _optional_text

# The v1.1 regression contract freezes these diagnostic identifiers at this
# compatibility path. Emission and validation live in ``x7.adapter``.
_STABLE_ERROR_CODE_INVENTORY = (
    "X7_CALLBACK_IR_INCOMPLETE",
    "X7_ADAPTER_BACKEND_UNSUPPORTED",
    "X7_PROTECTION_CONTRACT_INVALID",
    "X7_STOPLOSS_CONFIG_INVALID",
    "X7_POSITION_CALLBACK_REQUIRED",
    "MARKET_METADATA_INVALID",
    "MARKET_LIMITS_REQUIRED",
    "X7_LIQUIDATION_CONTRACT_INVALID",
    "X7_NUMERIC_CONFIG_REQUIRED",
    "X7_STAKE_CONFIG_INVALID",
    "X7_FUTURES_LEVERAGE_REQUIRED",
    "X7_FUTURES_LEVERAGE_INVALID",
)

__all__ = [
    "X7_ADAPTER_VERSION",
    "_nfi_trade_manager_config",
    "_optional_text",
    "build_x7_simulation_input",
    "build_x7_vector_manifest",
    "x7_adapter_blockers",
]
