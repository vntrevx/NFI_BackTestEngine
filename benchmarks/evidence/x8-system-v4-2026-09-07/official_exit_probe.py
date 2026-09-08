"""Capture exact donor exit callbacks with official Freqtrade Trade/Order objects.

Run from the repository root in the pinned image with the repository read-only.
Only data-provider/order/profit-cache plumbing and downstream managed exit calls
are stubbed for the early-exit probe. Stop/target/helper method bodies are unchanged.
These callback vectors supplement, and do not replace, full backtest captures.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import freqtrade
import numpy as np
import pandas as pd
from freqtrade.persistence import Order, Trade
from freqtrade.persistence.custom_data import CustomDataWrapper

ROOT = Path("benchmarks/fixtures/source-contracts/x8-system-v4")
body, hashes = [], {}
for name in ("dispatch.source", "exits.source"):
    source = ROOT / name
    hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    cls = next(
        node for node in ast.parse(source.read_text()).body if isinstance(node, ast.ClassDef)
    )
    body.extend(cls.body)
cls = ast.ClassDef(name="SourceCallbacks", bases=[], keywords=[], body=body, decorator_list=[])
module = ast.Module(
    body=[ast.parse("from __future__ import annotations").body[0], cls], type_ignores=[]
)
namespace = {"np": np, "datetime": datetime, "timedelta": timedelta}
exec(compile(ast.fix_missing_locations(module), "retained-source-contracts", "exec"), namespace)
Owner = namespace["SourceCallbacks"]
Trade.use_db = False
CustomDataWrapper.use_db = False
NOW = datetime(2022, 4, 1, tzinfo=UTC)
VARIANTS = {
    "default": {},
    "doom-enabled": {"system_v4_stops_enable": True},
    "ue-enabled": {"u_e_stops_enable": True},
    "all-stops-off": {"stops_enable": False},
    "controller-off": {"system_v4_bad_trade_exit_enable": False},
    "structure-off": {"bad_trade_controller_structure_break_exit_enable": False},
    "stale-off": {"bad_trade_controller_stale_exit_enable": False},
}
records = []


def setup(short, futures, tag="1", amount=1.0, exit_tag=None):
    identifier = len(records) + 1
    trade = Trade(
        id=identifier,
        pair="BTC/USDT:USDT" if futures else "BTC/USDT",
        exchange="binance",
        is_short=short,
        open_date=NOW,
        open_rate=100.0,
        amount=amount,
        stake_amount=100.0,
        fee_open=0.0,
        fee_close=0.0,
        is_open=True,
        leverage=3.0 if futures else 1.0,
        enter_tag=tag,
    )
    entry = Order(
        order_id=f"entry-{identifier}",
        ft_pair=trade.pair,
        ft_order_side=trade.entry_side,
        ft_is_open=False,
        status="closed",
        filled=1.0,
        amount=1.0,
        average=100.0,
        price=100.0,
        ft_order_tag=tag,
        order_date=NOW,
        order_filled_date=NOW,
    )
    trade.orders = [entry]
    if exit_tag is not None:
        trade.orders.append(
            Order(
                order_id=f"exit-{identifier}",
                ft_pair=trade.pair,
                ft_order_side=trade.exit_side,
                ft_is_open=False,
                status="closed",
                filled=0.01,
                amount=0.01,
                average=100.0,
                price=100.0,
                ft_order_tag=exit_tag,
                order_date=NOW,
                order_filled_date=NOW,
            )
        )
    trade.set_custom_data("system_version", "system_v4")
    return trade, entry


def owner(variant, futures):
    obj = Owner()
    obj.is_futures_mode = futures
    obj.dp = SimpleNamespace(runmode=SimpleNamespace(value="backtest"))
    for key, value in VARIANTS[variant].items():
        setattr(obj, key, value)
    return obj


def native_trade(trade):
    return {
        "is_short": trade.is_short,
        "leverage": trade.leverage,
        "open_rate": trade.open_rate,
        "amount": trade.amount,
    }


def common(trade, entry, row, previous, profit):
    return dict(
        mode_name="short_normal" if trade.is_short else "long_normal",
        pair=trade.pair,
        trade=trade,
        current_time=NOW,
        current_rate=100.0,
        profit_stake=profit,
        profit_ratio=profit / 100.0,
        profit_current_stake_ratio=profit / 100.0,
        profit_init_ratio=profit / 100.0,
        max_profit=0.0,
        max_loss=0.0,
        filled_orders=trade.orders,
        filled_entries=[entry],
        filled_exits=trade.select_filled_orders(trade.exit_side),
        last_candle=row,
        previous_candle_1=previous,
        buy_tag=trade.enter_tag.split(),
    )


def call(method, values):
    return method(**{key: values[key] for key in inspect.signature(method).parameters})


for variant in ("default", "doom-enabled", "ue-enabled", "all-stops-off"):
    for short in (False, True):
        for futures in (False, True):
            obj = owner(variant, futures)
            leverage = 3.0 if futures else 1.0
            boundary = -(100.0 * (0.35 if futures else 0.14) / leverage)
            for profit in (boundary - 1e-10, boundary, boundary + 1e-10, -50.0):
                trade, entry = setup(short, futures)
                row = {
                    "close": 100.5 if short else 99.5,
                    "EMA_200": 100.0,
                    "RSI_14": 20.0 if short else 80.0,
                    "CMF_20": 0.1 if short else -0.1,
                    "RSI_14_1h": 50.0,
                }
                previous = {"RSI_14": 30.0 if short else 70.0}
                values = common(trade, entry, row, previous, profit)
                helper = "short_exit_stoploss" if short else "long_exit_stoploss"
                expected = call(getattr(obj, helper), values)
                native = {
                    key: values[key]
                    for key in ("mode_name", "profit_stake", "last_candle", "previous_candle_1")
                }
                native.update(
                    trade=native_trade(trade),
                    is_futures_mode=futures,
                    entry_cost=entry.safe_filled * entry.safe_price,
                )
                records.append(
                    dict(
                        kind="stop",
                        variant=variant,
                        helper=helper,
                        inputs=native,
                        expected=expected,
                    )
                )

for short in (False, True):
    for tag in ("501", "661") if short else ("1", "161"):
        for bucket in range(13):
            profit = 0.005 if bucket == 0 else bucket / 100.0
            for drawdown in (0.008, 0.02, 0.06):
                obj = owner("default", True)
                trade, entry = setup(short, True, tag)
                row = {
                    "RSI_14": 50.0,
                    "CMF_20": 0.0,
                    "CMF_20_1h": 0.0,
                    "CMF_20_4h": 0.0,
                    "ROC_9_4h": 0.0,
                }
                values = common(trade, entry, row, row, profit * 100.0)
                mode = values["mode_name"]
                values.update(
                    previous_rate=100.0,
                    previous_profit=profit + drawdown,
                    previous_sell_reason=f"exit_profit_{mode}_max",
                    previous_time_profit_reached=NOW,
                    enter_tags=tag.split(),
                )
                removed = []
                obj._remove_profit_target = removed.append
                expected = call(obj.exit_profit_target, values)
                native = {
                    key: values[key]
                    for key in (
                        "mode_name",
                        "profit_stake",
                        "profit_ratio",
                        "profit_current_stake_ratio",
                        "profit_init_ratio",
                        "last_candle",
                        "previous_candle_1",
                        "previous_profit",
                        "previous_sell_reason",
                    )
                }
                native.update(trade=native_trade(trade), is_futures_mode=True, is_derisk=False)
                records.append(
                    dict(
                        kind="target",
                        variant="default",
                        inputs=native,
                        tags=tag.split(),
                        expected=[*expected, bool(removed)],
                    )
                )

for reason in ("stoploss_doom", "stoploss", "stoploss_u_e"):
    for profit in (-0.09, -0.01, 0.01):
        for amount, exit_tag in (
            (1.0, None),
            (1.0, "d detail"),
            (1.0, "d\tdetail"),
            (0.95, "other"),
            (0.949999, "other"),
        ):
            obj = owner("default", True)
            trade, entry = setup(False, True, amount=amount, exit_tag=exit_tag)
            row = {
                "RSI_14": 50.0,
                "CMF_20": 0.0,
                "CMF_20_1h": 0.0,
                "CMF_20_4h": 0.0,
                "ROC_9_4h": 0.0,
            }
            values = common(trade, entry, row, row, profit * 100.0)
            values.update(
                previous_rate=100.0,
                previous_profit=-0.02,
                previous_sell_reason=f"exit_long_normal_{reason}",
                previous_time_profit_reached=NOW,
                enter_tags=["1"],
            )
            removed = []
            obj._remove_profit_target = removed.append
            expected = call(obj.exit_profit_target, values)
            native = {
                key: values[key]
                for key in (
                    "mode_name",
                    "profit_stake",
                    "profit_ratio",
                    "profit_current_stake_ratio",
                    "profit_init_ratio",
                    "last_candle",
                    "previous_candle_1",
                    "previous_profit",
                    "previous_sell_reason",
                )
            }
            native.update(trade=native_trade(trade), is_futures_mode=True)
            records.append(
                dict(
                    kind="target",
                    variant="default",
                    inputs=native,
                    tags=["1"],
                    exit_tags=[] if exit_tag is None else [exit_tag],
                    first_amount=1.0,
                    expected=[*expected, bool(removed)],
                )
            )

for variant in ("default", "controller-off", "structure-off", "stale-off"):
    for short in (False, True):
        for rate, ema, days, range_pct, tag, profit in (
            (74.5, 50.0, 15.5, 6.0, "1 192 ", -0.4),
            (75.0, 65.0, 1.0, 6.0, "1 ", -0.2),
            (75.00001, 65.0, 1.0, 6.0, "1 ", -0.2),
            (90.0, 100.0, 14.5, 6.0, "1 ", -0.1),
            (90.0, 100.0, 13.999, 6.0, "1 ", -0.1),
            (90.0, 100.0, 15.5, 7.0, "1 ", -0.1),
            (90.0, math.nan, 15.5, math.nan, "192 ", -0.3),
            (90.0, math.inf, 15.5, math.nan, "1921 ", -0.3),
            (74.5, 50.0, 15.5, 6.0, "192 ", 0.0),
        ):
            if short:
                rate, ema = 200.0 - rate, 200.0 - ema
            obj = owner(variant, True)
            trade, entry = setup(short, True, tag)
            row = {"EMA_50_1d": ema, "EMA_200_1d": 100.0, "RANGE_PCT_14_1d": range_pct}
            frame = pd.DataFrame([row, row])
            obj.dp.get_analyzed_dataframe = lambda *args, frame=frame: (frame, None)
            obj.filled_order_snapshot = lambda trade, entry=entry: (trade.orders, [entry], [])
            obj.calc_total_profit = lambda *args: (0.0, 0.0, 0.0, 0.0)
            obj.cache_backtest_profit_snapshot = lambda *args: None
            custom_exit = next(
                n for n in body if isinstance(n, ast.FunctionDef) and n.name == "custom_exit"
            )
            for node in ast.walk(custom_exit):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr.startswith(("long_exit_", "short_exit_"))
                    and not node.attr.endswith("_tags")
                ):
                    setattr(obj, node.attr, lambda *args: (False, None))
            now = NOW + timedelta(days=days)
            expected = obj.custom_exit(trade.pair, trade, now, rate, profit)
            native = dict(
                trade=native_trade(trade),
                current_time=(now - NOW).total_seconds(),
                current_rate=rate,
                current_profit=profit,
                last_candle=row,
                filled_entries=[{"safe_price": entry.safe_price}],
                enter_tag=tag,
                enter_tags=tag.split(),
                system_version="system_v4",
            )
            records.append(dict(kind="prefix", variant=variant, inputs=native, expected=expected))


def portable(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
    if isinstance(value, dict):
        return {key: portable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [portable(item) for item in value]
    return value


print(
    json.dumps(
        portable(
            dict(
                freqtrade_version=freqtrade.__version__,
                sources=hashes,
                scope=__doc__,
                variants=VARIANTS,
                cases=records,
            )
        ),
        indent=2,
        allow_nan=False,
    )
)
