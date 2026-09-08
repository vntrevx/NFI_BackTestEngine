from __future__ import annotations

import ast
import hashlib
import json
import math
from pathlib import Path

import pytest
from nfi_backtest_engine._indicator_ast import _effective_backtest_config
from nfi_backtest_engine.callback_confirm import _compile_confirm_statements
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.generic_adapter import _surface_max_open_trades
from nfi_backtest_engine.indicator_program import (
    compile_indicator_program,
    validate_indicator_program,
)
from nfi_backtest_engine.slot_settings import simulator_trade_slots, source_trade_slots
from nfi_backtest_engine.wallet_settings import wallet_policy


def test_unbounded_indicator_config_emits_an_exact_infinity_literal(tmp_path):
    source = tmp_path / "SlotIndicator.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class SlotIndicator(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        dataframe['capacity'] = self.config['max_open_trades']\n"
        "        return dataframe\n"
    )
    program = compile_indicator_program(
        source, class_name="SlotIndicator", config={"max_open_trades": -1}
    )
    validate_indicator_program(program)
    json.dumps(program, allow_nan=False)
    assert any(node["parameters"].get("special") == "+infinity" for node in program["nodes"])


def test_official_slot_probe_and_compiled_scalp_source_are_authenticated():
    evidence = Path("benchmarks/evidence/x8-slot-settings-2026-09-08")
    receipt = json.loads((evidence / "official-run.json").read_text())
    assert receipt["exit_code"] == 0
    for name, key in (
        ("official-probe.source", "script_sha256"),
        ("official-results.json", "output_sha256"),
    ):
        assert hashlib.sha256((evidence / name).read_bytes()).hexdigest() == receipt[key]
    source = Path(
        "benchmarks/fixtures/captured/x8-original-futures-20220401-20220420-r1/inputs/strategy.py"
    )
    compiled = json.loads((evidence / "scalp-programs.json").read_text())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == compiled["source_sha256"]
    cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef))
    method = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_handle_scalp_mode"
    )
    assert ast.dump(method, include_attributes=False) == compiled["method_ast"]
    for minimum, program in compiled["programs"].items():
        assert (
            _compile_confirm_statements(
                method.body, constants={"min_free_slots_scalp_mode": int(minimum)}
            )
            == program["statements"]
        )


def test_initial_entry_reserve_source_is_authenticated_and_independent_of_strategy_stoploss():
    evidence = Path("benchmarks/evidence/x8-slot-settings-2026-09-08")
    receipt = json.loads((evidence / "entry-source-run.json").read_text())
    assert receipt["exit_code"] == 0
    for name, key in (
        ("entry-source-probe.source", "script_sha256"),
        ("entry-source.json", "output_sha256"),
    ):
        assert hashlib.sha256((evidence / name).read_bytes()).hexdigest() == receipt[key]
    captured = json.loads((evidence / "entry-source.json").read_text())
    assert "-0.05 if not pos_adjust else 0.0" in captured["entry"]
    for stoploss in (-0.99, -0.1, -0.01):
        assert wallet_policy({"stoploss": stoploss})["entry_minimum_stoploss_ratio"] == -0.05


@pytest.mark.parametrize("slots", [-1, 0])
def test_nonpositive_source_slots_keep_finite_pair_capacity_without_changing_callbacks(slots):
    config = {"max_open_trades": slots, "stake_amount": 100}
    assert simulator_trade_slots(config, 2) == 2
    assert wallet_policy(config)["source_max_open_trades"] == slots
    effective = _effective_backtest_config(config)["max_open_trades"]
    assert effective == (math.inf if slots == -1 else 0)
    assert config["max_open_trades"] == slots


@pytest.mark.parametrize("slots", [1, 6, 6.0])
def test_positive_limits_retain_historical_wallet_contract(slots):
    config = {"max_open_trades": slots}
    assert simulator_trade_slots(config, 2) == int(slots)
    assert "source_max_open_trades" not in wallet_policy(config)


def test_zero_unlimited_stake_still_reaches_the_native_wallet_policy():
    assert (
        wallet_policy({"max_open_trades": 0, "stake_amount": "unlimited"})["source_max_open_trades"]
        == 0
    )
    with pytest.raises(StrategyAnalysisError, match="both be unlimited"):
        wallet_policy({"max_open_trades": -1, "stake_amount": "unlimited"})


@pytest.mark.parametrize("slots", [-1, 0, 2])
def test_pair_capacity_proof_rejects_position_stacking(slots):
    with pytest.raises(StrategyAnalysisError, match="position_stacking"):
        wallet_policy({"max_open_trades": slots, "position_stacking": True})


@pytest.mark.parametrize("slots", [False, None, -2, -0.5, 0.5, 1.5, math.nan, math.inf, 10**500])
def test_slots_without_an_exact_surface_contract_fail_closed(slots):
    with pytest.raises(StrategyAnalysisError):
        source_trade_slots({"max_open_trades": slots})


@pytest.mark.parametrize(("slots", "expected"), [(0, 0), (-1, 2), (6, 2), (1, 1)])
def test_reported_slot_limit_does_not_depend_on_observed_trades(slots, expected):
    config = {"max_open_trades": slots, "exchange": {"pair_whitelist": ["BTC/USDT", "ETH/USDT"]}}
    assert _surface_max_open_trades(config, {"maximum_concurrent_trades": 0}) == expected
