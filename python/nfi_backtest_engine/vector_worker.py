"""One trusted X7 vector-calculation worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .canonical import read_json, write_json
from .errors import StrategyAnalysisError
from .fixture import sha256_file
from .resource_usage import process_peak_rss_bytes
from .runtime_versions import vector_dependency_versions
from .strategy_compat import (
    VectorDataProvider,
    load_strategy_class,
    prepare_worker_config,
    timeframe_minutes,
)
from .timerange import parse_timerange_milliseconds

VECTOR_REQUEST_VERSION = "1.2.0"
VECTOR_OUTPUT_VERSION = "1.8.0"


@dataclass(frozen=True)
class PreparedExecutionFrame:
    """Analyzed rows plus the first row visible to the trading loop.

    Rows before ``execution_start_index`` exist only because strategy
    callbacks can read earlier analyzed candles. They must never emit orders
    or observer events.
    """

    frame: pd.DataFrame
    execution_start_index: int


def run_vector_request(request: dict[str, Any]) -> dict[str, Any]:
    _validate_request(request)
    output = Path(request["output_path"]).resolve()
    metadata_path = output.with_suffix(f"{output.suffix}.json")
    if output.exists() or metadata_path.exists():
        raise StrategyAnalysisError(f"vector output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    started_ns = time.perf_counter_ns()
    raw_frames = {
        _split_frame_key(key): _read_frame(Path(path)) for key, path in request["frames"].items()
    }
    frames = _bound_indicator_frames(
        raw_frames,
        request["timerange"],
        startup_candles=request["startup_candles"],
    )
    strategy_class = load_strategy_class(
        request["strategy_path"],
        request["class_name"],
    )
    config = prepare_worker_config(
        request["config"],
        user_data_directory=output.parent / "worker-user-data",
    )
    strategy = strategy_class(config)
    provider = VectorDataProvider(frames, request["pairs"])
    strategy.dp = provider
    pair = request["pair"]
    timeframe = request["base_timeframe"]
    try:
        base = frames[(pair, timeframe)].copy(deep=True)
    except KeyError as exc:
        raise StrategyAnalysisError(f"base candle frame is missing for {pair} {timeframe}") from exc
    metadata = {"pair": pair}
    indicators = strategy.populate_indicators(base, metadata)
    entries = strategy.populate_entry_trend(indicators, metadata)
    signals = strategy.populate_exit_trend(entries, metadata)
    _validate_output_frame(signals)
    prepared = _prepare_execution_frame(
        signals,
        request["timerange"],
        startup_candles=request["startup_candles"],
    )
    prepared = PreparedExecutionFrame(
        _attach_funding_events(prepared.frame, request["funding_data"]),
        prepared.execution_start_index,
    )
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    prepared.frame.reset_index(drop=True).to_feather(temporary)
    temporary.replace(output)
    record = {
        "schema_version": VECTOR_OUTPUT_VERSION,
        "pair": pair,
        "base_timeframe": timeframe,
        "timerange": request["timerange"],
        "rows": len(prepared.frame),
        "execution_start_index": prepared.execution_start_index,
        "context_rows": prepared.execution_start_index,
        "columns": [str(column) for column in prepared.frame.columns],
        "signal_counts": _signal_counts(
            prepared.frame.iloc[prepared.execution_start_index :]
        ),
        "bytes": output.stat().st_size,
        "sha256": sha256_file(output),
        "wall_time_seconds": (time.perf_counter_ns() - started_ns) / 1_000_000_000,
        "peak_rss_bytes": process_peak_rss_bytes(),
        "strategy_sha256": request["strategy_sha256"],
        "config_sha256": request["config_sha256"],
        "input_sha256": _request_input_sha256(request),
    }
    write_json(metadata_path, record)
    return record


def _signal_counts(frame: pd.DataFrame) -> dict[str, int]:
    result: dict[str, int] = {}
    for column in ("enter_long", "enter_short", "exit_long", "exit_short"):
        execution_column = f"nfi_exec_{column}"
        if execution_column not in frame:
            result[column] = 0
            continue
        raw = frame[execution_column]
        if not isinstance(raw, pd.Series):
            raise StrategyAnalysisError(f"vector signal column is not one-dimensional: {column}")
        values = pd.to_numeric(raw, errors="coerce")
        if not isinstance(values, pd.Series):
            raise StrategyAnalysisError(f"vector signal conversion failed: {column}")
        values = values.fillna(0)
        result[column] = int(values.ne(0).sum())
    return result


def _materialize_execution_signals(frame: pd.DataFrame) -> pd.DataFrame:
    """Shift decision-candle signals onto the next candle's executable open."""
    result = frame.copy(deep=False)
    for column in ("enter_long", "enter_short", "exit_long", "exit_short"):
        if column not in result:
            result[f"nfi_exec_{column}"] = 0
            continue
        raw = result[column]
        if not isinstance(raw, pd.Series):
            raise StrategyAnalysisError(f"vector signal column is not one-dimensional: {column}")
        numeric = pd.to_numeric(raw, errors="coerce")
        if not isinstance(numeric, pd.Series):
            raise StrategyAnalysisError(f"vector signal conversion failed: {column}")
        result[f"nfi_exec_{column}"] = numeric.fillna(0).shift(1, fill_value=0)
    for column in ("enter_tag", "exit_tag"):
        if column in result:
            raw = result[column]
            if not isinstance(raw, pd.Series):
                raise StrategyAnalysisError(
                    f"vector tag column is not one-dimensional: {column}"
                )
            shifted = raw.shift(1).astype("object")
            result[f"nfi_exec_{column}"] = shifted.where(shifted.notna(), None)
    return result


