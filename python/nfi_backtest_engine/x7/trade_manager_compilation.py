"""Focused compiler orchestration for X7 manager route programs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import StrategyAnalysisError
from ..trade_ir import build_trade_dependency_ir
from .adjustment_dispatch import (
    active_system_flags,
    compile_adjustment_dispatch,
    prove_adjustment_inputs,
)
from .adjustment_ir import compile_system_adjustment_ir
from .adjustment_specialization import source_derisk_switches
from .adjustments import _build_adjustment_constants, _build_rebuy_adjustment_constants
from .custom_exit_prefix import compile_custom_exit_prefix
from .legacy import _build_long_btc_route, _build_long_grind_route, _build_short_grind_route
from .legacy_entry import source_legacy_entry_program
from .managed_exit_ir import ManagedExitCompilation, compile_managed_exit_ir
from .managed_short_exit_ir import (
    ManagedShortExitCompilation,
    compile_managed_short_exit_ir,
    managed_short_route_specs,
)
from .rebuy_ir import compile_rebuy_transition_ir
from .route_contracts import (
    MANAGED_LONG_PROGRAM_ORDER,
    MANAGED_LONG_ROUTE_SPECS,
)
from .routes import (
    _build_managed_long_routes,
    _build_managed_short_routes,
    _extract_rebuy_terminal_exit,
    _require_managed_long_methods,
    _require_managed_short_methods,
    _top_coins_program_order,
)
from .system_exit_ir import compile_system_exit_programs
from .trade_manager_constants import (
    LONG_REGULAR_ADJUSTMENT_PROGRAM,
    MANAGED_LONG_ADJUSTMENT_PROGRAM,
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
    adjustment_dispatch: dict[str, Any] | None
    system_exit_programs: dict[str, Any] | None
    custom_exit_prefix: dict[str, Any] | None


def compile_trade_manager(
    analysis: dict[str, Any],
    source: TradeManagerSource,
) -> TradeManagerCompilation:
    methods = source.methods
    constants = source.constants
    strategy = source.strategy
    system_exit_programs = None
    custom_exit_prefix = None
    if isinstance(constants.get("system_v4_name"), str):
        system_exit_programs = compile_system_exit_programs(methods, constants)
        custom_exit_prefix = compile_custom_exit_prefix(methods, constants)
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
            {"long_rebuy": rebuy_terminal_exit} if rebuy_terminal_exit is not None else None
        ),
    )
    short_route_specs = managed_short_route_specs(methods)
    managed_short_exit = compile_managed_short_exit_ir(
        methods,
        constants,
        short_route_specs,
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
    if system_exit_programs is not None and short_grind_route is not None:
        short_grind_route["decision_program"] = source_legacy_entry_program(
            methods["short_grind_adjust_trade_position"],
            methods,
        )
    managed_routes = _build_managed_long_routes(constants)
    if rebuy_terminal_exit is not None:
        managed_routes["long_rebuy"]["terminal_exit"] = rebuy_terminal_exit
    managed_short_routes = _build_managed_short_routes(constants, short_route_specs)

    has_adjustment = "adjust_trade_position" in source.method_records and (
        constants.get("position_adjustment_enable") is True or system_exit_programs is not None
    )
    adjustment_constants = None
    adjustment_program = None
    short_adjustment_constants = None
    short_adjustment_program = None
    rebuy_constants = None
    rebuy_program = None
    short_rebuy_program = None
    adjustment_dispatch = None
    family = "v3"
    if has_adjustment:
        if system_exit_programs is not None:
            adjustment_dispatch = compile_adjustment_dispatch(methods, constants)
            if active_system_flags(constants)["is_system_v4"]:
                family = "v4"
            for side in ("long", "short"):
                prove_adjustment_inputs(
                    methods[f"{side}_grind_adjust_trade_position_{family}"],
                    side=side,
                    family=family,
                )
        system_prefix = f"system_{family}"
        derisk_prefix = "system_v4" if family == "v4" else "system_v3_2"
        if adjustment_dispatch is not None and active_system_flags(constants)["is_system_v3"]:
            derisk_prefix = "system_v3"
        static_inputs = (
            ({"is_system_v4": True} if family == "v4" else active_system_flags(constants))
            if adjustment_dispatch is not None
            else None
        )
        local_switches = {
            side: source_derisk_switches(
                methods[f"{side}_grind_adjust_trade_position_{family}"],
                constants,
            )
            if adjustment_dispatch is not None
            else {}
            for side in ("long", "short")
        }
        adjustment_constants = _build_adjustment_constants(
            constants,
            methods[f"long_grind_adjust_trade_position_{family}"],
            side="long",
            system_prefix=system_prefix,
            derisk_prefix=derisk_prefix,
            allow_disabled=system_exit_programs is not None,
            allow_buyback=static_inputs is not None,
            source_switches=local_switches["long"],
        )
        short_adjustment_constants = _build_adjustment_constants(
            constants,
            methods[f"short_grind_adjust_trade_position_{family}"],
            side="short",
            system_prefix=system_prefix,
            derisk_prefix=derisk_prefix,
            allow_disabled=system_exit_programs is not None,
            allow_buyback=static_inputs is not None,
            source_switches=local_switches["short"],
        )
        rebuy_constants = _build_rebuy_adjustment_constants(constants, system_prefix=system_prefix)
        long_policy = adjustment_constants.get("policy")
        short_policy = short_adjustment_constants.get("policy")
        if not isinstance(long_policy, dict) or not isinstance(short_policy, dict):
            raise StrategyAnalysisError("NFI rebuy delegate policy is unavailable")
        adjustment_program = compile_system_adjustment_ir(
            methods[f"long_grind_adjust_trade_position_{family}"],
            methods[f"long_grind_exit_{family}"],
            constants,
            side="long",
            retry_policy=long_policy,
            static_inputs=static_inputs,
            constant_prefix=f"{system_prefix}_",
        )
        short_adjustment_program = compile_system_adjustment_ir(
            methods[f"short_grind_adjust_trade_position_{family}"],
            methods[f"short_grind_exit_{family}"],
            constants,
            side="short",
            retry_policy=short_policy,
            static_inputs=static_inputs,
            constant_prefix=f"{system_prefix}_",
        )
        rebuy_program = compile_rebuy_transition_ir(
            methods[f"long_rebuy_adjust_trade_position_{family}"],
            constants,
            delegate_retry_ms=int(long_policy["entry_retry_ms"]),
            corrected_minimum_method=(
                methods["correct_min_stake"] if adjustment_dispatch is not None else None
            ),
        )
        short_rebuy_program = compile_rebuy_transition_ir(
            methods[f"short_rebuy_adjust_trade_position_{family}"],
            constants,
            delegate_retry_ms=int(short_policy["entry_retry_ms"]),
            corrected_minimum_method=(
                methods["correct_min_stake"] if adjustment_dispatch is not None else None
            ),
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
        *((f"long_grind_entry_{family}",) if has_adjustment else ()),
        *((f"short_grind_entry_{family}",) if has_adjustment else ()),
        *(
            group["entry_program"]
            for program in (adjustment_program, short_adjustment_program)
            if program is not None
            for group in program["order_scan"].get("counted_entry_groups", [])
            if group.get("entry_program")
        ),
        *((LONG_REGULAR_ADJUSTMENT_PROGRAM,) if long_btc_route is not None else ()),
        *(
            (MANAGED_LONG_ADJUSTMENT_PROGRAM,)
            if system_exit_programs is not None
            and (long_grind_route is not None or long_btc_route is not None)
            else ()
        ),
        *(
            (short_grind_route["decision_program"],)
            if system_exit_programs is not None and short_grind_route is not None
            else ()
        ),
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
            key: record[key] for key in ("line", "end_line", "node_count", "input_contract")
        }

    proof_methods = dict.fromkeys(
        [
            "custom_exit",
            *(spec.method for spec in MANAGED_LONG_ROUTE_SPECS),
            *(spec.method for spec in short_route_specs),
            "long_exit_stoploss",
            "exit_profit_target",
            "mark_profit_target",
            "_set_profit_target",
            "_remove_profit_target",
            "short_exit_stoploss",
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
        managed_exit,
        managed_short_exit,
        managed_routes,
        managed_short_routes,
        long_grind_route,
        long_btc_route,
        short_grind_route,
        adjustment_constants,
        adjustment_program,
        short_adjustment_constants,
        short_adjustment_program,
        rebuy_constants,
        rebuy_program,
        short_rebuy_program,
        decision_report,
        decision_roots,
        programs,
        program_proof,
        method_identity,
        has_adjustment,
        adjustment_dispatch,
        system_exit_programs,
        custom_exit_prefix,
    )
