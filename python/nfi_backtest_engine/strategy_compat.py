"""Minimal Freqtrade compatibility surface for trusted X7 vector methods."""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .errors import StrategyAnalysisError


@dataclass(frozen=True)
class RunModeValue:
    value: str


class IStrategy:
    """Only the initialization contract used by NFI X7 vector preparation."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.dp: VectorDataProvider | None = None

    def populate_indicators(
        self,
        dataframe: pd.DataFrame,
        metadata: dict[str, Any],
    ) -> pd.DataFrame:
        raise NotImplementedError

    def populate_entry_trend(
        self,
        dataframe: pd.DataFrame,
        metadata: dict[str, Any],
    ) -> pd.DataFrame:
        raise NotImplementedError

    def populate_exit_trend(
        self,
        dataframe: pd.DataFrame,
        metadata: dict[str, Any],
    ) -> pd.DataFrame:
        raise NotImplementedError


class Trade:
    pass


class Order:
    pass


class VectorDataProvider:
    def __init__(
        self,
        frames: dict[tuple[str, str], pd.DataFrame],
        pairs: list[str],
        *,
        runmode: str = "backtest",
    ) -> None:
        self._frames = frames
        self._pairs = tuple(pairs)
        self.runmode = RunModeValue(runmode)

    def current_whitelist(self) -> list[str]:
        return list(self._pairs)

    def get_pair_dataframe(self, pair: str, timeframe: str) -> pd.DataFrame:
        try:
            frame = self._frames[(pair, timeframe)]
        except KeyError as exc:
            raise StrategyAnalysisError(
                f"vector worker is missing informative data for {pair} {timeframe}"
            ) from exc
        return frame.copy(deep=True)

    def get_analyzed_dataframe(self, pair: str, timeframe: str) -> tuple[pd.DataFrame, None]:
        return self.get_pair_dataframe(pair=pair, timeframe=timeframe), None


def install_freqtrade_shims() -> None:
    """Install deterministic modules before importing a trusted strategy source."""
    freqtrade = types.ModuleType("freqtrade")
    strategy = types.ModuleType("freqtrade.strategy")
    interface = types.ModuleType("freqtrade.strategy.interface")
    persistence = types.ModuleType("freqtrade.persistence")
    strategy.__dict__.update(
        IStrategy=IStrategy,
        merge_informative_pair=merge_informative_pair,
    )
    interface.__dict__["IStrategy"] = IStrategy
    persistence.__dict__.update(Trade=Trade, Order=Order)
    freqtrade.__dict__.update(strategy=strategy, persistence=persistence)
    sys.modules.update(
        {
            "freqtrade": freqtrade,
            "freqtrade.strategy": strategy,
            "freqtrade.strategy.interface": interface,
            "freqtrade.persistence": persistence,
        }
    )


def load_strategy_class(source: str | Path, class_name: str) -> type[IStrategy]:
    install_freqtrade_shims()
    path = Path(source).resolve()
    module_name = f"_nfi_strategy_{path.stem}_{abs(hash(str(path)))}"
    specification = importlib.util.spec_from_file_location(module_name, path)
    if specification is None or specification.loader is None:
        raise StrategyAnalysisError(f"cannot create strategy import spec: {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except Exception as exc:
        raise StrategyAnalysisError(f"strategy import failed: {path}: {exc}") from exc
    selected = getattr(module, class_name, None)
    if not isinstance(selected, type) or not issubclass(selected, IStrategy):
        raise StrategyAnalysisError(f"strategy class {class_name!r} is not an IStrategy in {path}")
    return selected


def prepare_worker_config(
    config: dict[str, Any],
    *,
    user_data_directory: str | Path,
) -> dict[str, Any]:
    prepared = _copy_json(config)
    exchange = prepared.setdefault("exchange", {})
    if not isinstance(exchange, dict):
        raise StrategyAnalysisError("strategy config exchange must be an object")
    exchange.setdefault("ccxt_config", {})
    exchange.setdefault("ccxt_async_config", {})
    prepared.setdefault("stake_currency", "USDT")
    prepared.setdefault("trading_mode", "spot")
    prepared.setdefault("margin_mode", "")
    prepared.setdefault("max_open_trades", 6)
    prepared.setdefault("dry_run_wallet", 1000.0)
    prepared["user_data_dir"] = Path(user_data_directory).resolve()
    prepared["runmode"] = RunModeValue("backtest")
    return prepared


def merge_informative_pair(
    dataframe: pd.DataFrame,
    informative: pd.DataFrame,
    timeframe: str,
    timeframe_inf: str,
    *,
    ffill: bool = False,
    append_timeframe: bool = True,
    date_column: str = "date",
) -> pd.DataFrame:
    """Align an informative candle to base candles without lookahead."""
    if date_column not in dataframe or date_column not in informative:
        raise StrategyAnalysisError("informative merge requires date columns")
    base_minutes = timeframe_minutes(timeframe)
    informative_minutes = timeframe_minutes(timeframe_inf)
    if informative_minutes < base_minutes:
        raise StrategyAnalysisError("informative timeframe cannot be smaller than base timeframe")
    prepared = informative.copy(deep=True)
    merge_column = "__nfi_merge_date"
    if informative_minutes == base_minutes:
        prepared[merge_column] = prepared[date_column]
    else:
        prepared[merge_column] = prepared[date_column] + pd.to_timedelta(
            informative_minutes - base_minutes,
            unit="m",
        )
    if append_timeframe:
        prepared.rename(
            columns={
                column: f"{column}_{timeframe_inf}"
                for column in prepared.columns
                if column != merge_column
            },
            inplace=True,
        )
    result = pd.merge_ordered(
        dataframe,
        prepared,
        left_on=date_column,
        right_on=merge_column,
        fill_method="ffill" if ffill else None,
        how="left",
    )
    result.drop(columns=[merge_column], inplace=True)
    return result


def timeframe_minutes(timeframe: str) -> int:
    units = {"m": 1, "h": 60, "d": 1440, "w": 10080}
    if len(timeframe) < 2 or timeframe[-1] not in units:
        raise StrategyAnalysisError(f"unsupported timeframe: {timeframe}")
    try:
        count = int(timeframe[:-1])
    except ValueError as exc:
        raise StrategyAnalysisError(f"unsupported timeframe: {timeframe}") from exc
    if count <= 0:
        raise StrategyAnalysisError(f"unsupported timeframe: {timeframe}")
    return count * units[timeframe[-1]]


def _copy_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value
