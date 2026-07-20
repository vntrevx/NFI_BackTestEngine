from __future__ import annotations

import copy
from pathlib import Path

import pandas as pd
import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.vector_manifest import EMPTY_TAG_TRANSPORT_SENTINEL
from nfi_backtest_engine.x7_adapter import (
    _optional_text,
    build_x7_simulation_input,
    build_x7_vector_manifest,
    x7_adapter_blockers,
)


def test_x7_adapter_decodes_the_empty_tag_transport_marker() -> None:
    assert _optional_text(EMPTY_TAG_TRANSPORT_SENTINEL) is None
    assert _optional_text("121") == "121"


def _analysis() -> dict:
    return {
        "strategies": [
            {
                "constants": {
                    "can_short": False,
                    "position_adjustment_enable": True,
                    "max_entry_position_adjustment": 3,
                    "stoploss": -0.99,
                },
                "strategy_callbacks": [
                    "adjust_trade_position",
                    "bot_loop_start",
                    "confirm_trade_entry",
                    "custom_exit",
                    "custom_stake_amount",
                    "order_filled",
                ],
            }
        ]
    }


def _hot_ir() -> dict:
    return {
        "hot_loop_ready": True,
        "trade_dependency_ir": {
            "compiled_scalar_methods": {
                "custom_exit": {"input_contract": {"indexed_fields": {"last_candle": ["RSI_14"]}}}
            }
        },
        "nfi_trade_manager": {
            "proof": {
                "programs": {
                    "long_exit_dec": {
                        "input_contract": {"indexed_fields": {"last_candle": ["CMF_20"]}}
                    }
                }
            }
        },
        "callbacks": [
            {
                "name": "adjust_trade_position",
                "active_for_run": True,
                "backend": "rust-adjustment-vm",
                "lowering": {
                    "operation": {
                        "opcode": "adjust-trade-position-scalar-bundle-v1",
                        "schema_version": "1.0.0",
                        "entry": "adjust_trade_position",
                        "programs": {
                            "adjust_trade_position": {
                                "schema_version": "1.1.0",
                                "opcode": "scalar-decision-program-v1",
                                "parameters": [],
                                "expressions": [["literal", None]],
                                "statements": [["return", 0]],
                            }
                        },
                    }
                },
            },
            {
                "name": "bot_loop_start",
                "active_for_run": True,
                "backend": "rust-noop",
                "lowering": {"operation": {"opcode": "noop"}},
            },
            {
                "name": "confirm_trade_entry",
                "active_for_run": True,
                "backend": "rust-entry-confirm-vm",
                "lowering": {
                    "operation": {
                        "opcode": "entry-confirm-program-v1",
                        "statements": [
                            {
                                "op": "return",
                                "value": {"op": "literal", "value": True},
                            }
                        ],
                        "functions": {},
                    }
                },
            },
            {
                "name": "custom_exit",
                "active_for_run": True,
                "backend": "rust-custom-exit-vm",
                "lowering": {
                    "operation": {
                        "opcode": "custom-exit-scalar-bundle-v1",
                        "schema_version": "1.0.0",
                        "entry": "custom_exit",
                        "programs": {
                            "custom_exit": {
                                "schema_version": "1.1.0",
                                "opcode": "scalar-decision-program-v1",
                                "parameters": [],
                                "expressions": [["literal", None]],
                                "statements": [["return", 0]],
                            }
                        },
                    }
                },
            },
            {
                "name": "custom_stake_amount",
                "active_for_run": True,
                "backend": "rust-stake-vm",
                "lowering": {
                    "operation": {
                        "opcode": "custom-stake-program-v1",
                        "statements": [
                            {
                                "op": "return",
                                "value": {
                                    "op": "variable",
                                    "name": "proposed_stake",
                                },
                            }
                        ],
                    }
                },
            },
            {
                "name": "order_filled",
                "active_for_run": True,
                "backend": "rust-order-state",
                "lowering": {
                    "operation": {
                        "opcode": "order-filled-state-v1",
                        "initial_successful_entry_writes": [
                            {"key": "system_version", "value": "system_v3_2"}
                        ],
                        "order_tag_actions": {},
                    }
                },
            },
        ],
    }


