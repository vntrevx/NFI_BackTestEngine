from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.hot_ir import build_hot_callback_ir
from nfi_backtest_engine.strategy_ir import analyze_strategy


def test_pure_custom_exit_lowers_to_a_transitive_rust_bundle(tmp_path: Path) -> None:
    source = tmp_path / "Typed.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Typed(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        return 'done' if current_profit > 0.1 else None\n",
        encoding="utf-8",
    )

    analysis = analyze_strategy(source, class_name="Typed")
    result = build_hot_callback_ir(analysis)

    assert result["hot_loop_ready"]
    assert result["execution_policy"]["python_per_candle"] is False
    assert result["callbacks"][0]["returns"] == "exit_reason|null"
    assert result["callbacks"][0]["executable_in_rust"] is True
    assert result["callbacks"][0]["backend"] == "rust-custom-exit-vm"
    assert result["callbacks"][0]["lowering"]["operation"]["entry"] == "custom_exit"
    assert result["blockers"] == []
    dependency_ir = result["trade_dependency_ir"]
    scalar = dependency_ir["compiled_scalar_methods"]["custom_exit"]
    assert scalar["expression_count"] > 0
    assert len(scalar["program_sha256"]) == 64
    assert "program" not in scalar


def test_pure_position_adjustment_lowers_to_a_rust_bundle(tmp_path: Path) -> None:
    source = tmp_path / "Adjustment.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Adjustment(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    position_adjustment_enable = True\n"
        "    def adjust_trade_position(self, trade, current_time, current_rate, "
        "current_profit, min_stake, max_stake, current_entry_rate, "
        "current_exit_rate, current_entry_profit, current_exit_profit, **kwargs):\n"
        "        if current_profit < -0.1:\n"
        "            return 50.0, 'rebuy'\n"
        "        return None\n",
        encoding="utf-8",
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="Adjustment"),
        run_mode="backtest",
    )

    callback = result["callbacks"][0]
    assert result["hot_loop_ready"]
    assert callback["backend"] == "rust-adjustment-vm"
    assert callback["lowering"]["operation"]["opcode"] == "adjust-trade-position-scalar-bundle-v1"


def test_incomplete_x7_router_fails_closed_instead_of_widening_scope(
    tmp_path: Path,
) -> None:
    source = tmp_path / "NostalgiaForInfinityX7Tiny.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class NostalgiaForInfinityX7Tiny(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    long_top_coins_mode_name = 'long_tc'\n"
        "    long_top_coins_mode_tags = ['141', '142']\n"
        "    derisk_enable = True\n"
        "    stops_enable = True\n"
        "    stop_threshold_futures = 0.1\n"
        "    stop_threshold_spot = 0.1\n"
        "    system_name_use = 'system_v3_2'\n"
        "    system_v3_2_name = 'system_v3_2'\n"
        "    system_v3_2_stop_threshold_doom_futures = 0.35\n"
        "    system_v3_2_stop_threshold_doom_spot = 0.12\n"
        "    system_v3_2_stops_enable = False\n"
        "    u_e_stops_enable = False\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        return self.long_exit_top_coins(current_profit)\n"
        "    def long_exit_top_coins(self, current_profit):\n"
        "        for exit_func in (\n"
        "            self.long_exit_signals,\n"
        "            self.long_exit_main,\n"
        "            self.long_exit_williams_r,\n"
        "            self.long_exit_dec,\n"
        "        ):\n"
        "            sell, reason = exit_func(current_profit)\n"
        "            if sell:\n"
        "                break\n"
        "        self.long_exit_stoploss()\n"
        "        self.exit_profit_target()\n"
        "        self.mark_profit_target()\n"
        "        self._set_profit_target()\n"
        "        self._remove_profit_target()\n"
        "        return sell, reason\n"
        "    def long_exit_signals(self, profit):\n"
        "        return profit > 0.01, 'signals'\n"
        "    def long_exit_main(self, profit):\n"
        "        return profit > 0.02, 'main'\n"
        "    def long_exit_williams_r(self, profit):\n"
        "        return profit > 0.03, 'williams'\n"
        "    def long_exit_dec(self, profit):\n"
        "        return profit > 0.04, 'dec'\n"
        "    def long_exit_stoploss(self):\n"
        "        self.mutable = True\n"
        "    def exit_profit_target(self):\n"
        "        self.mutable = True\n"
        "    def mark_profit_target(self):\n"
        "        self.mutable = True\n"
        "    def _set_profit_target(self):\n"
        "        self.mutable = True\n"
        "    def _remove_profit_target(self):\n"
        "        self.mutable = True\n",
        encoding="utf-8",
    )
    # Force a Windows-checkout byte layout so the NFI-specific identity check
    # is exercised on Linux and macOS runners too.
    source.write_bytes(
        source.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8")
    )

    with pytest.raises(
        StrategyAnalysisError,
        match="managed-long state machine is missing",
    ):
        build_hot_callback_ir(
            analyze_strategy(source, class_name="NostalgiaForInfinityX7Tiny"),
            run_mode="backtest",
        )


