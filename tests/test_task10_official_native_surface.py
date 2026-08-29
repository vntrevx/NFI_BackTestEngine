from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

OFFICIAL = Path(
    "benchmarks/fixtures/captured/"
    "current-short-grind-liquidation-rescue-futures-r1/"
    "artifacts/trade-surface.json"
)
NATIVE = Path("benchmarks/evidence/task10/native-result.json")
BOUNDARY_OFFICIAL = Path(
    "benchmarks/fixtures/captured/"
    "current-short-grind-liquidation-boundary-futures-r1/"
    "artifacts/trade-surface.json"
)
BOUNDARY_NATIVE = Path("benchmarks/evidence/task10/native-boundary-result.json")


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _order_surface(order: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            key: order[key]
            for key in ("sequence", "side", "is_entry", "filled_timestamp_ms", "tag")
        },
        **{
            key: _decimal(order[key])
            for key in ("amount", "price", "cost")
        },
    }


def test_current_short_grind_official_native_surface_is_zero_tolerance() -> None:
    # Given: pinned official and durable manager-bound Native artifacts.
    official = json.loads(OFFICIAL.read_text(encoding="utf-8"))
    native = json.loads(NATIVE.read_text(encoding="utf-8"))
    official_trade = official["trades"][0]
    native_trade = native["trades"][0]

    # When: every observable machine field is normalized without tolerance.
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
        "orders": [_order_surface(order) for order in official_trade["orders"]],
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
        "orders": [_order_surface(order) for order in native_trade["orders"]],
    }

    # Then: short side, funding, GD5 sell, and final buy are exactly equal.
    assert official_surface == native_surface


def test_short_liquidation_boundary_alternate_is_exact_and_has_no_gd5() -> None:
    # Given: the same short scenario at 2x, whose liquidation boundary is not crossed.
    official = json.loads(BOUNDARY_OFFICIAL.read_text(encoding="utf-8"))
    native = json.loads(BOUNDARY_NATIVE.read_text(encoding="utf-8"))
    official_trade = official["trades"][0]
    native_trade = native["trades"][0]

    # Then: the alternate remains exact while the GD5 sell is absent.
    assert _decimal(official["summary"]["final_balance"]) == _decimal(
        native["final_balance"]
    )
    assert _decimal(official["summary"]["profit_total_abs"]) == _decimal(
        native["profit_total_abs"]
    )
    assert _decimal(official_trade["leverage"]) == _decimal(native_trade["leverage"])
    assert _decimal(official_trade["fees"]["funding"]) == _decimal(
        native_trade["funding_fees"]
    )
    assert _decimal(official_trade["stake_amount"]) == _decimal(
        round(native_trade["stake_amount"], 8)
    )
    assert [_order_surface(order) for order in official_trade["orders"]] == [
        _order_surface(order) for order in native_trade["orders"]
    ]
    assert all(order["tag"] != "gd5" for order in official_trade["orders"])


def test_short_fixtures_embed_the_source_bound_schema_030_manager() -> None:
    # Given/When: both reached and alternate Native manifests are inspected.
    roots = [
        OFFICIAL.parents[1],
        BOUNDARY_OFFICIAL.parents[1],
    ]
    for root in roots:
        manifest = json.loads(
            (root / "inputs/native/simulation-input.manifest.json").read_text(
                encoding="utf-8"
            )
        )
        manager = manifest["config"]["nfi_x7_trade_manager"]
        short = manager["short_grind"]
        rescue = next(
            transition["liquidation_rescue"]
            for transition in short["program"]["source_order"]
            if transition.get("liquidation_rescue") is not None
        )

        # Then: the current source identity and short comparison contract are durable.
        assert manager["schema_version"] == "0.30.0"
        assert (
            manager["source_sha256"]
            == "a4ba29b94b459511163f05cce6687b5b84542147b11715a69e3fa468fab2767a"
        )
        assert short["entry_tags"] == ["620"]
        assert short["program"]["side"] == "short"
        assert rescue == {
            "side": "short",
            "cluster_level": 5,
            "loss_threshold": 0.12,
            "profit_comparison": "greater-than",
            "liquidation_multiplier": 0.8,
            "liquidation_comparison": "greater-than",
            "used_state_key": "gd5_liquidation_rescue_used",
        }
