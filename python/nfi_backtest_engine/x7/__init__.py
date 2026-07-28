"""NFI X7 source-bound adapter and trade-manager implementation."""

from .adapter import (
    X7_ADAPTER_VERSION,
    build_x7_simulation_input,
    build_x7_vector_manifest,
)
from .contracts import x7_adapter_blockers
from .trade_manager import NFI_TRADE_MANAGER_IR_VERSION, build_nfi_trade_manager_ir

__all__ = [
    "NFI_TRADE_MANAGER_IR_VERSION",
    "X7_ADAPTER_VERSION",
    "build_nfi_trade_manager_ir",
    "build_x7_simulation_input",
    "build_x7_vector_manifest",
    "x7_adapter_blockers",
]
