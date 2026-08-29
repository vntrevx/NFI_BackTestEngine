from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from nfi_backtest_engine.callback_contract import JsonObject
from nfi_backtest_engine.callback_lowering import (
    CALLBACK_LOWERING_VERSION,
    lower_strategy_callbacks,
)
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.hot_ir import build_hot_callback_ir
from nfi_backtest_engine.strategy_ir import analyze_strategy

_CALLBACK_COMPONENTS = (
    "callback_ast",
    "callback_confirm",
    "callback_confirm_calls",
    "callback_confirm_expression",
    "callback_contract",
    "callback_exit_confirm",
    "callback_lifecycle",
    "callback_leverage",
    "callback_order_state",
    "callback_order_state_values",
    "callback_scalar",
    "callback_source_identity",
    "callback_source_ir",
    "callback_source_reads",
    "callback_source_routes",
    "callback_source_tags",
    "callback_stake",
    "callback_stake_expression",
    "callback_timeout",
    "callback_timeout_tags",
)


def _pure_lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _analysis(path: Path, source: str, class_name: str) -> JsonObject:
    path.write_text(source, encoding="utf-8")
    return analyze_strategy(path, class_name=class_name)


def test_callback_components_are_focused_and_below_size_gate() -> None:
    package = Path(__file__).parents[1] / "python/nfi_backtest_engine"

    paths = [package / "callback_lowering.py"] + [
        package / f"{name}.py" for name in _CALLBACK_COMPONENTS
    ]

    assert all(path.is_file() for path in paths)
    assert all(len(_pure_lines(path)) <= 250 for path in paths)
    assert all("SIZE_OK" not in path.read_text(encoding="utf-8") for path in paths)


def test_facade_preserves_version_order_source_location_and_determinism(
    tmp_path: Path,
) -> None:
    analysis = _analysis(
        tmp_path / "Callbacks.py",
        """from freqtrade.strategy import IStrategy
class Callbacks(IStrategy):
    timeframe = '5m'
    def bot_loop_start(self, current_time, **kwargs):
        if self.config['runmode'].value not in ('live', 'dry_run'):
            return super().bot_loop_start(current_time, **kwargs)
    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        return 'take_profit' if current_profit > 0.1 else None
""",
        "Callbacks",
    )

    first = lower_strategy_callbacks(analysis, run_mode="backtest")
    second = lower_strategy_callbacks(analysis, run_mode="backtest")
    canonical = json.dumps(first, sort_keys=True, separators=(",", ":"))

    assert CALLBACK_LOWERING_VERSION == "1.10.0"
    assert list(first) == ["bot_loop_start", "custom_exit"]
    assert first["bot_loop_start"]["proof"]["first_statement_line"] == 5
    assert first == second
    assert hashlib.sha256(canonical.encode()).hexdigest() == (
        "dc24148b914d5a082041a529d7dbc84ee3118d15c3cb191f18f7da6fa5f5a2e1"
    )


def test_near_miss_remains_uncompiled(tmp_path: Path) -> None:
    analysis = _analysis(
        tmp_path / "NearMiss.py",
        """from freqtrade.strategy import IStrategy
class NearMiss(IStrategy):
    timeframe = '5m'
    def bot_loop_start(self, current_time, **kwargs):
        if self.config['runmode'].value not in ('live', 'dry_run'):
            self.mutate_state()
            return super().bot_loop_start(current_time, **kwargs)
""",
        "NearMiss",
    )

    assert lower_strategy_callbacks(analysis, run_mode="backtest") == {}
    assert build_hot_callback_ir(analysis, run_mode="backtest")["blockers"] == [
        {
            "code": "STRATEGY_CALLBACK_NOT_COMPILED",
            "callback": "bot_loop_start",
            "message": "bot_loop_start() has a typed contract but no exact Rust lowering",
        }
    ]


def test_source_hash_mismatch_preserves_exact_diagnostic(tmp_path: Path) -> None:
    source = tmp_path / "Changed.py"
    analysis = _analysis(
        source,
        """from freqtrade.strategy import IStrategy
class Changed(IStrategy):
    timeframe = '5m'
    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        return None
""",
        "Changed",
    )
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(
        StrategyAnalysisError,
        match="^callback lowering source hash differs from analysis$",
    ):
        lower_strategy_callbacks(analysis, run_mode="backtest")