def _config() -> dict:
    return {
        "exchange": {"name": "binance", "pair_whitelist": ["BTC/USDT"]},
        "trading_mode": "spot",
        "margin_mode": "isolated",
        "dry_run_wallet": 1000,
        "max_open_trades": 2,
        "stake_amount": "unlimited",
        "tradable_balance_ratio": 0.99,
    }


def _legacy_grind_constants() -> dict:
    tags = (
        ("gd1", "dd1"),
        ("gd2", "dd2"),
        ("gd3", "dd3"),
        ("gd4", "dd4"),
        ("gd5", "dd5"),
        ("gd6", "dd6"),
        ("dl1", "ddl1"),
        ("dl2", "ddl2"),
    )
    return {
        "max_stake_multiplier": 1.0,
        "stake_multipliers_futures": [0.2, 0.3, 0.4, 0.5],
        "stake_multipliers_spot": [0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        "derisk_1_reentry_futures": -0.08,
        "derisk_1_reentry_spot": -0.08,
        "clusters": [
            {
                "entry_tag": entry_tag,
                "stop_tag": stop_tag,
                "stakes_futures": [0.2, 0.24, 0.28],
                "stakes_spot": [0.2, 0.24, 0.28],
                "thresholds_futures": [-0.12, -0.16, -0.2],
                "thresholds_spot": [-0.12, -0.16, -0.2],
                "stop_threshold_futures": -0.06,
                "stop_threshold_spot": -0.06,
                "profit_threshold_futures": 0.018,
                "profit_threshold_spot": 0.018,
            }
            for entry_tag, stop_tag in tags
        ],
    }


def _regular_adjustment_constants() -> dict:
    return {
        "use_grind_stops": True,
        "derisk_enable": True,
        "rebuy_stakes_spot": [0.2, 0.25],
        "rebuy_thresholds_spot": [-0.08, -0.12],
        "derisk_threshold_spot": -0.6,
        "derisk_level_1_threshold_spot": -0.4,
        "grinds": [
            {
                "entry_tag": f"g{level}",
                "stop_tag": f"sg{level}",
                "stakes_spot": [0.2, 0.25],
                "thresholds_spot": [-0.08, -0.12],
                "stop_threshold_spot": -0.2,
                "profit_threshold_spot": 0.018,
            }
            for level in range(1, 7)
        ],
        "policy": {
            "entry_retry_ms": 600_000,
            "grind_force_order_age_ms": 7_200_000,
            "grind_order_age_ms": 21_600_000,
            "rebuy_order_age_ms": 43_200_000,
            "grind_entry_profit_gate": -0.02,
            "additional_grind_profit_gate": -0.03,
            "forced_age_profit_gate": -0.06,
            "minimum_entry_multiplier": 1.5,
            "minimum_remaining_multiplier": 1.55,
        },
    }


def _nfi_manager_hot_ir() -> dict:
    hot_ir = copy.deepcopy(_hot_ir())
    custom_exit = next(
        callback for callback in hot_ir["callbacks"] if callback["name"] == "custom_exit"
    )
    scalar = custom_exit["lowering"]["operation"]["programs"]["custom_exit"]
    order = [
        "long_exit_signals",
        "long_exit_main",
        "long_exit_williams_r",
        "long_exit_dec",
    ]
    route_specs = (
        ("long_normal", "normal", "long_normal", ["1"]),
        ("long_pump", "pump", "long_pump", ["21"]),
        ("long_quick", "quick", "long_quick", ["41"]),
        (
            "long_rebuy",
            "rebuy",
            "long_rebuy",
            ["61", "62", "63", "64", "65"],
        ),
        ("long_high_profit", "high-profit", "long_hp", ["81"]),
        ("long_rapid", "rapid", "long_rapid", ["101"]),
        (
            "long_top_coins",
            "top-coins",
            "long_tc",
            ["141", "142", "143", "144", "145"],
        ),
        ("long_scalp", "scalp", "long_scalp", ["161"]),
    )
    supported_routes = {}
    for key, profile, mode_name, tags in route_specs:
        route = {
            "profile": profile,
            "mode_name": mode_name,
            "entry_tags": tags,
            "decision_program_order": (order[:3] if profile == "high-profit" else order),
            "stateful_input_contract": {"indexed_fields": {}},
        }
        if profile in {"rebuy", "rapid", "scalp"}:
            route["stop_threshold_futures"] = 0.35
            route["stop_threshold_spot"] = 0.12
        supported_routes[key] = route
    operation = {
        "opcode": "nfi-x7-trade-manager-v1",
        "schema_version": "0.9.0",
        "source_sha256": "a" * 64,
        "route_order": [spec[0] for spec in route_specs],
        "supported_routes": supported_routes,
        "short_route_order": ["short_rebuy"],
        "supported_short_routes": {
            "short_rebuy": {
                "profile": "rebuy",
                "mode_name": "short_rebuy",
                "entry_tags": ["561", "562", "563"],
                "stop_threshold_futures": 1.4,
                "stop_threshold_spot": 0.48,
            }
        },
        "constants": {
            "derisk_enable": True,
            "stops_enable": True,
            "stop_threshold_futures": 0.1,
            "stop_threshold_spot": 0.1,
            "system_name_use": "system_v3_2",
            "system_v3_2_name": "system_v3_2",
            "system_v3_2_stop_threshold_doom_futures": 0.35,
            "system_v3_2_stop_threshold_doom_spot": 0.12,
            "system_v3_2_stops_enable": False,
            "u_e_stops_enable": False,
        },
        "programs": {
            name: copy.deepcopy(scalar)
            for name in [
                *order,
                "short_exit_signals",
                "short_exit_main",
                "short_exit_williams_r",
                "short_exit_dec",
            ]
        },
        "rebuy_adjustment": {
            "enabled": True,
            "entry_tags": ["61", "62", "63", "64", "65"],
            "system_version": "system_v3_2",
            "stateful_input_contract": {"indexed_fields": {}},
            "constants": {
                "derisk_enable": True,
                "stakes_futures": [1.0, 1.0, 1.0, 1.0],
                "stakes_spot": [1.0, 1.0, 1.0, 1.0],
                "thresholds_futures": [-0.08, -0.12, -0.16, -0.20],
                "thresholds_spot": [-0.08, -0.12, -0.16, -0.20],
                "derisk_futures": -1.4,
                "derisk_spot": -0.48,
            },
        },
    }
    operation["short_rebuy_adjustment"] = {
        **copy.deepcopy(operation["rebuy_adjustment"]),
        "entry_tags": ["561", "562", "563"],
        "execution_scope": "pre-derisk-only-v1",
        "post_derisk_action": "fail-simulation",
    }
    manager = {
        "backend": "rust-nfi-x7-trade-manager",
        "executable_in_rust": True,
        "operation": operation,
        "proof": {"programs": {}},
    }
    hot_ir["nfi_trade_manager"] = manager
    custom_exit["backend"] = manager["backend"]
    custom_exit["executable_in_rust"] = True
    custom_exit["lowering"] = manager
    return hot_ir


def _markets(path: Path, *, include_limits: bool = True) -> None:
    market = {
        "precision": {"amount": 0.00001, "price": 0.01},
        "taker": 0.001,
    }
    if include_limits:
        market["limits"] = {
            "amount": {"min": 0.0001, "max": None},
            "cost": {"min": 5.0, "max": None},
            "leverage": {"min": 1.0, "max": 5.0},
        }
        market["leverage_tiers"] = [
            {
                "min_notional": 0.0,
                "max_notional": 1000.0,
                "maximum_leverage": 5.0,
                "maintenance_margin_rate": 0.005,
                "maintenance_amount": 0.0,
            },
            {
                "min_notional": 1000.0,
                "max_notional": 10_000.0,
                "maximum_leverage": 3.0,
                "maintenance_margin_rate": 0.01,
                "maintenance_amount": 5.0,
            },
        ]
    write_json(
        path,
        {
            "schema_version": "1.2.0",
            "exchange": "binance",
            "markets": {"BTC/USDT": market},
        },
    )


def _with_leverage(hot_ir: dict, *, values: tuple[float, float, float]) -> dict:
    result = copy.deepcopy(hot_ir)
    default, rebuy, grind = values
    result["callbacks"].append(
        {
            "name": "leverage",
            "active_for_run": True,
            "backend": "rust-nfi-x7-leverage",
            "lowering": {
                "operation": {
                    "opcode": "nfi-x7-leverage-v1",
                    "default": default,
                    "ordered_tag_overrides": [
                        {"entry_tags": ["61"], "leverage": rebuy},
                        {"entry_tags": ["120"], "leverage": grind},
                    ],
                }
            },
        }
    )
    return result


def test_x7_adapter_requires_frozen_amount_and_cost_limits(tmp_path: Path) -> None:
    markets = tmp_path / "markets.json"
    _markets(markets, include_limits=False)

    blockers = x7_adapter_blockers(
        _analysis(),
        _hot_ir(),
        _config(),
        market_metadata_path=markets,
    )

    assert [item["code"] for item in blockers] == ["MARKET_LIMITS_REQUIRED"]


def test_x7_futures_preflight_accepts_compiled_tag_dependent_leverage(
    tmp_path: Path,
) -> None:
    markets = tmp_path / "markets.json"
    _markets(markets)
    config = {**_config(), "trading_mode": "futures"}

    missing = x7_adapter_blockers(
        _analysis(),
        _hot_ir(),
        config,
        market_metadata_path=markets,
    )
    non_uniform = x7_adapter_blockers(
        _analysis(),
        _with_leverage(_hot_ir(), values=(3.0, 2.0, 3.0)),
        config,
        market_metadata_path=markets,
    )
    uniform = x7_adapter_blockers(
        _analysis(),
        _with_leverage(_hot_ir(), values=(3.0, 3.0, 3.0)),
        config,
        market_metadata_path=markets,
    )

    assert [item["code"] for item in missing] == [
        "X7_FUTURES_LEVERAGE_REQUIRED",
    ]
    assert non_uniform == []
    assert uniform == []


def test_x7_futures_serializes_ordered_leverage_program(tmp_path: Path) -> None:
    vector = tmp_path / "BTC_USDT.feather"
    pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1.0, 1.0],
            "CMF_20": [0.1, 0.2],
            "RSI_14": [49.0, 50.0],
            "nfi_exec_enter_long": [0, 1],
            "nfi_exec_exit_long": [0, 0],
            "nfi_exec_enter_short": [0, 0],
            "nfi_exec_exit_short": [0, 0],
            "nfi_exec_enter_tag": [None, "61"],
            "nfi_exec_funding_rate": [0.0, 0.0],
            "nfi_exec_funding_mark_price": [100.5, 101.5],
        }
    ).to_feather(vector)
    markets = tmp_path / "markets.json"
    _markets(markets)

    document = build_x7_simulation_input(
        analysis=_analysis(),
        hot_ir=_with_leverage(_hot_ir(), values=(4.0, 2.0, 3.0)),
        config={**_config(), "trading_mode": "futures"},
        vector_report={"outputs": [{"pair": "BTC/USDT", "path": str(vector)}]},
        market_metadata_path=markets,
        destination=tmp_path / "simulation.json",
    )

    assert document["config"]["leverage"] == 4.0
    assert document["config"]["nfi_leverage_program"] == {
        "default": 4.0,
        "ordered_tag_overrides": [
            {"entry_tags": ["61"], "leverage": 2.0},
            {"entry_tags": ["120"], "leverage": 3.0},
        ],
    }
    assert document["config"]["maximum_leverage_by_pair"] == {"BTC/USDT": 5.0}
    assert document["config"]["liquidation_model"] == {
        "exchange": "binance",
        "margin_mode": "isolated",
        "buffer": 0.05,
        "tiers_by_pair": {
            "BTC/USDT": [
                {
                    "min_notional": 0.0,
                    "max_notional": 1000.0,
                    "maximum_leverage": 5.0,
                    "maintenance_margin_rate": 0.005,
                    "maintenance_amount": 0.0,
                },
                {
                    "min_notional": 1000.0,
                    "max_notional": 10_000.0,
                    "maximum_leverage": 3.0,
                    "maintenance_margin_rate": 0.01,
                    "maintenance_amount": 5.0,
                },
            ]
        },
    }


