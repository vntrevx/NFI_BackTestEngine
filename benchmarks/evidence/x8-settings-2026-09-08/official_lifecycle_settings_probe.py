"""Pinned inherited stoploss refresh and ROI boundary vectors from the full X8 donor."""

from __future__ import annotations

import contextlib
import hashlib
import io
import itertools
import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import ccxt
import freqtrade
from freqtrade.enums import ExitCheckTuple, ExitType, TradingMode
from freqtrade.optimize.backtesting import CLOSE_IDX, HIGH_IDX, LOW_IDX, OPEN_IDX, Backtesting
from freqtrade.persistence import Trade

# Use exactly the donor loader from the separately retained 96-vector probe.
with contextlib.redirect_stdout(io.StringIO()):
    environment = runpy.run_path(str(Path(__file__).with_name("official_exit_settings_probe.py")))
owner = environment["owner"]
source = environment["source"]
now = datetime(2026, 1, 1, tzinfo=UTC)


def new_trade(short, leverage, funding):
    trade = Trade(
        pair="BTC/USDT",
        amount=0.123,
        open_rate=100.0,
        stake_amount=12.3 / leverage,
        fee_open=0.0004,
        fee_close=0.0005,
        is_short=short,
        leverage=leverage,
        trading_mode=TradingMode.FUTURES if leverage > 1 else TradingMode.SPOT,
        funding_fees=funding,
        price_precision=0.01,
        precision_mode_price=ccxt.TICK_SIZE,
        open_date=now,
    )
    trade.open_trade_value = trade._calc_open_trade_value(trade.amount, trade.open_rate)
    trade.adjust_stop_loss(100.0, -0.20, initial=True)
    return trade


def stop_state(trade):
    return {
        "stop_loss": trade.stop_loss,
        "stop_loss_ratio": trade.stop_loss_pct,
        "trailing": trade.is_stop_loss_trailing,
    }


stops = []
owner.use_custom_stoploss = True
owner._ft_stop_uses_after_fill = True
owner.stoploss = -0.20
for short, leverage, trailing, only_offset, positive, funding, fill in itertools.product(
    (False, True),
    (1.0, 3.0),
    (False, True),
    (False, True),
    (None, 0.0, 0.01),
    (0.0, -0.173),
    (90.0, 110.0),
):
    owner.trailing_stop = trailing
    owner.trailing_only_offset_is_reached = only_offset
    owner.trailing_stop_positive = positive
    owner.trailing_stop_positive_offset = 0.03
    trade = new_trade(short, leverage, funding)
    high, low = (101.0, 96.0) if short else (104.0, 99.0)
    owner.ft_stoploss_adjust(
        100.0, trade, now, trade.calc_profit_ratio(100.0), 0, low=low, high=high
    )
    before = stop_state(trade)
    owner.ft_stoploss_adjust(fill, trade, now, trade.calc_profit_ratio(fill), 0, after_fill=True)
    stops.append(
        {
            "short": short,
            "leverage": leverage,
            "funding": funding,
            "trailing": trailing,
            "only_offset": only_offset,
            "positive": positive,
            "fill": fill,
            "before": before,
            "after": stop_state(trade),
        }
    )

rois = []
backtest = Backtesting.__new__(Backtesting)
backtest.strategy = owner
backtest.timeframe_min = 5
for short, leverage, funding, roi, entry, elapsed, open_rate in itertools.product(
    (False, True),
    (1.0, 3.0),
    (0.0, -0.173),
    (-1.0, 0.0, 0.03, 0.2),
    (0, 3, 5),
    (0, 3, 5, 10),
    (99.0, 100.0, 101.0),
):
    if elapsed < entry:
        continue
    owner.minimal_roi = {entry: roi}
    trade = new_trade(short, leverage, funding)
    trade.open_rate = open_rate
    trade.open_trade_value = trade._calc_open_trade_value(trade.amount, trade.open_rate)
    high, low, close = 104.0, 96.0, (103.0 if short else 97.0)
    current = now + timedelta(minutes=elapsed)
    reached = owner.min_roi_reached(trade, trade.calc_profit_ratio(low if short else high), current)
    row = [0.0] * (max(OPEN_IDX, HIGH_IDX, LOW_IDX, CLOSE_IDX) + 1)
    row[OPEN_IDX], row[HIGH_IDX], row[LOW_IDX], row[CLOSE_IDX] = 100.0, high, low, close
    rate = None
    if reached:
        with contextlib.suppress(ValueError):
            rate = backtest._get_close_rate_for_roi(
                tuple(row), trade, current, ExitCheckTuple(ExitType.ROI), elapsed
            )
    rois.append(
        {
            "short": short,
            "leverage": leverage,
            "funding": funding,
            "roi": roi,
            "entry": entry,
            "elapsed": elapsed,
            "open_rate": open_rate,
            "exit_rate": rate,
        }
    )
print(
    json.dumps(
        {
            "freqtrade_version": freqtrade.__version__,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "stop_cases": stops,
            "roi_cases": rois,
        },
        indent=2,
    )
)
