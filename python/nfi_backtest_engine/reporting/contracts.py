"""Stable result artifact names and CSV schema."""

from __future__ import annotations

SUMMARY_FILENAME = "summary.json"

TRADES_FILENAME = "trades.csv"

ORDERS_FILENAME = "orders.csv"

EQUITY_FILENAME = "equity.csv"

VERIFICATION_FILENAME = "verification.json"

EVIDENCE_INDEX_FILENAME = "evidence/index.json"

HTML_FILENAME = "report.html"

ORDERS_CSV_SCHEMA_VERSION = "1.0.0"

EQUITY_CSV_SCHEMA_VERSION = "1.0.0"

_TERMINAL_BREAKDOWN_NAME_LIMIT = 48

_TRADES_CSV_FIELDS = (
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

# Backward-compatible private alias retained for callers of the original trades
# exporter. New artifact contracts use the explicit names below.
_CSV_FIELDS = _TRADES_CSV_FIELDS

_ORDERS_CSV_FIELDS = (
    "schema_version",
    "trade_sequence",
    "order_sequence",
    "pair",
    "direction",
    "position_action",
    "side",
    "is_entry",
    "is_partial_exit",
    "filled_time_utc",
    "filled_timestamp_ms",
    "amount",
    "price",
    "cost",
    "tag",
    "trade_open_time_utc",
    "trade_close_time_utc",
    "trade_exit_reason",
    "trade_profit_abs",
    "leverage",
)

_EQUITY_CSV_FIELDS = (
    "schema_version",
    "event_sequence",
    "event",
    "timestamp_utc",
    "timestamp_ms",
    "trade_sequence",
    "pair",
    "direction",
    "profit_abs",
    "equity",
    "peak_equity",
    "drawdown_abs",
    "drawdown_ratio",
    "source_final_balance",
    "reconciliation_delta",
)