def test_event_callbacks_are_inventoried_and_spot_leverage_is_inactive(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Events.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Events(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def order_filled(self, pair, trade, order, current_time, **kwargs):\n"
        "        trade.set_custom_data(key='filled', value=True)\n"
        "    def leverage(self, pair, current_time, current_rate, proposed_leverage, "
        "max_leverage, entry_tag, side, **kwargs):\n"
        "        return 2.0\n",
        encoding="utf-8",
    )

    analysis = analyze_strategy(source, class_name="Events")
    result = build_hot_callback_ir(analysis, trading_mode="spot")

    callbacks = {item["name"]: item for item in result["callbacks"]}
    assert callbacks["order_filled"]["kind"] == "order-event"
    assert callbacks["order_filled"]["active_for_run"]
    assert not callbacks["leverage"]["active_for_run"]
    assert [item["callback"] for item in result["blockers"]] == ["order_filled"]


def test_x7_tag_leverage_is_frozen_for_futures_without_python_execution(
    tmp_path: Path,
) -> None:
    source = tmp_path / "NostalgiaForInfinityX7Leverage.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class NostalgiaForInfinityX7Leverage(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    long_rebuy_mode_tags = ['61', '62']\n"
        "    long_grind_mode_tags = ['120']\n"
        "    futures_mode_leverage = 3.0\n"
        "    futures_mode_leverage_rebuy_mode = 2.0\n"
        "    futures_mode_leverage_grind_mode = 1.5\n"
        "    def leverage(self, pair, current_time, current_rate, proposed_leverage, "
        "max_leverage, entry_tag, side, **kwargs):\n"
        "        enter_tags = entry_tag.split()\n"
        "        long_rebuy_mode_tags = self.long_rebuy_mode_tags\n"
        "        long_grind_mode_tags = self.long_grind_mode_tags\n"
        "        if all(c in long_rebuy_mode_tags for c in enter_tags):\n"
        "            return self.futures_mode_leverage_rebuy_mode\n"
        "        elif all(c in long_grind_mode_tags for c in enter_tags):\n"
        "            return self.futures_mode_leverage_grind_mode\n"
        "        return self.futures_mode_leverage\n",
        encoding="utf-8",
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="NostalgiaForInfinityX7Leverage"),
        trading_mode="futures",
        run_mode="backtest",
        config={
            "futures_mode_leverage": 4.0,
            "futures_mode_leverage_rebuy_mode": 2.5,
        },
    )

    callback = result["callbacks"][0]
    operation = callback["lowering"]["operation"]
    assert result["hot_loop_ready"]
    assert callback["backend"] == "rust-nfi-x7-leverage"
    assert operation == {
        "opcode": "nfi-x7-leverage-v1",
        "default": 4.0,
        "ordered_tag_overrides": [
            {"entry_tags": ["61", "62"], "leverage": 2.5},
            {"entry_tags": ["120"], "leverage": 1.5},
        ],
    }


