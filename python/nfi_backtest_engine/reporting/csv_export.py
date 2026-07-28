"""Derived trades CSV export."""

from __future__ import annotations

import csv
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import _CSV_FIELDS
from .values import _float, _iso_timestamp, _mapping


def _write_trades_csv(
    destination: Path,
    surface: Mapping[str, Any] | None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    trades = surface.get("trades", []) if surface is not None else []
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        if not isinstance(trades, list):
            return
        for trade in trades:
            if not isinstance(trade, Mapping):
                continue
            profit = _mapping(trade, "profit")
            fees = _mapping(trade, "fees")
            ratio = _float(profit.get("ratio"))
            writer.writerow(
                {
                    "sequence": trade.get("sequence"),
                    "pair": trade.get("pair"),
                    "direction": trade.get("direction"),
                    "open_time_utc": _iso_timestamp(trade.get("open_timestamp_ms")),
                    "close_time_utc": _iso_timestamp(trade.get("close_timestamp_ms")),
                    "duration_minutes": trade.get("duration_minutes"),
                    "open_rate": trade.get("open_rate"),
                    "close_rate": trade.get("close_rate"),
                    "amount": trade.get("amount"),
                    "stake_amount": trade.get("stake_amount"),
                    "max_stake_amount": trade.get("max_stake_amount"),
                    "leverage": trade.get("leverage"),
                    "profit_abs": profit.get("absolute"),
                    "profit_ratio": profit.get("ratio"),
                    "profit_percent": ratio * 100 if ratio is not None else None,
                    "entry_tag": trade.get("entry_tag"),
                    "exit_reason": trade.get("exit_reason"),
                    "fee_open_rate": fees.get("open_rate"),
                    "fee_close_rate": fees.get("close_rate"),
                    "funding": fees.get("funding"),
                    "liquidation_price": trade.get("liquidation_price"),
                    "initial_stop_loss": trade.get("initial_stop_loss"),
                    "stop_loss": trade.get("stop_loss"),
                    "minimum_rate": trade.get("minimum_rate"),
                    "maximum_rate": trade.get("maximum_rate"),
                    "order_count": len(trade.get("orders", []))
                    if isinstance(trade.get("orders"), list)
                    else 0,
                    "is_open": trade.get("is_open"),
                }
            )
