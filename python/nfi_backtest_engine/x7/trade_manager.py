"""Stable facade for the source-bound NFI X7 trade-manager IR.

Route declarations and assembly concerns live in focused private modules. This
module retains the established manager entry point and compatibility names used
by the adjacent X7 compilers.
"""

from __future__ import annotations

from typing import Any

from . import route_contracts as _routes
from . import trade_manager_constants as _constants
from .trade_manager_source import load_trade_manager_source

NFI_TRADE_MANAGER_IR_VERSION = "0.30.0"

__all__ = [
    "NFI_TRADE_MANAGER_IR_VERSION",
    "build_nfi_trade_manager_ir",
]

# Compatibility bindings for adjacent compilers. Their values are declared in
# independent modules so importing those compilers no longer creates a cycle.
_MANAGED_LONG_PROGRAM_ORDER = _routes.MANAGED_LONG_PROGRAM_ORDER
_MANAGED_SHORT_PROGRAM_ORDER = _routes.MANAGED_SHORT_PROGRAM_ORDER
_MANAGED_LONG_ROUTE_SPECS = _routes.MANAGED_LONG_ROUTE_SPECS
_MANAGED_SHORT_ROUTE_SPECS = _routes.MANAGED_SHORT_ROUTE_SPECS
_MANAGED_LONG_STATEFUL_STEPS = _routes.MANAGED_LONG_STATEFUL_STEPS
_MANAGED_SHORT_STATEFUL_STEPS = _routes.MANAGED_SHORT_STATEFUL_STEPS
_MANAGED_LONG_STATEFUL_FEATURES = _routes.MANAGED_LONG_STATEFUL_FEATURES
_QUICK_RAPID_STATEFUL_FEATURES = _routes.QUICK_RAPID_STATEFUL_FEATURES
_ROUTE_STOP_CONSTANTS = _routes.ROUTE_STOP_CONSTANTS
_MANAGED_SHORT_ROUTE_ORDER = tuple(spec.key for spec in _MANAGED_SHORT_ROUTE_SPECS)

_ADJUSTMENT_BOOL_CONSTANTS = _constants.ADJUSTMENT_BOOL_CONSTANTS
_ADJUSTMENT_GRIND_FIELDS = _constants.ADJUSTMENT_GRIND_FIELDS
_ADJUSTMENT_NUMBER_CONSTANTS = _constants.ADJUSTMENT_NUMBER_CONSTANTS
_REBUY_ADJUSTMENT_LIST_CONSTANTS = _constants.REBUY_ADJUSTMENT_LIST_CONSTANTS
_REBUY_ADJUSTMENT_NUMBER_CONSTANTS = _constants.REBUY_ADJUSTMENT_NUMBER_CONSTANTS
_LONG_BTC_ADJUSTMENT_SCOPE = _constants.LONG_BTC_ADJUSTMENT_SCOPE
_LONG_BTC_STATEFUL_METHODS = _constants.LONG_BTC_STATEFUL_METHODS
_LONG_GRIND_ADJUSTMENT_SCOPE = _constants.LONG_GRIND_ADJUSTMENT_SCOPE
_LONG_GRIND_STATEFUL_METHODS = _constants.LONG_GRIND_STATEFUL_METHODS
_LONG_REGULAR_ADJUSTMENT_PROGRAM = _constants.LONG_REGULAR_ADJUSTMENT_PROGRAM
_MANAGED_LONG_ADJUSTMENT_PROGRAM = _constants.MANAGED_LONG_ADJUSTMENT_PROGRAM


def build_nfi_trade_manager_ir(
    analysis: dict[str, Any],
    trade_dependency_ir: dict[str, Any],
) -> dict[str, Any] | None:
    """Build a scope-limited executable X7 manager from hash-bound source."""

    # Delayed assembly imports keep compatibility imports from older adjacent
    # modules acyclic while the facade remains their stable access point.
    from .trade_manager_compilation import compile_trade_manager
    from .trade_manager_document import assemble_trade_manager_document

    source = load_trade_manager_source(analysis)
    if source is None:
        return None
    compilation = compile_trade_manager(analysis, source)
    return assemble_trade_manager_document(source, trade_dependency_ir, compilation)
