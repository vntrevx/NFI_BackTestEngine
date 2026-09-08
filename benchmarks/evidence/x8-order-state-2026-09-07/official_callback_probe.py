"""Run the exact donor callback with the pinned Freqtrade Trade/Order models.

This supplements the captured portfolio fixture, whose state projection does not
include arbitrary custom-data fields. It is a direct callback probe, not a backtest.
Run in the pinned image from the repository root with the repository mounted read-only.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import freqtrade
from freqtrade.persistence import Order, Trade
from freqtrade.persistence.custom_data import CustomDataWrapper

source = Path("benchmarks/fixtures/source-contracts/x8-system-v4/strategy.source")
tree = ast.parse(source.read_text(encoding="utf-8"))
strategy = next(node for node in tree.body if isinstance(node, ast.ClassDef))
strategy.bases = []
strategy.body = [
    node for node in strategy.body
    if isinstance(node, ast.Assign)
    or isinstance(node, ast.FunctionDef) and node.name == "order_filled"
]
module = ast.Module(
    body=[ast.parse("from __future__ import annotations").body[0], strategy],
    type_ignores=[],
)
namespace = {}
exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
callback_owner = namespace[strategy.name]()
Trade.use_db = False
CustomDataWrapper.use_db = False
now = datetime(2022, 4, 1, tzinfo=UTC)
results = []

for identifier, is_short in enumerate([False, True], start=1):
    trade = Trade(
        id=identifier, pair="BTC/USDT:USDT", exchange="binance", is_short=is_short,
        open_date=now, open_rate=100.0, amount=1.0, stake_amount=100.0,
        fee_open=0.0, fee_close=0.0, is_open=True,
    )
    entry = Order(
        order_id=f"entry-{identifier}", ft_pair=trade.pair, ft_order_side=trade.entry_side,
        ft_is_open=False, status="closed", filled=1.0, amount=1.0,
        ft_order_tag="initial", order_date=now, order_filled_date=now,
    )
    trade.orders = [entry]
    callback_owner.order_filled(trade.pair, trade, entry, now)
    assert trade.nr_of_successful_entries == 1
    assert trade.nr_of_successful_exits == 0
    assert trade.get_custom_data("system_version") == callback_owner.system_v4_name
    trade.set_custom_data("system_version", "preserved")
    snapshots = []
    for exit_count in [1, 2]:
        trade.set_custom_data("grind_1_cluster_max_profit_stake", 10.0)
        exit_order = Order(
            order_id=f"exit-{identifier}-{exit_count}", ft_pair=trade.pair,
            ft_order_side=trade.exit_side, ft_is_open=False, status="closed",
            filled=0.25, amount=0.25, ft_order_tag="grind_1_exit detail",
            order_date=now, order_filled_date=now,
        )
        trade.orders.append(exit_order)
        callback_owner.order_filled(trade.pair, trade, exit_order, now)
        assert trade.nr_of_successful_exits == exit_count
        assert trade.get_custom_data("system_version") == "preserved"
        assert trade.get_custom_data("grind_1_cluster_max_profit_stake") == 0.0
        snapshots.append({
            "successful_entries": trade.nr_of_successful_entries,
            "successful_exits": trade.nr_of_successful_exits,
            "system_version": trade.get_custom_data("system_version"),
            "cluster_maximum": trade.get_custom_data("grind_1_cluster_max_profit_stake"),
        })
    results.append({"is_short": is_short, "after_exit_fills": snapshots})

print(json.dumps({
    "freqtrade_version": freqtrade.__version__,
    "source_contract_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    "scope": "Exact donor callback with official Trade/Order models; direct probe, not backtest",
    "results": results,
}, indent=2))
