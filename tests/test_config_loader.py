from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.config_loader import (
    freeze_pairlist,
    load_effective_config,
    sanitize_config,
)
from nfi_backtest_engine.errors import SpecValidationError


def test_split_nfi_config_is_merged_redacted_and_hashed(tmp_path: Path) -> None:
    included = tmp_path / "included.json"
    included.write_text(
        """
        {
          // Freqtrade accepts comments in config files.
          "exchange": {
            "name": "binance",
            "key": "public-key",
            "secret": "private-secret",
            "pair_whitelist": ["BTC/USDT", "ETH/USDT"],
          },
          "max_open_trades": 4
        }
        """,
        encoding="utf-8",
    )
    root = tmp_path / "config.json"
    root.write_text(
        """
        {
          "add_config_files": ["included.json"],
          "max_open_trades": 6,
          "trading_mode": "spot"
        }
        """,
        encoding="utf-8",
    )

    loaded = load_effective_config(root)

    assert loaded["config"]["max_open_trades"] == 6
    assert loaded["redacted_config"]["exchange"]["key"] == "<redacted>"
    assert loaded["redacted_config"]["exchange"]["secret"] == "<redacted>"
    frozen = freeze_pairlist(loaded["config"])
    assert frozen["pairs"] == ["BTC/USDT", "ETH/USDT"]
    assert len(frozen["sha256"]) == 64


def test_config_include_cycle_is_rejected(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text('{"add_config_files": ["second.json"]}', encoding="utf-8")
    second.write_text('{"add_config_files": ["first.json"]}', encoding="utf-8")

    with pytest.raises(SpecValidationError, match="include cycle"):
        load_effective_config(first)


def test_pairlist_duplicate_is_rejected() -> None:
    config = {
        "exchange": {
            "name": "binance",
            "pair_whitelist": ["BTC/USDT", "BTC/USDT"],
        }
    }

    with pytest.raises(SpecValidationError, match="duplicate"):
        freeze_pairlist(config)


def test_sanitize_config_blanks_credentials_without_mutating_source() -> None:
    source = {
        "exchange": {
            "name": "binance",
            "key": "public",
            "secret": "private",
        },
        "telegram": {"token": "bot-token"},
    }

    result = sanitize_config(source)

    assert result["exchange"]["key"] == ""
    assert result["exchange"]["secret"] == ""
    assert result["telegram"]["token"] == ""
    assert source["exchange"]["key"] == "public"
