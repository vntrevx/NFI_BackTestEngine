"""Typed leverage callback signatures."""

from typing import Any

LEVERAGE_SIGNATURES: dict[str, dict[str, Any]] = {
    "leverage": {
        "inputs": [
            "pair",
            "timestamp",
            "rate",
            "proposed_leverage",
            "maximum_leverage",
            "entry_tag|null",
            "side",
        ],
        "returns": "leverage",
    },
}
