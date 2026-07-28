"""Stable result artifact names and CSV schema."""

from __future__ import annotations

SUMMARY_FILENAME = "summary.json"

TRADES_FILENAME = "trades.csv"

HTML_FILENAME = "report.html"

_TERMINAL_BREAKDOWN_NAME_LIMIT = 48

_CSV_FIELDS = (
    "sequence",
    "pair",
    "direction",
    "open_time_utc",
    "close_time_utc",
    "duration_minutes",
    "open_rate",
    "close_rate",
    "amount",
    "stake_amount",
    "max_stake_amount",
    "leverage",
    "profit_abs",
    "profit_ratio",
    "profit_percent",
    "entry_tag",
    "exit_reason",
    "fee_open_rate",
    "fee_close_rate",
    "funding",
    "liquidation_price",
    "initial_stop_loss",
    "stop_loss",
    "minimum_rate",
    "maximum_rate",
    "order_count",
    "is_open",
)
