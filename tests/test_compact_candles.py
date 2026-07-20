from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
from nfi_backtest_engine.data_seal import compact_candle_directory


def _write_candles(path: Path, *, minutes: int, rows: int) -> None:
    start = datetime(2024, 12, 31, tzinfo=UTC)
    frame = pl.DataFrame(
        {
            "date": [start + timedelta(minutes=minutes * index) for index in range(rows)],
            "open": [float(index) for index in range(rows)],
            "high": [float(index + 1) for index in range(rows)],
            "low": [float(index) for index in range(rows)],
            "close": [float(index + 1) for index in range(rows)],
            "volume": [1.0 for _ in range(rows)],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_ipc(path, compression="uncompressed")


def test_compact_candles_keeps_per_timeframe_startup_windows(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_candles(source / "BTC_USDT-5m.feather", minutes=5, rows=600)
    _write_candles(source / "BTC_USDT-1h.feather", minutes=60, rows=49)
    destination = tmp_path / "compact"

    report = compact_candle_directory(
        source,
        destination,
        pairs=["BTC/USDT"],
        timeframes=["5m", "1h"],
        trading_mode="spot",
        timerange="20250101-20250102",
        startup_candles=3,
    )

    five_minute = pl.read_ipc(destination / "BTC_USDT-5m.feather")
    hourly = pl.read_ipc(destination / "BTC_USDT-1h.feather")
    assert five_minute.height == 3 + 24 * 12 + 1
    assert hourly.height == 3 + 24 + 1
    assert five_minute.get_column("open").to_list() == list(
        range(24 * 12 - 3, 24 * 12 + 24 * 12 + 1)
    )
    assert hourly.get_column("open").to_list() == list(range(21, 49))
    assert [record["path"] for record in report["files"]] == [
        "BTC_USDT-1h.feather",
        "BTC_USDT-5m.feather",
    ]
