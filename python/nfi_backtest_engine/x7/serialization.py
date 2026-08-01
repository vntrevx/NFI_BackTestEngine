"""Serialization and schema validation for the X7 simulator contract."""

from __future__ import annotations

from typing import Any

from ..errors import StrategyAnalysisError
from ..strategy_overrides import effective_stoploss_ratio
from .contracts import _x7_leverage_contract, _x7_protection_contract


def _x7_portfolio_config(
    *,
    analysis: dict[str, Any],
    hot_ir: dict[str, Any],
    config: dict[str, Any],
    nfi_manager: dict[str, Any] | None,
    fee_rate: float,
    amount_step: float,
    price_step: float,
    pair_count: int,
    maximum_leverage_by_pair: dict[str, float],
    funding_fee_interval_ms: int | None,
    liquidation_model: dict[str, Any] | None,
) -> dict[str, Any]:
    """Serialize callbacks once for both JSON and Feather transports."""
    callbacks = {item["name"]: item for item in hot_ir["callbacks"]}
    order_operation = _operation(callbacks, "order_filled", "order-filled-state-v1")
    stake_operation = _operation(
        callbacks,
        "custom_stake_amount",
        "custom-stake-program-v1",
    )
    entry_confirmation = _operation(
        callbacks,
        "confirm_trade_entry",
        "entry-confirm-program-v1",
    )
    exit_confirmation = _operation(
        callbacks,
        "confirm_trade_exit",
        "exit-confirm-program-v1",
    )
    custom_exit = (
        None
        if callbacks.get("custom_exit", {}).get("backend") == "rust-nfi-x7-trade-manager"
        else _operation(
            callbacks,
            "custom_exit",
            "custom-exit-scalar-bundle-v1",
        )
    )
    position_adjustment = (
        None
        if callbacks.get("adjust_trade_position", {}).get("backend")
        == "rust-nfi-x7-position-adjustment"
        else _operation(
            callbacks,
            "adjust_trade_position",
            "adjust-trade-position-scalar-bundle-v1",
        )
    )
    leverage, leverage_program = _x7_leverage_contract(
        callbacks,
        trading_mode=config.get("trading_mode", "spot"),
    )
    constants = analysis["strategies"][0]["constants"]
    max_open_trades = int(config["max_open_trades"])
    if max_open_trades <= 0:
        max_open_trades = pair_count
    raw_stake = config["stake_amount"]
    unlimited = raw_stake == "unlimited"
    starting_balance = float(config["dry_run_wallet"])
    return {
        "starting_balance": starting_balance,
        "max_open_trades": max_open_trades,
        "stake_amount": starting_balance if unlimited else float(raw_stake),
        "fee_rate": fee_rate,
        "fee_open_rate": fee_rate,
        "fee_close_rate": fee_rate,
        "leverage": leverage,
        "nfi_leverage_program": leverage_program,
        "maximum_leverage_by_pair": maximum_leverage_by_pair,
        "liquidation_model": liquidation_model,
        "protection_program": _x7_protection_contract(analysis, config),
        "stoploss_ratio": effective_stoploss_ratio(constants, config),
        "amount_step": amount_step,
        "price_step": price_step,
        "custom_exit_after_ms": None,
        "adjustment_rule": None,
        "callback_program": (
            {
                "order_filled": {
                    "initial_successful_entry_writes": order_operation[
                        "initial_successful_entry_writes"
                    ],
                    "order_tag_actions": order_operation["order_tag_actions"],
                }
            }
            if order_operation is not None
            else None
        ),
        "stake_program": (
            {"statements": stake_operation["statements"]} if stake_operation is not None else None
        ),
        "amount_reserve_percent": float(config.get("amount_reserve_percent", 0.05)),
        "unlimited_stake": unlimited,
        "tradable_balance_ratio": float(config.get("tradable_balance_ratio", 0.99)),
        "entry_confirmation_program": (
            {
                "statements": entry_confirmation["statements"],
                "functions": entry_confirmation["functions"],
            }
            if entry_confirmation is not None
            else None
        ),
        "exit_confirmation_program": (
            {
                "statements": exit_confirmation["statements"],
                "functions": exit_confirmation["functions"],
            }
            if exit_confirmation is not None
            else None
        ),
        "custom_exit_program": (
            {
                "schema_version": custom_exit["schema_version"],
                "entry": custom_exit["entry"],
                "programs": custom_exit["programs"],
            }
            if custom_exit is not None
            else None
        ),
        "adjust_trade_position_program": (
            {
                "schema_version": position_adjustment["schema_version"],
                "entry": position_adjustment["entry"],
                "programs": position_adjustment["programs"],
            }
            if position_adjustment is not None
            else None
        ),
        "nfi_x7_trade_manager": nfi_manager,
        "max_entry_position_adjustment": int(constants.get("max_entry_position_adjustment", -1)),
        "is_futures": config.get("trading_mode", "spot") == "futures",
        **(
            {"funding_fee_interval_ms": funding_fee_interval_ms}
            if funding_fee_interval_ms is not None
            else {}
        ),
    }