def test_x7_adapter_serializes_compiled_programs_and_unlimited_stake(
    tmp_path: Path,
) -> None:
    vector = tmp_path / "BTC_USDT.feather"
    pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1.0, 1.0],
            "CMF_20": [0.1, 0.2],
            "RSI_14": [49.0, float("nan")],
            "nfi_exec_enter_long": [0, 1],
            "nfi_exec_exit_long": [0, 0],
            "nfi_exec_enter_tag": [None, "61"],
        }
    ).to_feather(vector)
    markets = tmp_path / "markets.json"
    _markets(markets)
    analysis = _analysis()
    analysis["strategies"][0].update(
        {
            "protections_static": True,
            "protections": [
                {
                    "method": "CooldownPeriod",
                    "lookback_period_candles": 4,
                    "stop_duration_candles": 2,
                },
                {
                    "method": "StoplossGuard",
                    "lookback_period": 60,
                    "stop_duration": 30,
                    "trade_limit": 2,
                    "only_per_pair": False,
                    "only_per_side": True,
                    "required_profit": -0.02,
                },
                {
                    "method": "MaxDrawdown",
                    "lookback_period_candles": 12,
                    "unlock_at": "04:30",
                    "trade_limit": 3,
                    "max_allowed_drawdown": 0.2,
                    "calculation_mode": "equity",
                },
                {
                    "method": "LowProfitPairs",
                    "lookback_period": 90,
                    "stop_duration_candles": 3,
                    "trade_limit": 2,
                    "only_per_side": False,
                    "required_profit": -0.01,
                },
            ],
        }
    )
    config = {
        **_config(),
        "enable_protections": True,
        "timeframe": "5m",
        "stoploss": -0.001,
    }

    document = build_x7_simulation_input(
        analysis=analysis,
        hot_ir=_hot_ir(),
        config=config,
        vector_report={"outputs": [{"pair": "BTC/USDT", "path": str(vector)}]},
        market_metadata_path=markets,
        destination=tmp_path / "simulation.json",
    )

    assert document["config"]["unlimited_stake"] is True
    assert document["config"]["stoploss_ratio"] == -0.001
    assert document["config"]["stake_program"]["statements"][0]["op"] == "return"
    assert document["config"]["entry_confirmation_program"]["statements"][0]["op"] == "return"
    assert document["config"]["custom_exit_program"]["entry"] == "custom_exit"
    assert document["config"]["adjust_trade_position_program"]["entry"] == "adjust_trade_position"
    assert document["config"]["max_entry_position_adjustment"] == 3
    protection_program = document["config"]["protection_program"]
    assert protection_program["timeframe_ms"] == 300_000
    assert [item["method"] for item in protection_program["handlers"]] == [
        "CooldownPeriod",
        "StoplossGuard",
        "MaxDrawdown",
        "LowProfitPairs",
    ]
    assert protection_program["handlers"][0]["timing"] == {
        "lookback_ms": 1_200_000,
        "lookback_text": "4 candles",
        "duration_ms": 600_000,
        "unlock_at_minute_utc": None,
        "lock_text": "for 2 candles",
    }
    assert protection_program["handlers"][2]["timing"]["unlock_at_minute_utc"] == 270
    assert protection_program["handlers"][2]["timing"]["lock_text"] == "until 04:30"
    assert (
        document["config"]["callback_program"]["order_filled"]["initial_successful_entry_writes"][
            0
        ]["value"]
        == "system_v3_2"
    )
    assert document["pairs"][0]["minimum_cost"] == 5.0
    assert document["pairs"][0]["feature_columns"] == {
        "CMF_20": [0.1, 0.2],
        "RSI_14": [49.0, {"$float": "nan"}],
    }
    assert "RSI_14" not in document["pairs"][0]["candles"][0]
    assert document["pairs"][0]["candles"][1]["previous_close"] == 100.5