def test_x7_leverage_near_miss_with_extra_side_branch_fails_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "NostalgiaForInfinityX7Leverage.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class NostalgiaForInfinityX7Leverage(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    long_rebuy_mode_tags = ['61']\n"
        "    long_grind_mode_tags = ['120']\n"
        "    futures_mode_leverage = 3.0\n"
        "    futures_mode_leverage_rebuy_mode = 3.0\n"
        "    futures_mode_leverage_grind_mode = 3.0\n"
        "    def leverage(self, pair, current_time, current_rate, proposed_leverage, "
        "max_leverage, entry_tag, side, **kwargs):\n"
        "        if side == 'short':\n"
        "            return 1.0\n"
        "        enter_tags = entry_tag.split()\n"
        "        long_rebuy_mode_tags = self.long_rebuy_mode_tags\n"
        "        long_grind_mode_tags = self.long_grind_mode_tags\n"
        "        if all(c in long_rebuy_mode_tags for c in enter_tags):\n"
        "            return self.futures_mode_leverage_rebuy_mode\n"
        "        elif all(c in long_grind_mode_tags for c in enter_tags):\n"
        "            return self.futures_mode_leverage_grind_mode\n"
        "        return self.futures_mode_leverage\n",
        encoding="utf-8",
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="NostalgiaForInfinityX7Leverage"),
        trading_mode="futures",
        run_mode="backtest",
    )

    assert not result["hot_loop_ready"]
    assert result["callbacks"][0]["backend"] == "uncompiled-python-source"


def test_backtest_bot_loop_base_delegation_lowers_to_rust_noop(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Lifecycle.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Lifecycle(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def bot_loop_start(self, current_time, **kwargs):\n"
        "        if self.config['runmode'].value not in ('live', 'dry_run'):\n"
        "            return super().bot_loop_start(current_time, **kwargs)\n"
        "        self.refresh_remote_state()\n",
        encoding="utf-8",
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="Lifecycle"),
        run_mode="backtest",
    )

    callback = result["callbacks"][0]
    assert result["hot_loop_ready"]
    assert result["blockers"] == []
    assert callback["backend"] == "rust-noop"
    assert callback["lowering"]["operation"]["opcode"] == "noop"


def test_bot_loop_near_miss_with_side_effect_before_return_fails_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Lifecycle.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Lifecycle(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def bot_loop_start(self, current_time, **kwargs):\n"
        "        if self.config['runmode'].value not in ('live', 'dry_run'):\n"
        "            self.mutate_state()\n"
        "            return super().bot_loop_start(current_time, **kwargs)\n",
        encoding="utf-8",
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="Lifecycle"),
        run_mode="backtest",
    )

    assert not result["hot_loop_ready"]
    assert result["callbacks"][0]["backend"] == "uncompiled-python-source"


def test_x7_open_order_timeouts_are_bound_to_immediate_fill_backtests(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Timeouts.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Timeouts(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def check_entry_timeout(self, pair, trade, order, current_time, **kwargs):\n"
        "        ob = self.dp.orderbook(pair, 1)\n"
        "        bids = ob['bids'][0][0]\n"
        "        asks = ob['asks'][0][0]\n"
        "        if trade.is_short:\n"
        "            if asks < order.price * 0.97:\n"
        "                return True\n"
        "        else:\n"
        "            if bids > order.price * 1.03:\n"
        "                return True\n"
        "        return False\n"
        "    def check_exit_timeout(self, pair, trade, order, current_time, **kwargs):\n"
        "        ob = self.dp.orderbook(pair, 1)\n"
        "        bids = ob['bids'][0][0]\n"
        "        asks = ob['asks'][0][0]\n"
        "        if trade.is_short:\n"
        "            if bids > order.price * 1.03:\n"
        "                return True\n"
        "        else:\n"
        "            if asks < order.price * 0.97:\n"
        "                return True\n"
        "        return False\n",
        encoding="utf-8",
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="Timeouts"),
        run_mode="backtest",
    )

    callbacks = {item["name"]: item for item in result["callbacks"]}
    assert result["hot_loop_ready"]
    assert result["blockers"] == []
    assert {
        callback["backend"] for callback in callbacks.values()
    } == {"rust-immediate-fill-open-order-proof"}
    assert callbacks["check_entry_timeout"]["lowering"]["operation"] == {
        "opcode": "open-order-timeout-policy-v1",
        "execution_scope": "unreachable-immediate-fill-backtest-v1",
        "orderbook_depth": 1,
        "short": {
            "price": "asks",
            "comparison": "less-than",
            "order_price_multiplier": 0.97,
        },
        "long": {
            "price": "bids",
            "comparison": "greater-than",
            "order_price_multiplier": 1.03,
        },
    }


