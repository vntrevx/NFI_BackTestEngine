from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError
from nfi_backtest_engine.probe_capture import (
    _candle_role,
    _load_probe_spec,
    _require_native_surface_coverage,
)


def _spec() -> dict:
    return {
        "schema_version": "1.0.0",
        "fixture": {
            "id": "probe",
            "description": "probe",
            "probe_kind": "tag-121",
            "required_coverage": {},
        },
        "upstream": {
            "repository": "https://github.com/example/project",
            "commit": "a" * 40,
        },
        "strategy": {
            "source": "strategy.py",
            "class_name": "X7",
        },
        "config": {
            "source": "config.json",
            "overrides": {},
            "remove_paths": [],
        },
        "data": {
            "directory": "data",
            "timerange": "20250101-20250102",
            "pairs": ["BTC/USDT"],
        },
        "markets": {
            "engine": "engine-markets.json",
            "reference": "reference-markets.json",
        },
        "execution": {
            "profile": "execution-profile.json",
            "audit_timestamps_ms": [],
        },
    }


def test_probe_spec_rejects_duplicate_pairs(tmp_path: Path) -> None:
    document = _spec()
    document["data"]["pairs"] = ["BTC/USDT", "BTC/USDT"]
    path = tmp_path / "probe.json"
    write_json(path, document)

    with pytest.raises(SpecValidationError, match="unique CCXT"):
        _load_probe_spec(path)


def test_probe_spec_rejects_unbound_boolean_toggle_fields(tmp_path: Path) -> None:
    document = _spec()
    document["strategy"]["boolean_toggles"] = [
        {
            "mapping": "flags",
            "key": "route",
            "from": False,
            "to": True,
        }
    ]
    path = tmp_path / "probe.json"
    write_json(path, document)

    with pytest.raises(SpecValidationError, match="toggle 0 fields"):
        _load_probe_spec(path)


def test_probe_data_roles_keep_futures_side_inputs_separate() -> None:
    assert _candle_role(Path("BTC_USDT_USDT-1h-funding_rate.feather")) == (
        "funding_candles"
    )
    assert _candle_role(Path("BTC_USDT_USDT-1h-mark.feather")) == "mark_candles"
    assert _candle_role(Path("BTC_USDT_USDT-5m-futures.feather")) == "candles"


def test_native_surface_coverage_rejects_missing_trade_branch() -> None:
    required = {
        "entry_tags": ["121"],
        "compound_tags": [],
        "exit_reasons": [],
        "sides": ["long"],
        "minimum_distinct_leverages": 1,
    }
    surface = {
        "trades": [
            {
                "entry_tag": "120 ",
                "exit_reason": "force_exit",
                "direction": "long",
                "leverage": "1",
            }
        ]
    }

    with pytest.raises(BenchmarkError, match="entry_tags:121"):
        _require_native_surface_coverage(required, surface)


def test_native_surface_coverage_accepts_compound_and_variable_leverage() -> None:
    required = {
        "entry_tags": ["121", "122"],
        "compound_tags": ["121 122"],
        "exit_reasons": ["force_exit"],
        "sides": ["long", "short"],
        "minimum_distinct_leverages": 2,
    }
    surface = {
        "trades": [
            {
                "entry_tag": "121 122 ",
                "exit_reason": "force_exit",
                "direction": "long",
                "leverage": "1",
            },
            {
                "entry_tag": "122 ",
                "exit_reason": "force_exit",
                "direction": "short",
                "leverage": "3",
            },
        ]
    }

    _require_native_surface_coverage(required, surface)