def _prepare_execution_frame(
    frame: pd.DataFrame,
    timerange: str,
    *,
    startup_candles: int,
) -> PreparedExecutionFrame:
    """Apply Freqtrade's signal-to-execution shift before range trimming.

    Freqtrade analyzes startup candles before entering the chronological
    backtest loop, trims that analyzed frame, shifts signals inside the
    trimmed frame, and drops its first row. A decision before the effective
    trim boundary must therefore not execute, while a decision on the first
    trimmed candle executes on the following open.

    The startup prefix also remains available to callbacks. X7 position
    management reads ``previous_candle`` through ``previous_candle_5`` after a
    trade opens. Keeping the full available startup prefix avoids coupling this
    generic vector stage to the current callback compiler's maximum lookback.
    """
    executable = _materialize_execution_signals(frame)
    start_index, stop_index = _execution_positions(
        executable,
        timerange,
        startup_candles=startup_candles,
    )
    if start_index is None or stop_index is None:
        return PreparedExecutionFrame(executable.iloc[0:0].copy(), 0)

    # Freqtrade shifts signals only after trimming and then drops the first
    # trimmed row. Because the full-frame shift is identical from the second
    # trimmed row onward, retaining that first row as callback context gives
    # the simulator the same executable arrays and previous-candle view.
    first_executable_index = start_index + 1
    if first_executable_index > stop_index:
        return PreparedExecutionFrame(executable.iloc[0:0].copy(), 0)
    context_rows = min(startup_candles, start_index)
    context_start = start_index - context_rows
    selected = executable.iloc[context_start : stop_index + 1].copy()
    execution_start_index = first_executable_index - context_start
    return PreparedExecutionFrame(selected, execution_start_index)