def test_open_order_timeout_near_miss_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "Timeout.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Timeout(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def check_entry_timeout(self, pair, trade, order, current_time, **kwargs):\n"
        "        ob = self.dp.orderbook(pair, 1)\n"
        "        bids = ob['bids'][0][0]\n"
        "        asks = ob['asks'][0][0]\n"
        "        if trade.is_short:\n"
        "            if asks < order.price * 0.96:\n"
        "                return True\n"
        "        else:\n"
        "            if bids > order.price * 1.03:\n"
        "                return True\n"
        "        return False\n",
        encoding="utf-8",
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="Timeout"),
        run_mode="backtest",
    )

    assert not result["hot_loop_ready"]
    assert result["callbacks"][0]["backend"] == "uncompiled-python-source"


def test_callback_lowering_hash_check_accepts_windows_crlf(tmp_path: Path) -> None:
    source = tmp_path / "Crlf.py"
    source.write_bytes(
        (
            "from freqtrade.strategy import IStrategy\n"
            "class Crlf(IStrategy):\n"
            "    timeframe = '5m'\n"
            "    def bot_loop_start(self, current_time, **kwargs):\n"
            "        if self.config['runmode'].value not in ('live', 'dry_run'):\n"
            "            return super().bot_loop_start(current_time, **kwargs)\n"
        )
        .replace("\n", "\r\n")
        .encode()
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="Crlf"),
        run_mode="backtest",
    )

    assert result["hot_loop_ready"]


def test_x7_order_filled_state_machine_is_structurally_lowered(
    tmp_path: Path,
) -> None:
    source = tmp_path / "OrderState.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class OrderState(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    system_v3_name = 'system_v3'\n"
        "    system_v3_1_name = 'system_v3_1'\n"
        "    system_v3_2_name = 'system_v3_2'\n"
        "    system_name_use = system_v3_2_name\n"
        "    def order_filled(self, pair, trade, order, current_time, **kwargs):\n"
        "        system_name_use = self.system_name_use\n"
        "        system_v3_2_name = self.system_v3_2_name\n"
        "        system_v3_1_name = self.system_v3_1_name\n"
        "        system_v3_name = self.system_v3_name\n"
        "        set_custom_data = trade.set_custom_data\n"
        "        if trade.nr_of_successful_entries == 1:\n"
        "            if system_name_use == system_v3_2_name:\n"
        "                set_custom_data(key='system_version', value=system_v3_2_name)\n"
        "            elif system_name_use == system_v3_1_name:\n"
        "                set_custom_data(key='system_version', value=system_v3_1_name)\n"
        "            elif system_name_use == system_v3_name:\n"
        "                set_custom_data(key='system_version', value=system_v3_name)\n"
        "        if system_name_use == system_v3_2_name:\n"
        "            filled_entries = trade.select_filled_orders(trade.entry_side)\n"
        "            order_tag = order.ft_order_tag\n"
        "            if order_tag is None:\n"
        "                return None\n"
        "            order_mode = order_tag.split(' ', 1)\n"
        "            order_tags = []\n"
        "            if len(order_mode) > 0:\n"
        "                order_mode = order_mode[0]\n"
        "            order_tags = order_tag.split(' ')\n"
        "            if len(order_tags) > 1:\n"
        "                order_tags = order_tags[1:]\n"
        "            if order_mode in ['derisk_level_1']:\n"
        "                trade.set_custom_data(key='derisk_level_1', value=True)\n"
        "            elif order_mode in ['grind_1_exit']:\n"
        "                trade.set_custom_data("
        "key='grind_1_cluster_max_profit_stake', value=0.0)\n"
        "                trade.set_custom_data("
        "key='grind_1_cluster_max_profit_rate', value=0.0)\n"
        "        return None\n",
        encoding="utf-8",
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="OrderState"),
        run_mode="backtest",
    )

    callback = result["callbacks"][0]
    operation = callback["lowering"]["operation"]
    assert result["hot_loop_ready"]
    assert callback["backend"] == "rust-order-state"
    assert operation["initial_successful_entry_writes"] == [
        {"key": "system_version", "value": "system_v3_2"}
    ]
    assert operation["order_tag_actions"]["grind_1_exit"] == [
        {"key": "grind_1_cluster_max_profit_stake", "value": 0.0},
        {"key": "grind_1_cluster_max_profit_rate", "value": 0.0},
    ]


