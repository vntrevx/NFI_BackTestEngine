from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import nfi_backtest_engine.binance_archive as archive
import polars as pl
import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError


def _timestamp(year: int, month: int, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


def _zip_csv(text: str, *, name: str = "candles.csv") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(name, text)
    return output.getvalue()


def _candle_zip(timestamp: int) -> bytes:
    return _zip_csv(f"{timestamp},1,2,0.5,1.5,9,0,0,0,0,0,0\n")


def _fetched(payload: bytes) -> tuple[bytes, str]:
    return payload, hashlib.sha256(payload).hexdigest()


def test_archive_frame_normalizes_microseconds_and_headers() -> None:
    payload = _zip_csv(
        "open_time,open,high,low,close,volume,close_time\n"
        "1759276800000000,1,2,0.5,1.5,9,1759276800000001\n"
    )

    frame = archive._archive_frame(payload, role="base", source="fixture")

    assert frame.schema == {
        "date": pl.Datetime(time_unit="ms", time_zone="UTC"),
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
    }
    assert frame.row(0, named=True) == {
        "date": datetime(2025, 10, 1, tzinfo=UTC),
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 9.0,
    }


def test_archive_frame_does_not_infer_integer_volume_from_prefix() -> None:
    payload = _zip_csv(
        "1759276800000,1,2,0.5,1.5,9,1759276800001,0,0,0,0,0\n"
        "1759277100000,1,2,0.5,1.5,153391.41,1759277100001,0,0,0,0,0\n"
    )

    frame = archive._archive_frame(payload, role="base", source="fixture")

    assert frame.get_column("volume").to_list() == [9.0, 153391.41]


def test_archive_frame_converts_funding_rate_shape() -> None:
    payload = _zip_csv(
        "calc_time,funding_interval_hours,last_funding_rate\n"
        "1759276800000,8,0.000031\n"
    )

    frame = archive._archive_frame(payload, role="funding", source="fixture")

    assert frame.row(0, named=True) == {
        "date": datetime(2025, 10, 1, tzinfo=UTC),
        "open": 0.000031,
        "high": 0.0,
        "low": 0.0,
        "close": 0.0,
        "volume": 0.0,
    }


def test_prepare_spot_archive_is_checksum_identified_and_resumable(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(url: str) -> tuple[bytes, str]:
        calls.append(url)
        month = 10 if "2025-10" in url else 11
        return _fetched(_candle_zip(_timestamp(2025, month)))

    kwargs = {
        "pairs": ["BTC/USDT"],
        "timeframes": ["1d"],
        "trading_mode": "spot",
        "coverage_start_timestamp_ms_by_timeframe": {"1d": _timestamp(2025, 10)},
        "end_timestamp_ms": _timestamp(2025, 12),
        "workers": 1,
        "fetch_archive": fetch,
    }
    first = archive.prepare_binance_archive_data(tmp_path, **kwargs)
    first_calls = list(calls)
    second = archive.prepare_binance_archive_data(tmp_path, **kwargs)

    assert len(first_calls) == 2
    assert calls == first_calls
    assert first["downloaded_archive_count"] == 2
    assert second["downloaded_archive_count"] == 0
    assert first["aggregate_sha256"] == second["aggregate_sha256"]
    assert first["series"][0]["archive_identity_sha256"] == (
        second["series"][0]["archive_identity_sha256"]
    )
    output = tmp_path / "BTC_USDT-1d.feather"
    assert pl.read_ipc(output).height == 2
    manifest = read_json(output.with_suffix(".feather.archive.json"))
    assert len(manifest["archives"]) == 2
    assert manifest["output_sha256"] == first["series"][0]["output_sha256"]


def test_prepare_futures_materializes_base_mark_and_funding_names(tmp_path: Path) -> None:
    def fetch(url: str) -> tuple[bytes, str]:
        if "fundingRate" in url:
            payload = _zip_csv(
                "calc_time,funding_interval_hours,last_funding_rate\n"
                f"{_timestamp(2025, 10)},8,0.00001\n"
            )
        else:
            payload = _candle_zip(_timestamp(2025, 10))
        return _fetched(payload)

    result = archive.prepare_binance_archive_data(
        tmp_path,
        pairs=["BTC/USDT:USDT"],
        timeframes=["1h"],
        trading_mode="futures",
        coverage_start_timestamp_ms_by_timeframe={"1h": _timestamp(2025, 10)},
        end_timestamp_ms=_timestamp(2025, 11),
        workers=1,
        fetch_archive=fetch,
    )

    assert result["series_count"] == 3
    assert {item["path"] for item in result["series"]} == {
        "futures/BTC_USDT_USDT-1h-futures.feather",
        "futures/BTC_USDT_USDT-1h-mark.feather",
        "futures/BTC_USDT_USDT-1h-funding_rate.feather",
    }


def test_leading_missing_archives_are_recorded_but_internal_gap_fails(
    tmp_path: Path,
) -> None:
    def leading_fetch(url: str) -> tuple[bytes, str] | None:
        if "2025-10" in url:
            return None
        return _fetched(_candle_zip(_timestamp(2025, 11)))

    result = archive.prepare_binance_archive_data(
        tmp_path / "leading",
        pairs=["NEW/USDT"],
        timeframes=["1d"],
        trading_mode="spot",
        coverage_start_timestamp_ms_by_timeframe={"1d": _timestamp(2025, 10)},
        end_timestamp_ms=_timestamp(2025, 12),
        workers=1,
        fetch_archive=leading_fetch,
    )
    assert result["missing_leading_archive_count"] == 1

    def internal_fetch(url: str) -> tuple[bytes, str] | None:
        if "2025-11" in url:
            return None
        return _fetched(_candle_zip(_timestamp(2025, 10)))

    with pytest.raises(BenchmarkError, match="internal missing month"):
        archive.prepare_binance_archive_data(
            tmp_path / "internal",
            pairs=["BTC/USDT"],
            timeframes=["1d"],
            trading_mode="spot",
            coverage_start_timestamp_ms_by_timeframe={"1d": _timestamp(2025, 10)},
            end_timestamp_ms=_timestamp(2025, 12),
            workers=1,
            fetch_archive=internal_fetch,
        )


def test_existing_cache_without_verified_manifest_fails_closed(tmp_path: Path) -> None:
    pl.DataFrame(
        {
            "date": [datetime(2025, 10, 1, tzinfo=UTC)],
            "open": [1.0],
            "high": [2.0],
            "low": [0.5],
            "close": [1.5],
            "volume": [9.0],
        }
    ).write_ipc(tmp_path / "BTC_USDT-1d.feather")

    with pytest.raises(BenchmarkError, match="lacks its source manifest"):
        archive.prepare_binance_archive_data(
            tmp_path,
            pairs=["BTC/USDT"],
            timeframes=["1d"],
            trading_mode="spot",
            coverage_start_timestamp_ms_by_timeframe={"1d": _timestamp(2025, 10)},
            end_timestamp_ms=_timestamp(2025, 11),
            workers=1,
            fetch_archive=lambda _url: None,
        )


def test_fetcher_cannot_inject_an_unverified_payload(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkError, match="fetch identity differs"):
        archive.prepare_binance_archive_data(
            tmp_path,
            pairs=["BTC/USDT"],
            timeframes=["1d"],
            trading_mode="spot",
            coverage_start_timestamp_ms_by_timeframe={"1d": _timestamp(2025, 10)},
            end_timestamp_ms=_timestamp(2025, 11),
            workers=1,
            fetch_archive=lambda _url: (_candle_zip(_timestamp(2025, 10)), "0" * 64),
        )


@pytest.mark.parametrize("pair", ["btc/USDT", "BTC-USDT", "BTC/USDT/EXTRA"])
def test_archive_pair_contract_rejects_noncanonical_symbols(
    tmp_path: Path,
    pair: str,
) -> None:
    with pytest.raises(SpecValidationError, match="pair is invalid"):
        archive.prepare_binance_archive_data(
            tmp_path,
            pairs=[pair],
            timeframes=["1d"],
            trading_mode="spot",
            coverage_start_timestamp_ms_by_timeframe={"1d": _timestamp(2025, 10)},
            end_timestamp_ms=_timestamp(2025, 11),
            workers=1,
            fetch_archive=lambda _url: None,
        )
