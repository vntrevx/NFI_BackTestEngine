"""Shared strategy-analysis and typed-callback contracts."""

STRATEGY_IR_VERSION = "1.8.0"

HOT_CALLBACKS = {
    "adjust_trade_position",
    "bot_loop_start",
    "confirm_trade_entry",
    "confirm_trade_exit",
    "custom_roi",
    "custom_entry_price",
    "custom_exit",
    "custom_exit_price",
    "custom_stake_amount",
    "custom_stoploss",
}

STRATEGY_CALLBACKS = HOT_CALLBACKS | {
    "adjust_entry_price",
    "adjust_exit_price",
    "adjust_order_price",
    "bot_start",
    "check_entry_timeout",
    "check_exit_timeout",
    "leverage",
    "order_filled",
}
