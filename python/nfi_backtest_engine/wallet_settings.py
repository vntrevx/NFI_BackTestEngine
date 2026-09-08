"""Validated Freqtrade wallet settings shared by native source adapters."""

from __future__ import annotations

import math
import re
from typing import Any

from .errors import StrategyAnalysisError
from .slot_settings import source_trade_slots


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise StrategyAnalysisError(f"config.{name} must be a finite number")
    return float(value)


def starting_wallet_balance(config: dict[str, Any]) -> float:
    """Resolve the stake currency wallet; other currencies do not fund isolated trades."""
    wallet = config.get("dry_run_wallet")
    if isinstance(wallet, dict):
        if config.get("margin_mode") == "cross":
            raise StrategyAnalysisError("multi-currency cross-margin collateral is not supported")
        for currency, amount in wallet.items():
            if not isinstance(currency, str) or re.fullmatch(r"[a-zA-Z0-9]+", currency) is None:
                raise StrategyAnalysisError("config.dry_run_wallet contains an invalid currency")
            _number(amount, f"dry_run_wallet.{currency}")
        wallet = wallet.get(config.get("stake_currency"), 0)
    balance = _number(wallet, "dry_run_wallet")
    if balance <= 0:
        raise StrategyAnalysisError("config.dry_run_wallet stake currency balance must be positive")
    return balance


def wallet_policy(config: dict[str, Any]) -> dict[str, Any]:
    """Keep allocation controls separate from the exchange's actual free funds."""
    amend = config.get("amend_last_stake_amount", False)
    if not isinstance(amend, bool):
        raise StrategyAnalysisError("config.amend_last_stake_amount must be boolean")
    ratio = _number(config.get("last_stake_amount_min_ratio", 0.5), "last_stake_amount_min_ratio")
    if not 0 <= ratio <= 1:
        raise StrategyAnalysisError("config.last_stake_amount_min_ratio must be between 0 and 1")
    capital = None
    if "available_capital" in config:
        capital = _number(config["available_capital"], "available_capital")
        if capital < 0:
            raise StrategyAnalysisError("config.available_capital must be nonnegative")
    if config.get("stake_amount") == "unlimited" and config.get("max_open_trades") in (
        -1,
        math.inf,
    ):
        raise StrategyAnalysisError("max_open_trades and stake_amount cannot both be unlimited")
    slots = source_trade_slots(config) if "max_open_trades" in config else None
    return {
        # The pinned backtester reserves 5% for initial entries independently of
        # the strategy stoploss; adjustment orders use their own zero reserve.
        "entry_minimum_stoploss_ratio": -0.05,
        **({"source_max_open_trades": slots} if slots is not None and slots <= 0 else {}),
        "amend_last_stake_amount": amend,
        "last_stake_amount_min_ratio": ratio,
        "available_capital": capital,
        "initial_asset_balances": {
            currency: _number(amount, f"dry_run_wallet.{currency}")
            for currency, amount in (
                config["dry_run_wallet"].items()
                if isinstance(config.get("dry_run_wallet"), dict)
                else ()
            )
            if currency != config.get("stake_currency")
        },
    }