def test_x7_order_filled_near_miss_with_unbounded_call_fails_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "OrderState.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class OrderState(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    system_v3_2_name = 'system_v3_2'\n"
        "    system_name_use = system_v3_2_name\n"
        "    def order_filled(self, pair, trade, order, current_time, **kwargs):\n"
        "        self.send_network_event()\n",
        encoding="utf-8",
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="OrderState"),
        run_mode="backtest",
    )

    assert not result["hot_loop_ready"]
    assert result["callbacks"][0]["backend"] == "uncompiled-python-source"


def test_bounded_custom_stake_ast_lowers_without_python_execution(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Stake.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Stake(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    rebuy_tags = ['61', '62']\n"
        "    multiplier = 0.25\n"
        "    def custom_stake_amount(self, pair, current_time, current_rate, "
        "proposed_stake, min_stake, max_stake, leverage, entry_tag, side, **kwargs):\n"
        "        rebuy_tags = self.rebuy_tags\n"
        "        multiplier = self.multiplier\n"
        "        enter_tags = entry_tag.split()\n"
        "        def scaled_stake(stake_multiplier):\n"
        "            stake = proposed_stake * stake_multiplier\n"
        "            return stake if stake > min_stake else min_stake\n"
        "        if side == 'long':\n"
        "            if all(c in rebuy_tags for c in enter_tags):\n"
        "                return scaled_stake(multiplier)\n"
        "        return proposed_stake\n",
        encoding="utf-8",
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="Stake"),
        run_mode="backtest",
    )

    callback = result["callbacks"][0]
    assert result["hot_loop_ready"]
    assert callback["backend"] == "rust-stake-vm"
    assert callback["lowering"]["operation"]["opcode"] == "custom-stake-program-v1"


def test_custom_stake_ast_with_unrecognized_call_fails_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Stake.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Stake(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def custom_stake_amount(self, pair, current_time, current_rate, "
        "proposed_stake, min_stake, max_stake, leverage, entry_tag, side, **kwargs):\n"
        "        def scaled_stake(stake_multiplier):\n"
        "            stake = proposed_stake * stake_multiplier\n"
        "            return stake if stake > min_stake else min_stake\n"
        "        return external_stake_service(pair)\n",
        encoding="utf-8",
    )

    result = build_hot_callback_ir(
        analyze_strategy(source, class_name="Stake"),
        run_mode="backtest",
    )

    assert not result["hot_loop_ready"]
    assert result["callbacks"][0]["backend"] == "uncompiled-python-source"
