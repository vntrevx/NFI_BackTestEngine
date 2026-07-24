from __future__ import annotations

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.release_contract import (
    FUTURES_RELEASE_CONTRACT,
    SPOT_RELEASE_CONTRACT,
    release_contract_for_config,
    release_contract_for_scope,
)


def _config(
    *,
    trading_mode: str,
    margin_mode: str | None,
    pair: str,
) -> dict:
    return {
        "trading_mode": trading_mode,
        "margin_mode": margin_mode,
        "stake_currency": "USDT",
        "exchange": {
            "name": "binance",
            "pair_whitelist": [pair],
        },
    }


def test_release_contract_is_derived_from_the_effective_config() -> None:
    spot = release_contract_for_config(
        _config(trading_mode="spot", margin_mode="", pair="BTC/USDT")
    )
    futures = release_contract_for_config(
        _config(
            trading_mode="futures",
            margin_mode="isolated",
            pair="BTC/USDT:USDT",
        )
    )

    assert spot is SPOT_RELEASE_CONTRACT
    assert futures is FUTURES_RELEASE_CONTRACT
    assert futures.required_data_roles == ("candles", "funding_rate", "mark")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("margin_mode", "cross", "isolated margin"),
        ("pair", "BTC/USDT", "invalid form"),
        ("exchange", "bybit", "requires exchange"),
        ("stake_currency", "USDC", "requires stake currency"),
    ],
)
def test_futures_release_contract_rejects_out_of_scope_configs(
    field: str,
    value: str,
    message: str,
) -> None:
    config = _config(
        trading_mode="futures",
        margin_mode="isolated",
        pair="BTC/USDT:USDT",
    )
    if field == "pair":
        config["exchange"]["pair_whitelist"] = [value]
    elif field == "exchange":
        config["exchange"]["name"] = value
    else:
        config[field] = value

    with pytest.raises(SpecValidationError, match=message):
        release_contract_for_config(config)


def test_release_scope_must_exactly_match_its_mode_contract() -> None:
    scope = FUTURES_RELEASE_CONTRACT.scope_fields()
    assert release_contract_for_scope(scope) is FUTURES_RELEASE_CONTRACT

    scope["margin_mode"] = "cross"
    with pytest.raises(SpecValidationError, match="contradicts"):
        release_contract_for_scope(scope)


def test_futures_release_contract_requires_real_lifecycle_evidence() -> None:
    requirements = {
        requirement.probe_kind: requirement
        for requirement in FUTURES_RELEASE_CONTRACT.probe_evidence
    }
    lifecycle = requirements["futures-lifecycle"]

    assert "tag-121" in FUTURES_RELEASE_CONTRACT.required_probe_kinds
    assert lifecycle.missing_from(
        {
            "sides": ["long"],
            "funded_trades": 0,
        }
    ) == ["sides:short", "funded_trades:0<1"]
    assert lifecycle.missing_from(
        {
            "sides": ["long", "short"],
            "funded_trades": 1,
        }
    ) == []
