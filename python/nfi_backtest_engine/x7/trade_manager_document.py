"""Canonical document assembly for compiled X7 manager components."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..errors import StrategyAnalysisError
from .route_contracts import MANAGED_LONG_STATEFUL_STEPS
from .trade_manager_compilation import TradeManagerCompilation
from .trade_manager_constants import (
    BACKTEST_EXCLUSIONS,
    LONG_BTC_IMPLEMENTED_STEPS,
    LONG_GRIND_IMPLEMENTED_STEPS,
    MANAGED_LONG_ADJUSTMENT_PROGRAM,
    MANAGED_LONG_FROZEN_CONSTANTS,
    MANAGED_SHORT_ADJUSTMENT_PROGRAM,
)
from .trade_manager_source import TradeManagerSource

NFI_TRADE_MANAGER_IR_VERSION = "0.31.0"


def _adjustment_program_order(constants: dict[str, Any]) -> list[str]:
    return [
        *(f"derisk_level_{record['level']}" for record in constants["derisk_levels"]),
        *(
            f"grind_{record['level']}_{action}"
            for record in constants["grinds"]
            for action in ("entry", "exit", "derisk")
        ),
    ]


def _frozen_constants(constants: dict[str, Any]) -> dict[str, Any]:
    frozen = {name: constants.get(name) for name in MANAGED_LONG_FROZEN_CONSTANTS}
    if not all(
        isinstance(frozen[name], bool)
        for name in (
            "derisk_enable", "stops_enable", "system_v3_2_stops_enable",
            "u_e_stops_enable",
        )
    ):
        raise StrategyAnalysisError("NFI top-coins boolean constants are invalid")
    if not all(
        isinstance(frozen[name], int | float) and not isinstance(frozen[name], bool)
        for name in (
            "stop_threshold_futures", "stop_threshold_spot",
            "system_v3_2_stop_threshold_doom_futures",
            "system_v3_2_stop_threshold_doom_spot",
        )
    ):
        raise StrategyAnalysisError("NFI top-coins numeric stop constants are invalid")
    if (
        not isinstance(frozen["system_name_use"], str)
        or frozen["system_name_use"] != frozen["system_v3_2_name"]
    ):
        raise StrategyAnalysisError(
            "NFI top-coins lowering currently requires frozen system_v3_2 routing"
        )
    return frozen


def assemble_trade_manager_document(
    source: TradeManagerSource,
    trade_dependency_ir: dict[str, Any],
    compilation: TradeManagerCompilation,
) -> dict[str, Any]:
    constants = source.constants
    frozen = _frozen_constants(constants)
    managed_routes = compilation.managed_routes
    short_routes = compilation.managed_short_routes
    supported_routes = dict(managed_routes)
    if compilation.long_grind_route is not None:
        supported_routes["long_grind"] = compilation.long_grind_route
    if compilation.long_btc_route is not None:
        supported_routes["long_btc"] = compilation.long_btc_route
    route_order = [
        name
        for name in compilation.managed_exit.long_route_order
        if name in supported_routes
    ]
    if set(route_order) != set(supported_routes):
        raise StrategyAnalysisError("NFI custom_exit long route inventory is incomplete")

    operation: dict[str, Any] = {
        "opcode": "nfi-x7-trade-manager-v1",
        "schema_version": NFI_TRADE_MANAGER_IR_VERSION,
        "source_sha256": source.source_sha256,
        "supported_routes": supported_routes,
        "route_order": route_order,
        "managed_exit_program": compilation.managed_exit.program,
        "managed_short_exit_program": compilation.managed_short_exit.program,
        "supported_short_routes": short_routes,
        "short_route_order": list(compilation.managed_short_exit.short_route_order),
        "short_grind": compilation.short_grind_route,
        "constants": frozen,
        "programs": {
            name: compilation.programs[name] for name in compilation.decision_roots
        },
    }
    managed_tags = sorted(
        {tag for route in managed_routes.values() for tag in route["entry_tags"]}
    )
    short_adjustment_tags = sorted(
        {
            tag
            for key, route in short_routes.items()
            if key != "short_rebuy"
            for tag in route["entry_tags"]
        }
    )
    if compilation.adjustment_constants is not None and compilation.adjustment_program is not None:
        operation["position_adjustment"] = {
            "enabled": constants["position_adjustment_enable"],
            "entry_tags": managed_tags,
            "system_version": frozen["system_v3_2_name"],
            "source_callback": source.methods["long_grind_adjust_trade_position_v3"].name,
            "decision_program": MANAGED_LONG_ADJUSTMENT_PROGRAM,
            "program_order": _adjustment_program_order(compilation.adjustment_constants),
            "stateful_input_contract": compilation.adjustment_program["input_contract"],
            "constants": compilation.adjustment_constants,
            "program": compilation.adjustment_program,
        }
    if (
        compilation.short_adjustment_constants is not None
        and compilation.short_adjustment_program is not None
    ):
        operation["short_position_adjustment"] = {
            "enabled": constants["position_adjustment_enable"],
            "entry_tags": short_adjustment_tags,
            "system_version": frozen["system_v3_2_name"],
            "source_callback": source.methods["short_grind_adjust_trade_position_v3"].name,
            "decision_program": MANAGED_SHORT_ADJUSTMENT_PROGRAM,
            "program_order": _adjustment_program_order(compilation.short_adjustment_constants),
            "stateful_input_contract": compilation.short_adjustment_program["input_contract"],
            "constants": compilation.short_adjustment_constants,
            "program": compilation.short_adjustment_program,
        }
    if (
        compilation.rebuy_adjustment_constants is not None
        and compilation.rebuy_transition_program is not None
        and compilation.short_rebuy_transition_program is not None
    ):
        operation["rebuy_adjustment"] = {
            "enabled": constants["position_adjustment_enable"],
            "entry_tags": managed_routes["long_rebuy"]["entry_tags"],
            "system_version": frozen["system_v3_2_name"],
            "stateful_input_contract": compilation.rebuy_transition_program["input_contract"],
            "constants": compilation.rebuy_adjustment_constants,
            "program": compilation.rebuy_transition_program,
        }
        operation["short_rebuy_adjustment"] = {
            "enabled": constants["position_adjustment_enable"],
            "entry_tags": short_routes["short_rebuy"]["entry_tags"],
            "system_version": frozen["system_v3_2_name"],
            "execution_scope": "rebuy-and-grind-v2",
            "post_derisk_action": "short-position-adjustment",
            "stateful_input_contract": compilation.short_rebuy_transition_program[
                "input_contract"
            ],
            "constants": compilation.rebuy_adjustment_constants,
            "program": compilation.short_rebuy_transition_program,
        }
    encoded = json.dumps(
        operation, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return _manager_envelope(
        source, trade_dependency_ir, compilation, operation,
        hashlib.sha256(encoded).hexdigest(),
    )


def _manager_envelope(
    source: TradeManagerSource,
    trade_dependency_ir: dict[str, Any],
    compilation: TradeManagerCompilation,
    operation: dict[str, Any],
    operation_sha256: str,
) -> dict[str, Any]:
    def fingerprint(program: dict[str, Any] | None) -> str | None:
        return program["fingerprint"] if program is not None else None

    return {
        "schema_version": NFI_TRADE_MANAGER_IR_VERSION,
        "backend": "rust-nfi-x7-trade-manager",
        "executable_in_rust": True,
        "execution_scope": {
            "sides": ["long", "short"], "entry_tag_match": "any",
            "unsupported_action": "fail-before-simulation",
        },
        "operation": operation,
        "proof": {
            "matcher": "nfi-x7-managed-long-short-router-v2",
            "source_sha256": source.source_sha256,
            "trade_ir_fingerprint": trade_dependency_ir["fingerprint"],
            "decision_ir_fingerprint": compilation.decision_report["fingerprint"],
            "managed_exit_ir_fingerprint": compilation.managed_exit.program["fingerprint"],
            "managed_short_exit_ir_fingerprint": compilation.managed_short_exit.program[
                "fingerprint"
            ],
            "rebuy_transition_ir_fingerprint": fingerprint(compilation.rebuy_transition_program),
            "short_rebuy_transition_ir_fingerprint": fingerprint(
                compilation.short_rebuy_transition_program
            ),
            "system_adjustment_ir_fingerprint": fingerprint(compilation.adjustment_program),
            "short_system_adjustment_ir_fingerprint": fingerprint(
                compilation.short_adjustment_program
            ),
            "legacy_grind_ir_fingerprint": fingerprint(
                compilation.long_grind_route["program"]
                if compilation.long_grind_route is not None else None
            ),
            "operation_sha256": operation_sha256,
            "programs": compilation.program_proof,
            "stateful_methods": compilation.method_identity,
        },
        "implemented_steps": [
            "ordered managed-long route dispatch", *MANAGED_LONG_STATEFUL_STEPS,
            "ordered managed-short route dispatch", "managed-short stop and target state",
            "short system-v3.2 grind position adjustment",
            "short-rebuy ladder and post-derisk grind transfer",
            *(LONG_GRIND_IMPLEMENTED_STEPS if compilation.long_grind_route is not None else ()),
            *(LONG_BTC_IMPLEMENTED_STEPS if compilation.long_btc_route is not None else ()),
        ],
        "backtest_exclusions": [dict(item) for item in BACKTEST_EXCLUSIONS],
        "remaining_steps": [],
    }
