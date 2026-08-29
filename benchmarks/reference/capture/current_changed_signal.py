"""Pinned Freqtrade interface capture for the current changed predicate contract."""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
from pathlib import Path

import freqtrade
import pandas as pd  # noqa: PANDAS_OK
from CurrentChangedPredicateContract import CurrentChangedPredicateContract
from freqtrade.strategy.interface import IStrategy
from nfi_backtest_engine.changed_signal_trust import official_capture_attestation

mode = sys.argv[1]
pair = "BTC/USDT:USDT" if mode == "futures" else "BTC/USDT"
frame = pd.DataFrame(
    {
        "date": pd.date_range("2026-01-01", periods=5, freq="5min", tz="UTC"),
        "open": [100.0] * 5,
        "high": [101.0] * 5,
        "low": [99.0] * 5,
        "close": [100.0] * 5,
        "volume": [1.0] * 5,
        "RSI_3_15m": [15.0, 15.000000000000002, 15.0, 15.0, 15.0],
        "RSI_3_1h": [20.0, 20.0, 20.000000000000004, 20.0, 20.0],
        "RSI_3_4h": [25.0, 25.0, 25.0, 25.000000000000004, 25.0],
        "AROONU_14_1h": [0.0, 0.0, 0.0, 0.0, 5e-324],
    }
)
strategy = CurrentChangedPredicateContract(
    {
        "runmode": "backtest",
        "trading_mode": mode,
        "timeframe": "5m",
        "stake_currency": "USDT",
        "dry_run": True,
    }
)
entry = strategy.advise_entry(frame.copy(), {"pair": pair})
result = strategy.advise_exit(entry.copy(), {"pair": pair})
interface_path = inspect.getsourcefile(IStrategy)
assert interface_path is not None
methods = {
    name: hashlib.sha256(inspect.getsource(getattr(IStrategy, name)).encode()).hexdigest()
    for name in ("advise_entry", "advise_exit")
}
print(
    json.dumps(
        {
            "freqtrade_version": freqtrade.__version__,
            "trading_mode": mode,
            "pair": pair,
            "interface_path": interface_path,
            "interface_sha256": hashlib.sha256(Path(interface_path).read_bytes()).hexdigest(),
            "method_sha256": methods,
            "call_order": ["advise_entry", "advise_exit"],
            "capture_contract": official_capture_attestation(Path(__file__), mode),
            "input": frame.to_json(
                orient="table", date_format="iso", double_precision=15
            ),
            "output": result.to_json(
                orient="table", date_format="iso", double_precision=15
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