def _read_frame(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise StrategyAnalysisError(f"candle input does not exist: {path}")
    if path.suffix.lower() == ".feather":
        frame = pd.read_feather(path)
    elif path.suffix.lower() in {".parquet", ".pq"}:
        frame = pd.read_parquet(path)
    else:
        raise StrategyAnalysisError(f"unsupported candle format: {path}")
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        missing = ", ".join(sorted(required - set(frame.columns)))
        raise StrategyAnalysisError(f"candle input {path} is missing columns: {missing}")
    frame.sort_values("date", kind="stable", inplace=True)
    frame.drop_duplicates("date", keep="last", inplace=True)
    frame.reset_index(drop=True, inplace=True)
    return frame


def _attach_funding_events(
    frame: pd.DataFrame,
    funding_data: dict[str, Any] | None,
) -> pd.DataFrame:
    """Attach Freqtrade funding events without forward-filling them.

    Freqtrade 2026.5.1 performs an inner join on the sparse 1h funding-rate
    candles and 1h mark candles, then charges `amount * open_fund *
    open_mark` only at matching timestamps. Materializing those two scalars
    on the base candle keeps Rust's chronological loop allocation-free and
    preserves the official order: funding, position adjustment, then exit.
    """
    result = frame.copy(deep=False)
    result["nfi_exec_funding_rate"] = float("nan")
    result["nfi_exec_funding_mark_price"] = float("nan")
    if funding_data is None or frame.empty:
        return result

    funding = _read_frame(Path(funding_data["funding_rate_path"]))
    mark = _read_frame(Path(funding_data["mark_path"]))
    events = mark[["date", "open"]].merge(
        funding[["date", "open"]],
        on="date",
        how="inner",
        suffixes=("_mark", "_fund"),
        validate="one_to_one",
    )
    if events.empty:
        return result
    rates = pd.Series(
        events["open_fund"].to_numpy(),
        index=pd.to_datetime(events["date"], utc=True),
    )
    marks = pd.Series(
        events["open_mark"].to_numpy(),
        index=pd.to_datetime(events["date"], utc=True),
    )
    dates = pd.to_datetime(result["date"], utc=True)
    result["nfi_exec_funding_rate"] = dates.map(rates)
    result["nfi_exec_funding_mark_price"] = dates.map(marks)
    return result


def _bound_indicator_frames(
    frames: dict[tuple[str, str], pd.DataFrame],
    timerange: str,
    *,
    startup_candles: int,
) -> dict[tuple[str, str], pd.DataFrame]:
    """Expose exactly the historical window Freqtrade loads per timeframe.

    Freqtrade subtracts ``startup_candle_count`` candles independently for
    every requested timeframe. Feeding an entire on-disk history to a strategy
    is observably different for cumulative indicators such as OBV: even a
    percentage change can flip sign when its cumulative baseline changes.
    Bounding before ``populate_indicators`` therefore belongs to the exactness
    contract, not merely to memory optimization.
    """

    if startup_candles < 0:
        raise StrategyAnalysisError("startup candle count cannot be negative")
    start_ms, stop_ms = parse_timerange_milliseconds(timerange)
    start = pd.to_datetime(start_ms, unit="ms", utc=True)
    stop = pd.to_datetime(stop_ms, unit="ms", utc=True)
    bounded: dict[tuple[str, str], pd.DataFrame] = {}
    for key, frame in frames.items():
        _, timeframe = key
        load_start = start - pd.to_timedelta(
            startup_candles * timeframe_minutes(timeframe),
            unit="m",
        )
        dates = pd.to_datetime(frame["date"], utc=True)
        selected = frame.loc[(dates >= load_start) & (dates <= stop)].copy()
        selected.reset_index(drop=True, inplace=True)
        bounded[key] = selected
    return bounded


def _trim_timerange(
    frame: pd.DataFrame,
    timerange: str,
    *,
    startup_candles: int,
) -> pd.DataFrame:
    """Return only rows processed by Freqtrade's chronological loop."""
    start_index, stop_index = _execution_positions(
        frame,
        timerange,
        startup_candles=startup_candles,
    )
    if start_index is None or stop_index is None:
        return frame.iloc[0:0].copy()
    return frame.iloc[start_index : stop_index + 1].copy()


def _execution_positions(
    frame: pd.DataFrame,
    timerange: str,
    *,
    startup_candles: int,
) -> tuple[int | None, int | None]:
    """Locate the inclusive rows processed by the official backtest loop."""
    if startup_candles < 0:
        raise StrategyAnalysisError("startup candle count cannot be negative")
    start, end = timerange.split("-", 1)
    start_time = _timerange_boundary(start)
    end_time = _timerange_boundary(end)
    if start_time > end_time:
        raise StrategyAnalysisError(f"timerange start is after its stop boundary: {timerange}")
    dates = pd.to_datetime(frame["date"], utc=True)
    available_before_start = int((dates < start_time).sum())
    missing_startup = max(0, startup_candles - available_before_start)
    # Freqtrade's `trim_dataframe()` uses an inclusive stop boundary. The
    # boundary candle is processed for already-open trades with new entries
    # disabled, and `handle_left_open()` then uses that same row's timestamp
    # and open price for force exits. Dropping it changes both close time and
    # close rate even when every strategy callback is otherwise exact.
    positions = [
        index
        for index, selected in enumerate((dates >= start_time) & (dates <= end_time))
        if selected
    ]
    if not positions or missing_startup >= len(positions):
        return None, None
    return positions[missing_startup], positions[-1]


def _timerange_boundary(value: str) -> pd.Timestamp:
    """Parse the three closed-boundary forms accepted by Freqtrade.

    Ten-digit values are Unix seconds and thirteen-digit values are Unix
    milliseconds. Keeping this contract beside vector trimming prevents a
    mid-day official fixture from being silently rounded to a calendar day.
    Open-ended ranges remain outside the research-vector contract because an
    immutable data seal needs both coverage boundaries.
    """
    try:
        milliseconds, _ = parse_timerange_milliseconds(f"{value}-{value}")
        return pd.to_datetime(milliseconds, unit="ms", utc=True)
    except (OverflowError, ValueError) as exc:
        raise StrategyAnalysisError(f"invalid timerange boundary: {value!r}") from exc


def _validate_output_frame(frame: Any) -> None:
    if not isinstance(frame, pd.DataFrame):
        raise StrategyAnalysisError("strategy vector methods must return a pandas DataFrame")
    required = {"date", "open", "high", "low", "close", "volume", "enter_long"}
    missing = required - set(frame.columns)
    if missing:
        raise StrategyAnalysisError(
            f"strategy vector output is missing columns: {', '.join(sorted(missing))}"
        )
    if not frame["date"].is_monotonic_increasing:
        raise StrategyAnalysisError("strategy vector output dates are not chronological")


def _request_input_sha256(request: dict[str, Any]) -> str:
    identity = {
        "strategy_sha256": request["strategy_sha256"],
        "config_sha256": request["config_sha256"],
        "pair": request["pair"],
        "base_timeframe": request["base_timeframe"],
        "startup_candles": request["startup_candles"],
        "timeframes": request["timeframes"],
        "timerange": request["timerange"],
        "frames": request["frame_sha256"],
        "funding_data": (
            None
            if request["funding_data"] is None
            else {
                "funding_rate_sha256": request["funding_data"]["funding_rate_sha256"],
                "mark_sha256": request["funding_data"]["mark_sha256"],
            }
        ),
        "runtime_versions": request["runtime_versions"],
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _split_frame_key(value: str) -> tuple[str, str]:
    pair, separator, timeframe = value.rpartition("|")
    if not separator or not pair or not timeframe:
        raise StrategyAnalysisError(f"invalid vector frame key: {value}")
    return pair, timeframe


def _validate_request(request: Any) -> None:
    if not isinstance(request, dict) or request.get("schema_version") != VECTOR_REQUEST_VERSION:
        raise StrategyAnalysisError("unsupported vector request")
    required = {
        "schema_version",
        "strategy_path",
        "strategy_sha256",
        "class_name",
        "config",
        "config_sha256",
        "pair",
        "pairs",
        "base_timeframe",
        "startup_candles",
        "timeframes",
        "timerange",
        "frames",
        "frame_sha256",
        "funding_data",
        "runtime_versions",
        "output_path",
    }
    if set(request) != required:
        raise StrategyAnalysisError("vector request fields differ from v1")
    if request["runtime_versions"] != vector_dependency_versions():
        raise StrategyAnalysisError("vector worker dependency versions differ from its request")
    if not isinstance(request["frames"], dict) or not request["frames"]:
        raise StrategyAnalysisError("vector request requires candle frames")
    funding_data = request["funding_data"]
    trading_mode = request["config"].get("trading_mode", "spot")
    if trading_mode == "futures":
        required_funding_fields = {
            "funding_rate_path",
            "funding_rate_sha256",
            "mark_path",
            "mark_sha256",
        }
        if not isinstance(funding_data, dict) or set(funding_data) != required_funding_fields:
            raise StrategyAnalysisError("futures vector request requires sealed funding data")
    elif funding_data is not None:
        raise StrategyAnalysisError("spot vector request cannot contain funding data")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m nfi_backtest_engine.vector_worker")
    parser.add_argument("request", type=Path)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    request = read_json(args.request)
    report = run_vector_request(request)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
