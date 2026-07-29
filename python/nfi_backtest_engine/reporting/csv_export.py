"""Derived trades CSV export."""

from __future__ import annotations

import csv
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import (
    _EQUITY_CSV_FIELDS,
    _ORDERS_CSV_FIELDS,
    _TRADES_CSV_FIELDS,
    EQUITY_CSV_SCHEMA_VERSION,
    ORDERS_CSV_SCHEMA_VERSION,
)
from .tags import parse_order_tag, trade_tag_details
from .values import _float, _iso_timestamp, _mapping


def _write_trades_csv(
    destination: Path,
    surface: Mapping[str, Any] | None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    trades = surface.get("trades", []) if surface is not None else []
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=_TRADES_CSV_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        if not isinstance(trades, list):
            return
        for trade in trades:
            if not isinstance(trade, Mapping):
                continue
            tag_details = trade_tag_details(trade)
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
                    "signal_tags": " ".join(tag_details["signal_tags"]),
                    "signal_tag_count": len(tag_details["signal_tags"]),
                    "grind_levels": " ".join(
                        str(level) for level in tag_details["grind_levels"]
                    ),
                    "grind_order_count": tag_details["grind_order_count"],
                    "grind_entry_count": tag_details["grind_entry_count"],
                    "grind_exit_count": tag_details["grind_exit_count"],
                    "grind_derisk_count": tag_details["grind_derisk_count"],
                }
            )


def _write_orders_csv(
    destination: Path,
    surface: Mapping[str, Any] | None,
) -> None:
    """Flatten sealed filled orders without modifying or estimating order values."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    trades = surface.get("trades", []) if surface is not None else []
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=_ORDERS_CSV_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        if not isinstance(trades, list):
            return
        for trade in trades:
            if not isinstance(trade, Mapping):
                continue
            orders = trade.get("orders")
            if not isinstance(orders, list):
                continue
            exit_indexes = [
                index
                for index, order in enumerate(orders)
                if isinstance(order, Mapping) and order.get("is_entry") is False
            ]
            final_exit_index = (
                exit_indexes[-1]
                if exit_indexes and not bool(trade.get("is_open"))
                else None
            )
            for index, order in enumerate(orders):
                if not isinstance(order, Mapping):
                    continue
                is_entry = order.get("is_entry") is True
                is_partial_exit = not is_entry and index != final_exit_index
                position_action = (
                    "entry"
                    if is_entry
                    else "partial_exit"
                    if is_partial_exit
                    else "exit"
                )
                parsed_tag = parse_order_tag(
                    order.get("tag"),
                    is_entry=is_entry,
                    entry_tag=trade.get("entry_tag"),
                    is_initial_entry=index == 0 and is_entry,
                    fallback_action=position_action,
                )
                writer.writerow(
                    {
                        "schema_version": ORDERS_CSV_SCHEMA_VERSION,
                        "trade_sequence": trade.get("sequence"),
                        "order_sequence": order.get("sequence"),
                        "pair": trade.get("pair"),
                        "direction": trade.get("direction"),
                        "position_action": position_action,
                        "side": order.get("side"),
                        "is_entry": is_entry,
                        "is_partial_exit": is_partial_exit,
                        "filled_time_utc": _iso_timestamp(
                            order.get("filled_timestamp_ms")
                        ),
                        "filled_timestamp_ms": order.get("filled_timestamp_ms"),
                        "amount": order.get("amount"),
                        "price": order.get("price"),
                        "cost": order.get("cost"),
                        "tag": order.get("tag"),
                        "tag_token": parsed_tag.token,
                        "tag_family": parsed_tag.family,
                        "tag_level": parsed_tag.level,
                        "tag_action": parsed_tag.action,
                        "tag_reference_order_ids": " ".join(
                            str(order_id)
                            for order_id in parsed_tag.reference_order_ids
                        ),
                        "trade_open_time_utc": _iso_timestamp(
                            trade.get("open_timestamp_ms")
                        ),
                        "trade_close_time_utc": _iso_timestamp(
                            trade.get("close_timestamp_ms")
                        ),
                        "trade_exit_reason": trade.get("exit_reason"),
                        "trade_profit_abs": _mapping(trade, "profit").get(
                            "absolute"
                        ),
                        "leverage": trade.get("leverage"),
                    }
                )


def _write_equity_csv(
    destination: Path,
    rows: list[dict[str, Any]],
) -> None:
    """Write the unsampled closed-trade equity event stream."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer: csv.DictWriter[str] = csv.DictWriter(
            handle,
            fieldnames=list(_EQUITY_CSV_FIELDS),
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "schema_version": EQUITY_CSV_SCHEMA_VERSION,
                    **row,
                    "timestamp_utc": _iso_timestamp(row.get("timestamp_ms")),
                }
            )
