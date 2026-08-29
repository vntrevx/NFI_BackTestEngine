from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

OFFICIAL = Path(
    "benchmarks/fixtures/captured/"
    "current-long-grind-liquidation-rescue-futures-r1/"
    "artifacts/trade-surface.json"
)
NATIVE = Path("benchmarks/evidence/task9/native-result.json")


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def test_current_long_grind_official_native_surface_is_zero_tolerance() -> None:
    # Given: the durable pinned official surface and recorded Native result.
    official = json.loads(OFFICIAL.read_text(encoding="utf-8"))
    native = json.loads(NATIVE.read_text(encoding="utf-8"))
    official_trade = official["trades"][0]
    native_trade = native["trades"][0]

    # When: machine-consumed wallet, trade, funding, and order values normalize.
    official_surface: dict[str, Any] = {
        "starting_balance": _decimal(official["summary"]["starting_balance"]),
        "final_balance": _decimal(official["summary"]["final_balance"]),
        "profit_total_abs": _decimal(official["summary"]["profit_total_abs"]),
        "pair": official_trade["pair"],
        "is_short": official_trade["direction"] == "short",
        "leverage": _decimal(official_trade["leverage"]),
        "open_timestamp_ms": official_trade["open_timestamp_ms"],
        "close_timestamp_ms": official_trade["close_timestamp_ms"],
        "open_rate": _decimal(official_trade["open_rate"]),
        "close_rate": _decimal(official_trade["close_rate"]),
        "amount": _decimal(official_trade["amount"]),
        "stake_amount": _decimal(official_trade["stake_amount"]),
        "max_stake_amount": _decimal(official_trade["max_stake_amount"]),
        "entry_tag": official_trade["entry_tag"],
        "exit_reason": official_trade["exit_reason"],
        "funding_fees": _decimal(official_trade["fees"]["funding"]),
        "profit_abs": _decimal(official_trade["profit"]["absolute"]),
        "profit_ratio": _decimal(official_trade["profit"]["ratio"]),
        "minimum_rate": _decimal(official_trade["minimum_rate"]),
        "maximum_rate": _decimal(official_trade["maximum_rate"]),
        "orders": [
            {
                "sequence": order["sequence"],
                "side": order["side"],
                "is_entry": order["is_entry"],
                "filled_timestamp_ms": order["filled_timestamp_ms"],
                "amount": _decimal(order["amount"]),
                "price": _decimal(order["price"]),
                "cost": _decimal(order["cost"]),
                "tag": order["tag"],
            }
            for order in official_trade["orders"]
        ],
    }
    native_surface: dict[str, Any] = {
        "starting_balance": _decimal(native["starting_balance"]),
        "final_balance": _decimal(native["final_balance"]),
        "profit_total_abs": _decimal(native["profit_total_abs"]),
        **{
            key: native_trade[key]
            for key in (
                "pair",
                "is_short",
                "open_timestamp_ms",
                "close_timestamp_ms",
                "entry_tag",
                "exit_reason",
            )
        },
        **{
            key: _decimal(native_trade[key])
            for key in (
                "leverage",
                "open_rate",
                "close_rate",
                "amount",
                "stake_amount",
                "max_stake_amount",
                "funding_fees",
                "profit_abs",
                "profit_ratio",
                "minimum_rate",
                "maximum_rate",
            )
        },
        "orders": [
            {
                **{
                    key: order[key]
                    for key in (
                        "sequence",
                        "side",
                        "is_entry",
                        "filled_timestamp_ms",
                        "tag",
                    )
                },
                **{
                    key: _decimal(order[key])
                    for key in ("amount", "price", "cost")
                },
            }
            for order in native_trade["orders"]
        ],
    }

    # Then: no numeric or structural tolerance is required.
    assert official_surface == native_surface
