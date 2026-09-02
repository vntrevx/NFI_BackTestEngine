"""Verified Binance Public Data ingestion for completed-month discovery windows."""

from __future__ import annotations

import hashlib
import io
import re
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from .canonical import read_json, write_json
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file

BINANCE_ARCHIVE_VERSION = "1.0.0"
BINANCE_ARCHIVE_BASE = "https://data.binance.vision/data"
_SHA256 = re.compile(r"^(?P<digest>[0-9a-f]{64})(?:\s+\*?.+)?$")
_SYMBOL = re.compile(r"^[A-Z0-9]+$")
_TIMEFRAME = re.compile(r"^[1-9]\d*[smhdwM]$")
_CANDLE_COLUMNS = ("date", "open", "high", "low", "close", "volume")
_MAX_CHECKSUM_BYTES = 4096
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_CSV_BYTES = 256 * 1024 * 1024
_DOWNLOAD_ATTEMPTS = 3

ArchiveFetcher = Callable[[str], tuple[bytes, str] | None]


def prepare_binance_archive_data(
    data_directory: str | Path,
    *,
    pairs: list[str],
    timeframes: list[str],
    trading_mode: str,
    coverage_start_timestamp_ms_by_timeframe: Mapping[str, int],
    end_timestamp_ms: int,
    workers: int = 8,
    fetch_archive: ArchiveFetcher | None = None,
) -> dict[str, Any]:
    """Materialize Freqtrade-shaped Feather files from checksum-verified archives."""
    root = Path(data_directory).resolve()
    if trading_mode not in {"spot", "futures"}:
        raise SpecValidationError("Binance archive mode must be spot or futures")
    if not pairs or len(pairs) != len(set(pairs)) or not all(
        isinstance(pair, str) and pair for pair in pairs
    ):
        raise SpecValidationError("Binance archive pairs must be unique strings")
    invalid_pair = next(
        (pair for pair in pairs if not _valid_pair(pair, trading_mode)),
        None,
    )
    if invalid_pair is not None:
        raise SpecValidationError(f"Binance archive pair is invalid: {invalid_pair}")
    if (
        not timeframes
        or len(timeframes) != len(set(timeframes))
        or not all(
            isinstance(value, str) and _TIMEFRAME.fullmatch(value)
            for value in timeframes
        )
        or set(coverage_start_timestamp_ms_by_timeframe) != set(timeframes)
    ):
        raise SpecValidationError("Binance archive timeframe coverage map differs")
    starts = list(coverage_start_timestamp_ms_by_timeframe.values())
    if (
        not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in starts
        )
        or not isinstance(end_timestamp_ms, int)
        or isinstance(end_timestamp_ms, bool)
        or end_timestamp_ms <= min(starts)
        or workers <= 0
    ):
        raise SpecValidationError("Binance archive bounds or worker count is invalid")
    if _month_floor(end_timestamp_ms) != end_timestamp_ms:
        raise SpecValidationError(
            "Binance archive discovery requires an exclusive calendar-month boundary"
        )

    root.mkdir(parents=True, exist_ok=True)
    fetch = fetch_archive or _fetch_verified_archive
    jobs = [
        (pair, timeframe, "base", int(coverage_start_timestamp_ms_by_timeframe[timeframe]))
        for pair in pairs
        for timeframe in timeframes
    ]
    if trading_mode == "futures":
        side_start = min(starts)
        jobs.extend(
            (pair, "1h", role, side_start)
            for pair in pairs
            for role in ("mark", "funding")
        )

    def execute(job: tuple[str, str, str, int]) -> dict[str, Any]:
        pair, timeframe, role, start_ms = job
        return _materialize_series(
            root,
            pair=pair,
            timeframe=timeframe,
            role=role,
            trading_mode=trading_mode,
            start_timestamp_ms=start_ms,
            end_timestamp_ms=end_timestamp_ms,
            fetch_archive=fetch,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        series = list(pool.map(execute, jobs))
    series.sort(key=lambda item: str(item["path"]))
    aggregate = hashlib.sha256()
    for item in series:
        aggregate.update(str(item["path"]).encode())
        aggregate.update(str(item["archive_identity_sha256"]).encode())
        aggregate.update(str(item["output_sha256"]).encode())
    return {
        "schema_version": BINANCE_ARCHIVE_VERSION,
        "source": BINANCE_ARCHIVE_BASE,
        "trading_mode": trading_mode,
        "pairs": pairs,
        "timeframes": timeframes,
        "end_timestamp_ms": end_timestamp_ms,
        "series_count": len(series),
        "downloaded_archive_count": sum(int(item["downloaded_archives"]) for item in series),
        "missing_leading_archive_count": sum(
            int(item["missing_leading_archives"]) for item in series
        ),
        "aggregate_sha256": aggregate.hexdigest(),
        "series": series,
    }


def _materialize_series(
    root: Path,
    *,
    pair: str,
    timeframe: str,
    role: str,
    trading_mode: str,
    start_timestamp_ms: int,
    end_timestamp_ms: int,
    fetch_archive: ArchiveFetcher,
) -> dict[str, Any]:
    destination = _destination(root, pair, timeframe, role, trading_mode)
    manifest_path = destination.with_suffix(destination.suffix + ".archive.json")
    existing, prior_records = _read_existing(
        destination,
        root=root,
        manifest_path=manifest_path,
        pair=pair,
        timeframe=timeframe,
        role=role,
        trading_mode=trading_mode,
    )
    months = _required_months(
        start_timestamp_ms,
        end_timestamp_ms,
        records=prior_records,
    )
    frames: list[pl.DataFrame] = [existing] if existing is not None else []
    records = list(prior_records)
    downloaded = 0
    for year, month in months:
        url = _archive_url(
            pair,
            timeframe=timeframe,
            role=role,
            trading_mode=trading_mode,
            year=year,
            month=month,
        )
        fetched = fetch_archive(url)
        if fetched is None:
            records.append({"url": url, "sha256": None})
            continue
        payload, digest = fetched
        if (
            not isinstance(payload, bytes)
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or hashlib.sha256(payload).hexdigest() != digest
        ):
            raise BenchmarkError(f"Binance archive fetch identity differs: {url}")
        frame = _archive_frame(payload, role=role, source=url)
        if not frame.height:
            raise BenchmarkError(f"Binance archive contains no rows: {url}")
        frames.append(frame)
        records.append({"url": url, "sha256": digest})
        downloaded += 1
    records.sort(key=lambda item: str(item["url"]))
    _validate_archive_sequence(records, source=destination)
    missing_total = sum(record["sha256"] is None for record in records)
    if not frames:
        _write_series_manifest(
            manifest_path,
            destination=destination,
            root=root,
            pair=pair,
            timeframe=timeframe,
            role=role,
            trading_mode=trading_mode,
            records=records,
            output_sha256=None,
        )
        return {
            "path": destination.relative_to(root).as_posix(),
            "rows": 0,
            "downloaded_archives": downloaded,
            "missing_leading_archives": missing_total,
            "archive_identity_sha256": _archive_identity(records),
            "output_sha256": None,
        }
    combined = pl.concat(frames, how="vertical_relaxed").sort("date")
    _validate_duplicate_rows(combined, source=destination)
    combined = combined.unique(subset=["date"], keep="first", maintain_order=True)
    if combined.height == 0:
        raise BenchmarkError(f"Binance archive series is empty after filtering: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    combined.write_ipc(temporary, compression="zstd")
    temporary.replace(destination)
    output_sha256 = sha256_file(destination)
    _write_series_manifest(
        manifest_path,
        destination=destination,
        root=root,
        pair=pair,
        timeframe=timeframe,
        role=role,
        trading_mode=trading_mode,
        records=records,
        output_sha256=output_sha256,
    )
    return {
        "path": destination.relative_to(root).as_posix(),
        "rows": combined.height,
        "downloaded_archives": downloaded,
        "missing_leading_archives": missing_total,
        "archive_identity_sha256": _archive_identity(records),
        "output_sha256": output_sha256,
    }


def _fetch_verified_archive(url: str) -> tuple[bytes, str] | None:
    checksum = _download(url + ".CHECKSUM", max_bytes=_MAX_CHECKSUM_BYTES, missing_ok=True)
    if checksum is None:
        return None
    try:
        line = checksum.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise BenchmarkError(f"Binance archive checksum is not ASCII: {url}") from exc
    match = _SHA256.fullmatch(line)
    if match is None:
        raise BenchmarkError(f"Binance archive checksum is invalid: {url}")
    expected = match.group("digest")
    payload = _download(url, max_bytes=_MAX_ARCHIVE_BYTES, missing_ok=False)
    assert payload is not None
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise BenchmarkError(f"Binance archive SHA-256 differs: {url}")
    return payload, actual


def _download(url: str, *, max_bytes: int, missing_ok: bool) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": "nfi-backtest-engine/1"})
    last_error: Exception | None = None
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        declared_length = int(length)
                    except ValueError as exc:
                        raise BenchmarkError(
                            f"Binance archive Content-Length is invalid: {url}"
                        ) from exc
                    if declared_length < 0 or declared_length > max_bytes:
                        raise BenchmarkError(f"Binance archive response exceeds limit: {url}")
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise BenchmarkError(f"Binance archive response exceeds limit: {url}")
                return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return None
            if exc.code < 500 and exc.code != 429:
                raise BenchmarkError(f"Binance archive HTTP {exc.code}: {url}") from exc
            last_error = exc
        except OSError as exc:
            last_error = exc
        if attempt + 1 < _DOWNLOAD_ATTEMPTS:
            time.sleep(2**attempt)
    raise BenchmarkError(f"Binance archive download failed: {url}: {last_error}")


def _archive_frame(payload: bytes, *, role: str, source: str) -> pl.DataFrame:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if (
                len(members) != 1
                or "/" in members[0].filename
                or "\\" in members[0].filename
                or members[0].file_size > _MAX_CSV_BYTES
            ):
                raise BenchmarkError(f"Binance archive inventory is unsafe: {source}")
            if archive.testzip() is not None:
                raise BenchmarkError(f"Binance archive CRC differs: {source}")
            csv_payload = archive.read(members[0])
    except zipfile.BadZipFile as exc:
        raise BenchmarkError(f"Binance archive ZIP is invalid: {source}") from exc
    if len(csv_payload) > _MAX_CSV_BYTES:
        raise BenchmarkError(f"Binance archive CSV exceeds limit: {source}")
    first = csv_payload.lstrip()[:1]
    has_header = bool(first and not first.isdigit())
    try:
        raw = pl.read_csv(io.BytesIO(csv_payload), has_header=has_header)
    except Exception as exc:
        raise BenchmarkError(f"Binance archive CSV is invalid: {source}: {exc}") from exc
    if raw.width < 2 or (role != "funding" and raw.width < 6):
        raise BenchmarkError(f"Binance archive CSV columns differ: {source}")
    timestamp = raw.get_column(raw.columns[0]).cast(pl.Int64, strict=True)
    timestamp = (
        timestamp.to_frame("date")
        .select(
            pl.when(pl.col("date") > 10**13)
            .then(pl.col("date") // 1000)
            .otherwise(pl.col("date"))
        )
        .to_series()
    )
    if role == "funding":
        rate_column = "last_funding_rate" if "last_funding_rate" in raw.columns else raw.columns[-1]
        frame = pl.DataFrame(
            {
                "date": timestamp,
                "open": raw.get_column(rate_column).cast(pl.Float64, strict=True),
                "high": 0.0,
                "low": 0.0,
                "close": 0.0,
                "volume": 0.0,
            }
        )
    else:
        frame = pl.DataFrame(
            {
                "date": timestamp,
                **{
                    name: raw.get_column(raw.columns[index]).cast(pl.Float64, strict=True)
                    for index, name in enumerate(_CANDLE_COLUMNS[1:], start=1)
                },
            }
        )
    frame = frame.with_columns(
        pl.col("date").cast(pl.Datetime(time_unit="ms", time_zone="UTC"))
    )
    if any(not frame.get_column(name).is_finite().all() for name in _CANDLE_COLUMNS[1:]):
        raise BenchmarkError(f"Binance archive contains non-finite candles: {source}")
    return frame


def _validate_duplicate_rows(frame: pl.DataFrame, *, source: Path) -> None:
    duplicated = frame.filter(pl.col("date").is_duplicated())
    if duplicated.height == 0:
        return
    conflicts = duplicated.group_by("date").agg(
        [pl.col(name).n_unique().alias(name) for name in _CANDLE_COLUMNS[1:]]
    )
    if any((conflicts.get_column(name) > 1).any() for name in _CANDLE_COLUMNS[1:]):
        raise BenchmarkError(f"Binance archive overlaps with changed candle rows: {source}")


def _read_existing(
    path: Path,
    *,
    root: Path,
    manifest_path: Path,
    pair: str,
    timeframe: str,
    role: str,
    trading_mode: str,
) -> tuple[pl.DataFrame | None, list[dict[str, str | None]]]:
    if not manifest_path.is_file():
        if path.exists():
            raise BenchmarkError(
                f"existing Binance archive cache lacks its source manifest: {path}"
            )
        return None, []
    manifest = read_json(manifest_path)
    expected_identity = {
        "pair": pair,
        "timeframe": timeframe,
        "role": role,
        "trading_mode": trading_mode,
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "schema_version",
            "source",
            "path",
            "relative_path",
            "pair",
            "timeframe",
            "role",
            "trading_mode",
            "archives",
            "archive_identity_sha256",
            "output_sha256",
        }
        or manifest.get("schema_version") != BINANCE_ARCHIVE_VERSION
        or manifest.get("source") != BINANCE_ARCHIVE_BASE
        or any(manifest.get(key) != value for key, value in expected_identity.items())
        or manifest.get("path") != path.name
        or manifest.get("relative_path") != path.relative_to(root).as_posix()
    ):
        raise BenchmarkError(f"existing Binance archive manifest identity differs: {path}")
    records = _archive_records(manifest.get("archives"), source=manifest_path)
    for record in records:
        year, month = _record_month(record)
        if record["url"] != _archive_url(
            pair,
            timeframe=timeframe,
            role=role,
            trading_mode=trading_mode,
            year=year,
            month=month,
        ):
            raise BenchmarkError(f"existing Binance archive manifest URL differs: {path}")
    if manifest.get("archive_identity_sha256") != _archive_identity(records):
        raise BenchmarkError(f"existing Binance archive identity SHA-256 differs: {path}")
    output_sha256 = manifest.get("output_sha256")
    if output_sha256 is None:
        if path.exists():
            raise BenchmarkError(f"empty Binance archive manifest has a data file: {path}")
        return None, records
    if (
        not isinstance(output_sha256, str)
        or not path.is_file()
        or sha256_file(path) != output_sha256
    ):
        raise BenchmarkError(f"existing Binance archive output SHA-256 differs: {path}")
    try:
        frame = pl.read_ipc(path, memory_map=False, rechunk=False)
    except Exception as exc:
        raise BenchmarkError(f"existing Binance archive cache is unreadable: {path}") from exc
    if tuple(frame.columns) != _CANDLE_COLUMNS or frame.height == 0:
        raise BenchmarkError(f"existing Binance archive cache schema differs: {path}")
    if frame.schema["date"] != pl.Datetime(time_unit="ms", time_zone="UTC"):
        raise BenchmarkError(f"existing Binance archive cache date type differs: {path}")
    _validate_archive_sequence(records, source=path)
    return frame, records


def _required_months(
    start_ms: int,
    end_ms: int,
    *,
    records: list[dict[str, str | None]],
) -> list[tuple[int, int]]:
    months = list(_months(start_ms, end_ms))
    covered = {_record_month(item) for item in records}
    return [month for month in months if month not in covered]


def _archive_records(value: Any, *, source: Path) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        raise BenchmarkError(f"Binance archive manifest records differ: {source}")
    records: list[dict[str, str | None]] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"url", "sha256"}
            or not isinstance(item.get("url"), str)
            or (
                item.get("sha256") is not None
                and (
                    not isinstance(item.get("sha256"), str)
                    or _SHA256.fullmatch(str(item["sha256"])) is None
                )
            )
        ):
            raise BenchmarkError(f"Binance archive manifest record differs: {source}")
        records.append({"url": str(item["url"]), "sha256": item.get("sha256")})
    if len({_record_month(item) for item in records}) != len(records):
        raise BenchmarkError(f"Binance archive manifest has duplicate months: {source}")
    return records


