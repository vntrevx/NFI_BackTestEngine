"""Typed entry callback signatures."""

from typing import Any

ENTRY_SIGNATURES: dict[str, dict[str, Any]] = {
    "confirm_trade_entry": {
        "inputs": ["pair", "order_type", "amount", "rate", "timestamp", "side"],
        "returns": "bool",
    },
    "custom_entry_price": {
        "inputs": ["pair", "trade|null", "timestamp", "proposed_rate", "entry_tag", "side"],
        "returns": "price",
    },
    "custom_stake_amount": {
        "inputs": [
            "pair",
            "timestamp",
            "rate",
            "proposed_stake",
            "minimum_stake|null",
            "maximum_stake",
            "leverage",
            "entry_tag|null",
            "side",
        ],
        "returns": "stake",
    },
}
