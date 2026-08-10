"""Minimal Freqtrade compatibility surface for trusted X7 vector methods."""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ccxt
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
    ffill: bool = True,
    append_timeframe: bool = True,
    date_column: str = "date",
    suffix: str | None = None,
) -> pd.DataFrame:
    """Mirror Freqtrade's informative merge without exposing future candles."""
    base_minutes = timeframe_minutes(timeframe)
    informative_minutes = timeframe_minutes(timeframe_inf)
    prepared = informative.copy(deep=True)
    merge_column = "date_merge"
    if informative_minutes == base_minutes:
        prepared[merge_column] = prepared[date_column]
    elif base_minutes < informative_minutes:
        if prepared.empty:
            prepared[merge_column] = prepared[date_column]
        elif timeframe_inf == "1M":
            prepared[merge_column] = (
                prepared[date_column] + pd.offsets.MonthBegin(1)
            ) - pd.to_timedelta(base_minutes, "m")
        else:
            prepared[merge_column] = (
                prepared[date_column]
                + pd.to_timedelta(informative_minutes, "m")
                - pd.to_timedelta(base_minutes, "m")
            )
    else:
        raise ValueError(
            "Tried to merge a faster timeframe to a slower timeframe."
            "This would create new rows, and can throw off your regular indicators."
        )

    if suffix and append_timeframe:
        raise ValueError("You can not specify `append_timeframe` as True and a `suffix`.")
    if append_timeframe:
        merge_column = f"date_merge_{timeframe_inf}"
        prepared.columns = [f"{column}_{timeframe_inf}" for column in prepared.columns]
    elif suffix:
        merge_column = f"date_merge_{suffix}"
        prepared.columns = [f"{column}_{suffix}" for column in prepared.columns]

    if not ffill:
        result = pd.merge(
            dataframe,
            prepared,
            left_on="date",
            right_on=merge_column,
            how="left",
        )
        return result.drop(merge_column, axis=1)

    result = pd.merge_ordered(
        dataframe,
        prepared,
        fill_method="ffill",
        left_on="date",
        right_on=merge_column,
        how="left",
    )
    if len(result) > 1 and len(prepared) > 0 and pd.isnull(result.at[0, merge_column]):
        first_valid_index = result[merge_column].first_valid_index()
        if isinstance(first_valid_index, int) and first_valid_index > 0:
            first_valid_date = result.at[first_valid_index, merge_column]
            historical = prepared[prepared[merge_column] < first_valid_date]
            if not historical.empty:
                result.loc[: first_valid_index - 1] = result.loc[
                    : first_valid_index - 1
                ].fillna(historical.iloc[-1])
    return result.drop(merge_column, axis=1)


def timeframe_minutes(timeframe: str) -> int:
    return timeframe_seconds(timeframe) // 60


def timeframe_seconds(timeframe: str) -> int:
    """Use the same CCXT timeframe parser as pinned Freqtrade."""
    try:
        return ccxt.Exchange.parse_timeframe(timeframe)
    except (TypeError, ValueError) as exc:
        raise StrategyAnalysisError(f"unsupported timeframe: {timeframe}") from exc


def _copy_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value