def test_x7_manifest_keeps_features_in_the_sealed_feather_file(tmp_path: Path) -> None:
    vector = tmp_path / "BTC_USDT.feather"
    pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1.0, 1.0],
            "CMF_20": [0.1, 0.2],
            "RSI_14": [49.0, float("nan")],
            "nfi_exec_enter_long": [0, 1],
            "nfi_exec_exit_long": [0, 0],
            "nfi_exec_enter_tag": [None, "61"],
        }
    ).to_feather(vector)
    markets = tmp_path / "markets.json"
    _markets(markets)

    document = build_x7_vector_manifest(
        analysis=_analysis(),
        hot_ir=_hot_ir(),
        config=_config(),
        vector_report={
            "outputs": [
                {
                    "pair": "BTC/USDT",
                    "path": str(vector),
                    "sha256": sha256_file(vector),
                    "execution_start_index": 1,
                }
            ]
        },
        market_metadata_path=markets,
        destination=tmp_path / "simulation-input.manifest.json",
    )

    pair = document["pairs"][0]
    assert pair["feature_columns"] == ["CMF_20", "RSI_14"]
    assert pair["execution_start_index"] == 1
    assert pair["vector"]["path"] == "BTC_USDT.feather"
    assert pair["vector"]["rows"] == 2
    assert "candles" not in pair
    assert "feature_columns" not in pair["vector"]


