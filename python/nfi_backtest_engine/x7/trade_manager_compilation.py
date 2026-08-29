"""Focused compiler orchestration for X7 manager route programs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import StrategyAnalysisError
from ..trade_ir import build_trade_dependency_ir
from .adjustment_ir import compile_system_adjustment_ir
from .adjustments import _build_adjustment_constants, _build_rebuy_adjustment_constants
from .legacy import _build_long_btc_route, _build_long_grind_route, _build_short_grind_route
from .managed_exit_ir import ManagedExitCompilation, compile_managed_exit_ir
from .managed_short_exit_ir import ManagedShortExitCompilation, compile_managed_short_exit_ir
from .rebuy_ir import compile_rebuy_transition_ir
from .route_contracts import (
    MANAGED_LONG_PROGRAM_ORDER,
    MANAGED_LONG_ROUTE_SPECS,
    MANAGED_SHORT_ROUTE_SPECS,
)
from .routes import (
    _build_managed_long_routes,
    _build_managed_short_routes,
    _extract_rebuy_terminal_exit,
    _require_managed_long_methods,
    _require_managed_short_methods,
    _top_coins_program_order,
)
from .trade_manager_constants import (
    LONG_REGULAR_ADJUSTMENT_PROGRAM,
    MANAGED_LONG_ADJUSTMENT_PROGRAM,
    MANAGED_SHORT_ADJUSTMENT_PROGRAM,
)
from .trade_manager_source import TradeManagerSource


@dataclass(frozen=True)
class TradeManagerCompilation:
    managed_exit: ManagedExitCompilation
    managed_short_exit: ManagedShortExitCompilation
    managed_routes: dict[str, Any]
    managed_short_routes: dict[str, Any]
    long_grind_route: dict[str, Any] | None
    long_btc_route: dict[str, Any] | None
    short_grind_route: dict[str, Any] | None
    adjustment_constants: dict[str, Any] | None
    adjustment_program: dict[str, Any] | None
    short_adjustment_constants: dict[str, Any] | None
    short_adjustment_program: dict[str, Any] | None
    rebuy_adjustment_constants: dict[str, Any] | None
    rebuy_transition_program: dict[str, Any] | None
    short_rebuy_transition_program: dict[str, Any] | None
    decision_report: dict[str, Any]
    decision_roots: tuple[str, ...]
    programs: dict[str, Any]
    program_proof: dict[str, Any]
    method_identity: dict[str, Any]
    has_position_adjustment: bool


def compile_trade_manager(
    analysis: dict[str, Any],
    source: TradeManagerSource,
) -> TradeManagerCompilation:
    methods = source.methods
    constants = source.constants
    strategy = source.strategy
    _require_managed_long_methods(methods)
    _require_managed_short_methods(methods)
    rebuy_terminal_exit, _ = _extract_rebuy_terminal_exit(methods["long_exit_rebuy"])
    managed_exit = compile_managed_exit_ir(
        methods,
        constants,
        MANAGED_LONG_ROUTE_SPECS,
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
    managed_short_exit = compile_managed_short_exit_ir(
        methods,
        constants,
        MANAGED_SHORT_ROUTE_SPECS,
    )
    if _top_coins_program_order(methods["long_exit_top_coins"]) != MANAGED_LONG_PROGRAM_ORDER:
        raise StrategyAnalysisError(
            "NFI X7 top-coins pure exit order changed; exact lowering must be reviewed"
        )

    long_grind_route, long_grind_identity = _build_long_grind_route(
        strategy.get("constants"), methods, strategy.get("methods")
    )
    long_btc_route, long_btc_identity = _build_long_btc_route(
        strategy.get("constants"), methods, strategy.get("methods")
    )
    short_grind_route, short_grind_identity = _build_short_grind_route(
        strategy.get("constants"), methods, strategy.get("methods")
    )
    managed_routes = _build_managed_long_routes(constants)
    if rebuy_terminal_exit is not None:
        managed_routes["long_rebuy"]["terminal_exit"] = rebuy_terminal_exit
    managed_short_routes = _build_managed_short_routes(constants)

    has_adjustment = (
        "adjust_trade_position" in source.method_records
        and constants.get("position_adjustment_enable") is True
    )
    adjustment_constants = None
    adjustment_program = None
    short_adjustment_constants = None
    short_adjustment_program = None
    rebuy_constants = None
    rebuy_program = None
    short_rebuy_program = None
    if has_adjustment:
        adjustment_constants = _build_adjustment_constants(
            constants, methods["long_grind_adjust_trade_position_v3"], side="long"
        )
        short_adjustment_constants = _build_adjustment_constants(
            constants, methods["short_grind_adjust_trade_position_v3"], side="short"
        )
        rebuy_constants = _build_rebuy_adjustment_constants(constants)
        long_policy = adjustment_constants.get("policy")
        short_policy = short_adjustment_constants.get("policy")
        if not isinstance(long_policy, dict) or not isinstance(short_policy, dict):
            raise StrategyAnalysisError("NFI rebuy delegate policy is unavailable")
        adjustment_program = compile_system_adjustment_ir(
            methods["long_grind_adjust_trade_position_v3"],
            methods["long_grind_exit_v3"], constants, side="long", retry_policy=long_policy
        )
        short_adjustment_program = compile_system_adjustment_ir(
            methods["short_grind_adjust_trade_position_v3"],
            methods["short_grind_exit_v3"], constants, side="short", retry_policy=short_policy
        )
        rebuy_program = compile_rebuy_transition_ir(
            methods["long_rebuy_adjust_trade_position_v3"],
            constants,
            delegate_retry_ms=int(long_policy["entry_retry_ms"]),
        )
        short_rebuy_program = compile_rebuy_transition_ir(
            methods["short_rebuy_adjust_trade_position_v3"],
            constants,
            delegate_retry_ms=int(short_policy["entry_retry_ms"]),
        )

    basic_roots = tuple(
        dict.fromkeys(
            program
            for route in managed_exit.program["routes"]
            for program in route["decision_program_order"]
        )
    )
    short_roots = tuple(
        dict.fromkeys(
            program
            for route in managed_short_exit.program["routes"]
            for program in route["decision_program_order"]
        )
    )
    decision_roots = (
        *basic_roots,
        *short_roots,
        *((MANAGED_LONG_ADJUSTMENT_PROGRAM,) if has_adjustment else ()),
        *((MANAGED_SHORT_ADJUSTMENT_PROGRAM,) if has_adjustment else ()),
        *((LONG_REGULAR_ADJUSTMENT_PROGRAM,) if long_btc_route is not None else ()),
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
            key: record[key]
            for key in ("line", "end_line", "node_count", "input_contract")
        }

    proof_methods = dict.fromkeys(
        [
            "custom_exit",
            *(spec.method for spec in MANAGED_LONG_ROUTE_SPECS),
            *(spec.method for spec in MANAGED_SHORT_ROUTE_SPECS),
            "long_exit_stoploss", "exit_profit_target", "mark_profit_target",
            "_set_profit_target", "_remove_profit_target", "short_exit_stoploss",
        ]
    )
    method_identity = {
        name: {
            "source_sha256": source.method_records[name]["source_sha256"],
            "location": source.method_records[name]["location"],
        }
        for name in proof_methods
    }
    method_identity.update(long_grind_identity)
    method_identity.update(long_btc_identity)
    method_identity.update(short_grind_identity)
    return TradeManagerCompilation(
        managed_exit, managed_short_exit, managed_routes, managed_short_routes,
        long_grind_route, long_btc_route, short_grind_route,
        adjustment_constants, adjustment_program,
        short_adjustment_constants, short_adjustment_program,
        rebuy_constants, rebuy_program, short_rebuy_program,
        decision_report, decision_roots, programs, program_proof, method_identity,
        has_adjustment,
    )