def _record_month(record: Mapping[str, str | None]) -> tuple[int, int]:
    url = record["url"]
    assert isinstance(url, str)
    match = re.search(r"-(\d{4})-(\d{2})\.zip$", url)
    if match is None:
        raise BenchmarkError(f"Binance archive manifest URL differs: {url}")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise BenchmarkError(f"Binance archive manifest month differs: {url}")
    return year, month


def _validate_archive_sequence(
    records: list[dict[str, str | None]],
    *,
    source: Path,
) -> None:
    seen_data = False
    for record in sorted(records, key=_record_month):
        if record["sha256"] is None:
            if seen_data:
                raise BenchmarkError(
                    f"Binance archive has an internal missing month: {source}"
                )
        else:
            seen_data = True


def _write_series_manifest(
    manifest_path: Path,
    *,
    destination: Path,
    root: Path,
    pair: str,
    timeframe: str,
    role: str,
    trading_mode: str,
    records: list[dict[str, str | None]],
    output_sha256: str | None,
) -> None:
    document = {
        "schema_version": BINANCE_ARCHIVE_VERSION,
        "source": BINANCE_ARCHIVE_BASE,
        "path": destination.name,
        "relative_path": destination.relative_to(root).as_posix(),
        "pair": pair,
        "timeframe": timeframe,
        "role": role,
        "trading_mode": trading_mode,
        "archives": records,
        "archive_identity_sha256": _archive_identity(records),
        "output_sha256": output_sha256,
    }
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    write_json(temporary, document)
    temporary.replace(manifest_path)