def test_x7_adapter_serializes_scope_limited_nfi_trade_manager(
    tmp_path: Path,
) -> None:
    vector = tmp_path / "BTC_USDT.feather"
    pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1.0, 1.0],
            "CMF_20": [0.1, 0.2],
            "RSI_14": [49.0, 50.0],
            "nfi_exec_enter_long": [0, 1],
            "nfi_exec_exit_long": [0, 0],
            "nfi_exec_enter_tag": [None, "141 142"],
        }
    ).to_feather(vector)
    markets = tmp_path / "markets.json"
    _markets(markets)

    document = build_x7_simulation_input(
        analysis=_analysis(),
        hot_ir=_nfi_manager_hot_ir(),
        config=_config(),
        vector_report={"outputs": [{"pair": "BTC/USDT", "path": str(vector)}]},
        market_metadata_path=markets,
        destination=tmp_path / "nfi-simulation.json",
    )

    manager = document["config"]["nfi_x7_trade_manager"]
    assert manager["source_sha256"] == "a" * 64
    top_coins = next(
        route for route in manager["managed_long_routes"] if route["key"] == "long_top_coins"
    )
    assert top_coins["entry_tags"] == [
        "141",
        "142",
        "143",
        "144",
        "145",
    ]
    assert manager["rebuy_adjustment"]["entry_tags"] == [
        "61",
        "62",
        "63",
        "64",
        "65",
    ]
    assert document["config"]["custom_exit_program"] is None


