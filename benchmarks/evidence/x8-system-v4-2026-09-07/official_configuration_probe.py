"""Exercise the unchanged donor constructor's parameter updates in the pinned image.

The base initializer stores config and the profit-cache object is an inert stub;
this isolates source parameter precedence from filesystem and exchange setup.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import freqtrade

SOURCE = Path("benchmarks/fixtures/source-contracts/x8-system-v4/configuration.source")


class Base:
    def __init__(self, config):
        self.config = config


class Cache:
    def __init__(self, path):
        self.path = path

    def save(self):
        pass


cases = [
    {
        "nfi_parameters": {
            "system_v4_bad_trade_exit_enable": False,
            "bad_trade_controller_stale_min_age_days": 10,
            "futures_mode_leverage": 2,
        },
        "futures_mode_leverage": 4,
    },
    {
        "nfi_parameters": {
            "long_entry_signal_params": {"long_entry_condition_1_enable": False, "unknown": True}
        },
        "long_entry_signal_params": {
            "long_entry_condition_1_enable": True,
            "long_entry_condition_2_enable": False,
        },
    },
    {"nfi_parameters": []},
    {"nfi_parameters": {"unknown": 12}},
    {"nfi_parameters": {"stops_enable": False}, "stops_enable": True},
]
results = []
for config_values in cases:
    cls = next(
        node for node in ast.parse(SOURCE.read_text()).body if isinstance(node, ast.ClassDef)
    )
    cls.bases = [ast.Name(id="Base", ctx=ast.Load())]
    module = ast.Module(body=[cls], type_ignores=[])
    namespace = {"Base": Base, "Cache": Cache, "log": logging.getLogger("configuration-probe")}
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)
    config = dict(
        exchange={"name": "binance"},
        stake_currency="USDT",
        user_data_dir=Path("/tmp"),
        runmode=SimpleNamespace(value="backtest"),
    )
    config.update(config_values)
    instance = namespace[cls.name](config)
    keys = (
        "system_v4_bad_trade_exit_enable",
        "bad_trade_controller_stale_min_age_days",
        "futures_mode_leverage",
        "stops_enable",
        "long_entry_signal_params",
    )
    results.append(
        {"config": config_values, "effective": {key: getattr(instance, key) for key in keys}}
    )
print(
    json.dumps(
        {
            "freqtrade_version": freqtrade.__version__,
            "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
            "scope": __doc__,
            "cases": results,
        },
        indent=2,
    )
)
