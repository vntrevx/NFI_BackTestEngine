"""Run unchanged donor profit arithmetic with official Trade/Order objects."""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

import freqtrade
from freqtrade.persistence import Order, Trade

source = Path("benchmarks/fixtures/source-contracts/virtual-fees/profit.source")
namespace = {}
exec(
    compile("from __future__ import annotations\n" + source.read_text(), str(source), "exec"),
    namespace,
)
owner = namespace["SourceProfit"]()
cases = []
for short, futures, rates, orders in itertools.product(
    (False, True),
    (False, True),
    ((None, None), (0, None), (None, 0), (0, 0), (0.001, None), (None, 0.002), (0.001, 0.002)),
    (
        [(True, 0.123, 32104.7)],
        [(True, 0.123, 32104.7), (True, 0.045, 31523.2), (False, 0.041, 33745.8)],
        [
            (True, 0.123, 32104.7),
            (False, 0.041, 33745.8),
            (True, 0.045, 31523.2),
            (False, 0.003, 30981.9),
            (True, 0.087, 31827.3),
        ],
    ),
):
    owner.custom_fee_open_rate, owner.custom_fee_close_rate = rates
    owner.is_futures_mode = futures
    trade = Trade(
        pair="BTC/USDT",
        amount=0.123,
        open_rate=32104.7,
        stake_amount=3948.8781,
        fee_open=0.0004,
        fee_close=0.0005,
        is_short=short,
        leverage=3.0 if futures else 1.0,
        funding_fees=-0.173 if futures else None,
    )
    trade.orders = [
        Order(
            order_id=str(i),
            ft_pair=trade.pair,
            ft_order_side=trade.entry_side if entry else trade.exit_side,
            ft_is_open=False,
            status="closed",
            amount=amount,
            filled=amount,
            average=price,
            price=price,
        )
        for i, (entry, amount, price) in enumerate(orders)
    ]
    expected = owner.calc_total_profit(
        trade,
        [o for o, spec in zip(trade.orders, orders, strict=True) if spec[0]],
        [o for o, spec in zip(trade.orders, orders, strict=True) if not spec[0]],
        32918.6,
    )
    cases.append(
        {
            "short": short,
            "futures": futures,
            "open_rate": rates[0],
            "close_rate": rates[1],
            "actual_open_rate": trade.fee_open,
            "actual_close_rate": trade.fee_close,
            "funding_fees": trade.funding_fees,
            "exit_rate": 32918.6,
            "orders": orders,
            "expected": expected,
        }
    )
print(
    json.dumps(
        {
            "freqtrade_version": freqtrade.__version__,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "cases": cases,
        },
        indent=2,
        allow_nan=False,
    )
)
