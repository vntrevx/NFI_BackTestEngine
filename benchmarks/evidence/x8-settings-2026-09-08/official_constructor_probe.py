"""Compare static overrides with the unchanged full donor and pinned IStrategy base."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import logging
import sys
import tempfile
from pathlib import Path

import freqtrade
from freqtrade.enums import RunMode

SOURCE = Path(
    "benchmarks/fixtures/captured/x8-original-futures-20220401-20220420-r1/inputs/strategy.py"
)
CASES = Path("benchmarks/evidence/x8-settings-2026-09-08/constructor-cases.json")
logging.disable(logging.CRITICAL)
spec = importlib.util.spec_from_file_location("nfi_settings_donor", SOURCE)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
strategy = module.NostalgiaForInfinityX8
cases = json.loads(CASES.read_text())
assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == cases["source_sha256"]
results = []
with tempfile.TemporaryDirectory() as temporary:
    for case in cases["cases"]:
        # The constructor mutates signal maps in place; isolate those class-owned
        # containers exactly as loading a fresh strategy class would.
        cls = type(
            "IsolatedSettingsProbe",
            (strategy,),
            {
                key: copy.deepcopy(getattr(strategy, key))
                for key in ("long_entry_signal_params", "short_entry_signal_params")
            },
        )
        config = {
            "exchange": {"name": "binance"},
            "stake_currency": "USDT",
            "user_data_dir": Path(temporary),
            "runmode": RunMode.BACKTEST,
            "timeframe": "5m",
            **copy.deepcopy(case["config"]),
        }
        instance = cls(config)
        actual = getattr(instance, case["key"])
        assert actual == case["expected"], (case["key"], actual, case["expected"])
        results.append({"key": case["key"], "effective": actual, "equal": True})
print(
    json.dumps(
        {
            "freqtrade_version": freqtrade.__version__,
            "source_sha256": cases["source_sha256"],
            "cases_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
            "scope": __doc__,
            "cases": results,
        },
        indent=2,
    )
)