def _months(start_ms: int, end_ms: int):
    current = datetime.fromtimestamp(_month_floor(start_ms) / 1000, tz=UTC)
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    while current < end:
        yield current.year, current.month
        current = _next_month(current)


def _archive_url(
    pair: str,
    *,
    timeframe: str,
    role: str,
    trading_mode: str,
    year: int,
    month: int,
) -> str:
    symbol = _pair_symbol(pair)
    stamp = f"{year:04d}-{month:02d}"
    if role == "funding":
        return (
            f"{BINANCE_ARCHIVE_BASE}/futures/um/monthly/fundingRate/{symbol}/"
            f"{symbol}-fundingRate-{stamp}.zip"
        )
    market = "spot" if trading_mode == "spot" else "futures/um"
    category = "markPriceKlines" if role == "mark" else "klines"
    return (
        f"{BINANCE_ARCHIVE_BASE}/{market}/monthly/{category}/{symbol}/{timeframe}/"
        f"{symbol}-{timeframe}-{stamp}.zip"
    )


def _destination(
    root: Path,
    pair: str,
    timeframe: str,
    role: str,
    trading_mode: str,
) -> Path:
    normalized = pair.replace("/", "_").replace(":", "_")
    directory = root / "futures" if trading_mode == "futures" else root
    suffix = (
        "funding_rate"
        if role == "funding"
        else "mark"
        if role == "mark"
        else "futures"
        if trading_mode == "futures"
        else None
    )
    stem = f"{normalized}-{timeframe}" + (f"-{suffix}" if suffix is not None else "")
    return directory / f"{stem}.feather"