def _operation(
    callbacks: dict[str, dict[str, Any]],
    name: str,
    opcode: str,
) -> dict[str, Any] | None:
    callback = callbacks.get(name)
    if callback is None or not callback.get("active_for_run"):
        return None
    lowering = callback.get("lowering")
    operation = lowering.get("operation") if isinstance(lowering, dict) else None
    if not isinstance(operation, dict) or operation.get("opcode") != opcode:
        raise StrategyAnalysisError(f"compiled callback operation differs for {name}")
    return operation


def _nfi_trade_manager_config(hot_ir: dict[str, Any]) -> dict[str, Any] | None:
    callbacks = hot_ir.get("callbacks")
    manager_selected = isinstance(callbacks, list) and any(
        isinstance(callback, dict)
        and callback.get("name") == "custom_exit"
        and callback.get("active_for_run")
        and callback.get("backend") == "rust-nfi-x7-trade-manager"
        for callback in callbacks
    )
    if not manager_selected:
        return None
    manager = hot_ir.get("nfi_trade_manager")
    if not isinstance(manager, dict):
        return None
    if not manager.get("executable_in_rust"):
        raise StrategyAnalysisError("NFI trade manager is not executable")
    operation = manager.get("operation")
    if not isinstance(operation, dict) or operation.get("opcode") != "nfi-x7-trade-manager-v1":
        raise StrategyAnalysisError("NFI trade manager operation is invalid")
    routes = operation.get("supported_routes")
    route_order = operation.get("route_order")
    short_routes = operation.get("supported_short_routes")
    short_route_order = operation.get("short_route_order")
    long_grind = routes.get("long_grind") if isinstance(routes, dict) else None
    long_btc = routes.get("long_btc") if isinstance(routes, dict) else None
    adjustment = operation.get("position_adjustment")
    short_adjustment = operation.get("short_position_adjustment")
    rebuy_adjustment = operation.get("rebuy_adjustment")
    short_rebuy_adjustment = operation.get("short_rebuy_adjustment")
    managed_exit_program = operation.get("managed_exit_program")
    managed_short_exit_program = operation.get("managed_short_exit_program")
    programs = operation.get("programs")
    constants = operation.get("constants")
    source_sha256 = operation.get("source_sha256")
    requires_managed_exit_program = operation.get("schema_version") in {
        "0.17.0",
        "0.18.0",
        "0.19.0",
        "0.20.0",
        "0.21.0",
        "0.22.0",
        "0.23.0",
    }
    requires_managed_short_exit_program = operation.get("schema_version") in {
        "0.20.0",
        "0.21.0",
        "0.22.0",
        "0.23.0",
    }
    requires_rebuy_program = operation.get("schema_version") in {"0.22.0", "0.23.0"}
    requires_long_adjustment_program = operation.get("schema_version") == "0.23.0"
    if (
        not isinstance(routes, dict)
        or not isinstance(route_order, list)
        or not route_order
        or not all(isinstance(name, str) and name for name in route_order)
        or not isinstance(short_routes, dict)
        or not isinstance(short_route_order, list)
        or not short_route_order
        or not all(isinstance(name, str) and name for name in short_route_order)
        or not isinstance(programs, dict)
        or not isinstance(constants, dict)
        or not isinstance(source_sha256, str)
        or (
            requires_managed_exit_program
            and (
                not isinstance(managed_exit_program, dict)
                or managed_exit_program.get("schema_version")
                != "managed-exit-program-v1"
            )
        )
        or (
            requires_managed_short_exit_program
            and (
                not isinstance(managed_short_exit_program, dict)
                or managed_short_exit_program.get("schema_version")
                != "managed-exit-program-v1"
            )
        )
        or (
            requires_rebuy_program
            and any(
                not isinstance(record, dict)
                or not isinstance(record.get("program"), dict)
                or record["program"].get("schema_version")
                != "adjustment-transition-program-v1"
                for record in (rebuy_adjustment, short_rebuy_adjustment)
            )
        )
        or (
            requires_long_adjustment_program
            and (
                not isinstance(adjustment, dict)
                or not isinstance(adjustment.get("program"), dict)
                or adjustment["program"].get("schema_version")
                != "system-adjustment-program-v1"
            )
        )
    ):
        raise StrategyAnalysisError("NFI managed-long operation is incomplete")
    managed_routes: list[dict[str, Any]] = []
    for key in route_order:
        route = routes.get(key)
        if not isinstance(route, dict):
            raise StrategyAnalysisError(f"NFI route order references missing route {key}")
        profile = route.get("profile")
        if not isinstance(profile, str):
            # Legacy grind/BTC routes are serialized in their dedicated
            # fields because their adjustment state machine is different.
            continue
        record = {
            "key": key,
            "profile": profile,
            "mode_name": route["mode_name"],
            "entry_tags": route["entry_tags"],
        }
        for name in ("stop_threshold_futures", "stop_threshold_spot", "terminal_exit"):
            if name in route:
                record[name] = route[name]
        managed_routes.append(record)
    if not managed_routes:
        raise StrategyAnalysisError("NFI operation has no managed-long route")
    managed_short_routes: list[dict[str, Any]] = []
    for key in short_route_order:
        route = short_routes.get(key)
        if not isinstance(route, dict):
            raise StrategyAnalysisError(f"NFI short route order references missing route {key}")
        required = ("profile", "mode_name", "entry_tags")
        if any(name not in route for name in required):
            raise StrategyAnalysisError(f"NFI short route {key} is incomplete")
        record = {
            "key": key,
            **{name: route[name] for name in required},
        }
        # Normal, pump, quick and high-profit delegate their stop handling to
        # the shared source-pinned callback and therefore have no route-local
        # thresholds. Rebuy, rapid and scalp carry explicit values. Preserve
        # that distinction instead of inventing placeholder thresholds.
        for name in ("stop_threshold_futures", "stop_threshold_spot"):
            if name in route:
                record[name] = route[name]
        managed_short_routes.append(record)
    constant_names = (
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
    if any(name not in constants for name in constant_names):
        raise StrategyAnalysisError("NFI top-coins constants are incomplete")

    def legacy_route_config(route: Any) -> dict[str, Any] | None:
        if not isinstance(route, dict):
            return None
        names = (
            "mode_name",
            "entry_tags",
            "exit_profit_threshold",
            "adjustment_scope",
            "grind_mode",
            "decision_program",
            "first_entry_profit_threshold_spot",
            "first_entry_stop_threshold_spot",
            "futures_fallback_loss_threshold",
            "derisk_use_grind_stops",
            "stateful_input_contract",
            "constants",
        )
        if any(name not in route for name in names):
            raise StrategyAnalysisError("NFI legacy route is incomplete")
        record = {name: route[name] for name in names}
        for name in ("regular_decision_program", "regular_constants"):
            if name in route:
                record[name] = route[name]
        return record

    return {
        "schema_version": operation["schema_version"],
        "source_sha256": source_sha256,
        "route_order": route_order,
        "managed_long_routes": managed_routes,
        "managed_exit_program": (
            managed_exit_program if isinstance(managed_exit_program, dict) else None
        ),
        "managed_short_exit_program": (
            managed_short_exit_program
            if isinstance(managed_short_exit_program, dict)
            else None
        ),
        "short_route_order": short_route_order,
        "managed_short_routes": managed_short_routes,
        "long_grind": legacy_route_config(long_grind),
        "long_btc": legacy_route_config(long_btc),
        "position_adjustment": adjustment if isinstance(adjustment, dict) else None,
        "short_position_adjustment": (
            short_adjustment if isinstance(short_adjustment, dict) else None
        ),
        "rebuy_adjustment": (rebuy_adjustment if isinstance(rebuy_adjustment, dict) else None),
        "short_rebuy_adjustment": (
            short_rebuy_adjustment if isinstance(short_rebuy_adjustment, dict) else None
        ),
        "constants": {name: constants[name] for name in constant_names},
        "programs": programs,
    }


def _required_trade_features(hot_ir: dict[str, Any]) -> list[str]:
    """Return only dataframe columns named by a compiled trade decision.

    Generic callback programs live in ``trade_dependency_ir``. NFI's
    top-coins decisions are reached through a literal function tuple, so their
    source-bound contracts live in ``nfi_trade_manager`` instead. Reading both
    locations prevents the adapter from relying on accidental call-graph
    reachability when the strategy router is refactored.
    """
    columns: set[str] = set()
    dependency_ir = hot_ir.get("trade_dependency_ir")
    if isinstance(dependency_ir, dict):
        compiled = dependency_ir.get("compiled_scalar_methods")
        if isinstance(compiled, dict):
            _collect_indexed_features(columns, compiled.values())
    manager = hot_ir.get("nfi_trade_manager")
    if isinstance(manager, dict):
        proof = manager.get("proof")
        programs = proof.get("programs") if isinstance(proof, dict) else None
        if isinstance(programs, dict):
            _collect_indexed_features(columns, programs.values())
        operation = manager.get("operation")
        routes = operation.get("supported_routes") if isinstance(operation, dict) else None
        if isinstance(routes, dict):
            _collect_indexed_features(columns, routes.values())
        if isinstance(operation, dict):
            adjustment = operation.get("position_adjustment")
            if isinstance(adjustment, dict):
                _collect_indexed_features(columns, [adjustment])
            rebuy_adjustment = operation.get("rebuy_adjustment")
            if isinstance(rebuy_adjustment, dict):
                _collect_indexed_features(columns, [rebuy_adjustment])
            short_rebuy_adjustment = operation.get("short_rebuy_adjustment")
            if isinstance(short_rebuy_adjustment, dict):
                _collect_indexed_features(columns, [short_rebuy_adjustment])
    return sorted(columns)


def _collect_indexed_features(
    columns: set[str],
    records: Any,
) -> None:
    for record in records:
        if not isinstance(record, dict):
            continue
        contract = record.get("input_contract", record.get("stateful_input_contract"))
        indexed_fields = contract.get("indexed_fields") if isinstance(contract, dict) else None
        if not isinstance(indexed_fields, dict):
            continue
        for fields in indexed_fields.values():
            if isinstance(fields, list):
                columns.update(field for field in fields if isinstance(field, str))
