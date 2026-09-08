"""Run the pinned StrategyResolver on the unchanged full donor for configuration precedence."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import tempfile
from pathlib import Path

import freqtrade
from freqtrade.enums import RunMode
from freqtrade.exceptions import OperationalException
from freqtrade.resolvers.strategy_resolver import StrategyResolver

logging.disable(logging.CRITICAL)
source = Path(
    "benchmarks/fixtures/captured/x8-original-futures-20220401-20220420-r1/inputs/strategy.py"
)
cases_file = Path(__file__).with_name("resolver-cases.json")
document = json.loads(cases_file.read_text())
results = []
with tempfile.TemporaryDirectory() as temporary:
    for case in document["cases"]:
        config = {
            "strategy": "NostalgiaForInfinityX8",
            "strategy_path": str(source.parent.resolve()),
            "user_data_dir": Path(temporary),
            "exchange": {"name": "binance"},
            "stake_currency": "USDT",
            "stake_amount": 100,
            "max_open_trades": 1,
            "trading_mode": "spot",
            "runmode": RunMode.BACKTEST,
            "timeframe": "5m",
            **copy.deepcopy(case["config"]),
        }
        try:
            strategy = StrategyResolver.load_strategy(config)
        except OperationalException as error:
            assert case.get("expected_error") in str(error), (case, str(error))
            results.append(
                {"config": case["config"], "rejected": True, "error": str(error), "equal": True}
            )
            continue
        assert "expected_error" not in case, case
        actual = {key: getattr(strategy, key) for key in case["expected"]}
        # JSON normalizes minute keys after the resolver's int-key ROI sorting.
        actual = json.loads(json.dumps(actual))
        assert actual == case["expected"], (case, actual)
        results.append({"config": case["config"], "effective": actual, "equal": True})
print(
    json.dumps(
        {
            "freqtrade_version": freqtrade.__version__,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "cases_sha256": hashlib.sha256(cases_file.read_bytes()).hexdigest(),
            "cases": results,
        },
        indent=2,
    )
)