def test_x7_adapter_serializes_the_declared_long_grind_route(
    tmp_path: Path,
) -> None:
    vector = tmp_path / "BTC_USDT.feather"
    pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 102.0],
            "high": [101.0, 103.0],
            "low": [99.0, 101.0],
            "close": [100.5, 102.5],
            "volume": [1.0, 1.0],
            "CMF_20": [0.1, 0.2],
            "RSI_14": [49.0, 50.0],
            "nfi_exec_enter_long": [1, 0],
            "nfi_exec_exit_long": [0, 0],
            "nfi_exec_enter_tag": ["120 ", None],
        }
    ).to_feather(vector)
    markets = tmp_path / "markets.json"
    _markets(markets)
    hot_ir = _nfi_manager_hot_ir()
    hot_ir["nfi_trade_manager"]["operation"]["supported_routes"]["long_grind"] = {
        "mode_name": "long_grind",
        "entry_tags": ["120"],
        "exit_profit_threshold": 0.25,
        "adjustment_scope": "spot-grind-backtest-v1",
        "grind_mode": True,
        "decision_program": "long_grind_entry_v3",
        "first_entry_profit_threshold_spot": 0.018,
        "first_entry_stop_threshold_spot": -0.2,
        "derisk_use_grind_stops": True,
        "stateful_input_contract": {"indexed_fields": {}},
        "constants": _legacy_grind_constants(),
    }
    hot_ir["nfi_trade_manager"]["operation"]["route_order"].insert(6, "long_grind")

    document = build_x7_simulation_input(
        analysis=_analysis(),
        hot_ir=hot_ir,
        config=_config(),
        vector_report={"outputs": [{"pair": "BTC/USDT", "path": str(vector)}]},
        market_metadata_path=markets,
        destination=tmp_path / "nfi-long-grind-simulation.json",
    )

    route = document["config"]["nfi_x7_trade_manager"]["long_grind"]
    assert route["entry_tags"] == ["120"]
    assert route["adjustment_scope"] == "spot-grind-backtest-v1"
    assert [cluster["entry_tag"] for cluster in route["constants"]["clusters"]] == [
        "gd1",
        "gd2",
        "gd3",
        "gd4",
        "gd5",
        "gd6",
        "dl1",
        "dl2",
    ]


