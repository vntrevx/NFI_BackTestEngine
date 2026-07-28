"""Compatibility facade for the source-bound NFI X7 trade-manager IR."""

from .x7.adjustments import _adjustment_literal_policy
from .x7.legacy import _legacy_futures_fallback_loss_threshold
from .x7.routes import _extract_rebuy_terminal_exit, _method_ast_sha256
from .x7.trade_manager import NFI_TRADE_MANAGER_IR_VERSION, build_nfi_trade_manager_ir

__all__ = [
    "NFI_TRADE_MANAGER_IR_VERSION",
    "_adjustment_literal_policy",
    "_extract_rebuy_terminal_exit",
    "_legacy_futures_fallback_loss_threshold",
    "_method_ast_sha256",
    "build_nfi_trade_manager_ir",
]