def _pair_symbol(pair: str) -> str:
    base, separator, quote = pair.partition("/")
    symbol = base + quote.partition(":")[0] if separator else ""
    if not _SYMBOL.fullmatch(symbol):
        raise SpecValidationError(f"Binance archive pair is invalid: {pair}")
    return symbol


def _valid_pair(pair: str, trading_mode: str) -> bool:
    match = re.fullmatch(
        r"(?P<base>[A-Z0-9]+)/(?P<quote>[A-Z0-9]+)(?::(?P<settle>[A-Z0-9]+))?",
        pair,
    )
    if match is None:
        return False
    settle = match.group("settle")
    return (
        settle is None
        if trading_mode == "spot"
        else settle == match.group("quote")
    )


def _month_floor(timestamp_ms: int) -> int:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    return int(datetime(value.year, value.month, 1, tzinfo=UTC).timestamp() * 1000)


def _next_month(value: datetime) -> datetime:
    return datetime(
        value.year + (value.month == 12),
        1 if value.month == 12 else value.month + 1,
        1,
        tzinfo=UTC,
    )


def _archive_identity(records: list[dict[str, str | None]]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=_record_month):
        digest.update(str(record["url"]).encode())
        digest.update(str(record["sha256"]).encode())
    return digest.hexdigest()