def test_x7_adapter_serializes_the_source_bound_long_btc_adjustment_route(
    tmp_path: Path,
) -> None:
    vector = tmp_path / "BTC_USDT.feather"
    pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 102.0],
            "high": [101.0, 103.0],
            "low": [99.0, 101.0],
            "close": [100.5, 102.5],
            "volume": [1.0, 1.0],
            "CMF_20": [0.1, 0.2],
            "RSI_14": [49.0, 50.0],
            "nfi_exec_enter_long": [1, 0],
            "nfi_exec_exit_long": [0, 0],
            "nfi_exec_enter_tag": ["121", None],
        }
    ).to_feather(vector)
    markets = tmp_path / "markets.json"
    _markets(markets)
    hot_ir = _nfi_manager_hot_ir()
    hot_ir["nfi_trade_manager"]["operation"]["supported_routes"]["long_btc"] = {
        "mode_name": "long_btc",
        "entry_tags": ["121"],
        "exit_profit_threshold": 0.25,
        "adjustment_scope": "spot-regular-backtest-v1",
        "grind_mode": False,
        "decision_program": "long_grind_entry_v3",
        "regular_decision_program": "long_grind_entry",
        "first_entry_profit_threshold_spot": 0.018,
        "first_entry_stop_threshold_spot": -0.2,
        "derisk_use_grind_stops": True,
        "stateful_input_contract": {"indexed_fields": {}},
        "constants": _legacy_grind_constants(),
        "regular_constants": _regular_adjustment_constants(),
    }
    hot_ir["nfi_trade_manager"]["operation"]["route_order"].insert(6, "long_btc")
    programs = hot_ir["nfi_trade_manager"]["operation"]["programs"]
    programs["long_grind_entry"] = copy.deepcopy(programs["long_exit_signals"])

    document = build_x7_simulation_input(
        analysis=_analysis(),
        hot_ir=hot_ir,
        config=_config(),
        vector_report={"outputs": [{"pair": "BTC/USDT", "path": str(vector)}]},
        market_metadata_path=markets,
        destination=tmp_path / "nfi-long-btc-simulation.json",
    )

    route = document["config"]["nfi_x7_trade_manager"]["long_btc"]
    assert route["adjustment_scope"] == "spot-regular-backtest-v1"
    assert route["regular_decision_program"] == "long_grind_entry"
    assert [grind["entry_tag"] for grind in route["regular_constants"]["grinds"]] == [
        "g1",
        "g2",
        "g3",
        "g4",
        "g5",
        "g6",
    ]


def test_x7_adapter_rejects_an_nfi_entry_tag_outside_compiled_scope(
    tmp_path: Path,
) -> None:
    vector = tmp_path / "BTC_USDT.feather"
    pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=1, freq="5min", tz="UTC"),
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [1.0],
            "CMF_20": [0.1],
            "RSI_14": [49.0],
            "nfi_exec_enter_long": [1],
            "nfi_exec_exit_long": [0],
            "nfi_exec_enter_tag": ["120"],
        }
    ).to_feather(vector)
    markets = tmp_path / "markets.json"
    _markets(markets)

    with pytest.raises(StrategyAnalysisError, match="entry tag '120'"):
        build_x7_simulation_input(
            analysis=_analysis(),
            hot_ir=_nfi_manager_hot_ir(),
            config=_config(),
            vector_report={"outputs": [{"pair": "BTC/USDT", "path": str(vector)}]},
            market_metadata_path=markets,
            destination=tmp_path / "rejected.json",
        )


def test_x7_adapter_rejects_a_mixed_rebuy_and_unknown_tag(
    tmp_path: Path,
) -> None:
    vector = tmp_path / "BTC_USDT.feather"
    pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=1, freq="5min", tz="UTC"),
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [1.0],
            "CMF_20": [0.1],
            "RSI_14": [49.0],
            "nfi_exec_enter_long": [1],
            "nfi_exec_exit_long": [0],
            # The rebuy word is compiled, but exact mode rejects a signal as
            # soon as any companion word has no reviewed route.
            "nfi_exec_enter_tag": ["61 999"],
        }
    ).to_feather(vector)
    markets = tmp_path / "markets.json"
    _markets(markets)

    with pytest.raises(StrategyAnalysisError, match="entry tag '61 999'"):
        build_x7_simulation_input(
            analysis=_analysis(),
            hot_ir=_nfi_manager_hot_ir(),
            config=_config(),
            vector_report={"outputs": [{"pair": "BTC/USDT", "path": str(vector)}]},
            market_metadata_path=markets,
            destination=tmp_path / "mixed-route.json",
        )
