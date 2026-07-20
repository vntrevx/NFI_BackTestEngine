"""Coverage-aware Freqtrade candle preparation and immutable data seals."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from .canonical import read_json, write_json
from .config_loader import load_effective_config
from .docker_runtime import managed_docker_run
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file
from .reference_runtime import (
    REFERENCE_IMAGE_REF,
    REFERENCE_PLATFORM,
    REFERENCE_PLATFORM_DIGEST,
    ensure_docker_config,
    ensure_reference_image,
)
from .timerange import parse_timerange_milliseconds

DATA_SEAL_VERSION = "1.3.0"
LEGACY_DATA_SEAL_VERSION = "1.2.0"
_DATA_SUFFIXES = {".feather", ".parquet"}
_TIMEFRAME = re.compile(r"^(?P<count>[1-9]\d*)(?P<unit>[smhdwM])$")


def prepare_data(
    *,
    config_path: str | Path,
    data_directory: str | Path,
    timerange: str,
    timeframes: list[str],
    destination: str | Path,
    download_missing: bool = True,
    startup_candles: int = 0,
    require_startup_coverage: bool = False,
    history_coverage_policy: str = "strict",
) -> dict[str, Any]:
    """Check coverage, download only missing edges, then seal every input byte."""
    config_file = Path(config_path).resolve()
    data_root = Path(data_directory).resolve()
    loaded_config = load_effective_config(config_file)
    config = loaded_config["config"]
    request = _data_request(
        config,
        timerange,
        timeframes,
        startup_candles=startup_candles,
        require_startup_coverage=require_startup_coverage,
        history_coverage_policy=history_coverage_policy,
    )
    data_root.mkdir(parents=True, exist_ok=True)
    gaps = find_coverage_gaps(data_root, request)
    startup_shortfalls = find_startup_shortfalls(data_root, request)
    downloads: list[dict[str, Any]] = []
    blocking_gaps = _blocking_coverage_gaps(gaps, history_coverage_policy)
    if (
        blocking_gaps or (require_startup_coverage and startup_shortfalls)
    ) and not download_missing:
        if blocking_gaps:
            raise BenchmarkError(_gap_message(blocking_gaps))
        raise BenchmarkError(_startup_gap_message(startup_shortfalls))
    if download_missing and (
        gaps or (require_startup_coverage and startup_shortfalls)
    ):
        needs_append = any(gap["end_missing"] for gap in gaps)
        # A normal download against an absent file already requests the full
        # timerange. Prepending immediately afterwards would repeat the same
        # network work for every newly created pair. Prepend only files that
        # existed with a later start before this preparation began.
        needs_prepend = _needs_prepend(
            gaps,
            startup_shortfalls,
            require_startup_coverage=require_startup_coverage,
        )
        if needs_append:
            downloads.append(
                _download_data(
                    config_file=config_file,
                    data_root=data_root,
                    request=request,
                    prepend=False,
                )
            )
        if needs_prepend:
            downloads.append(
                _download_data(
                    config_file=config_file,
                    data_root=data_root,
                    request=request,
                    prepend=True,
                )
            )
        gaps = find_coverage_gaps(data_root, request)
        startup_shortfalls = find_startup_shortfalls(data_root, request)
        blocking_gaps = _blocking_coverage_gaps(gaps, history_coverage_policy)
        if blocking_gaps:
            raise BenchmarkError(
                "download completed but coverage is still incomplete: "
                f"{_gap_message(blocking_gaps)}"
            )
        if require_startup_coverage and startup_shortfalls:
            raise BenchmarkError(
                "download completed but startup coverage is still incomplete: "
                f"{_startup_gap_message(startup_shortfalls)}"
            )

    files = _seal_data_files(data_root, request=request)
    if not files:
        raise BenchmarkError(f"no Feather or Parquet candle files found under {data_root}")
    seal = {
        "schema_version": DATA_SEAL_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "reference": {
            "image": REFERENCE_IMAGE_REF.split("@", 1)[0],
            "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
            "platform": REFERENCE_PLATFORM,
        },
        "request": {
            **request,
            "config_path": str(config_file),
            "config_sha256": loaded_config["sha256"],
            "config_inputs": loaded_config["inputs"],
        },
        "data_root": str(data_root),
        "downloads": downloads,
        "coverage_shortfalls": gaps,
        "startup_shortfalls": startup_shortfalls,
        "files": files,
        "aggregate_sha256": _aggregate_files(files),
    }
    validate_data_seal_document(seal, source=Path(destination), verify_files=True)
    write_json(destination, seal)
    return seal


def _needs_prepend(
    gaps: list[dict[str, Any]],
    startup_shortfalls: list[dict[str, Any]],
    *,
    require_startup_coverage: bool,
) -> bool:
    return any(
        gap["start_missing"] and gap["available_start_timestamp_ms"] is not None
        for gap in gaps
    ) or (
        require_startup_coverage
        and any(
            item["available_start_timestamp_ms"] is not None
            for item in startup_shortfalls
        )
    )


def validate_data_seal(source: str | Path) -> dict[str, Any]:
    path = Path(source).resolve()
    seal = read_json(path)
    validate_data_seal_document(seal, source=path, verify_files=True)
    return seal


def validate_data_seal_document(
    seal: Any,
    *,
    source: Path,
    verify_files: bool,
) -> None:
    if not isinstance(seal, dict) or seal.get("schema_version") not in {
        DATA_SEAL_VERSION,
        LEGACY_DATA_SEAL_VERSION,
    }:
        raise SpecValidationError("unsupported or invalid data seal")
    version = seal["schema_version"]
    required = {
        "schema_version",
        "created_at",
        "reference",
        "request",
        "data_root",
        "downloads",
        "startup_shortfalls",
        "files",
        "aggregate_sha256",
    }
    if version == DATA_SEAL_VERSION:
        required.add("coverage_shortfalls")
    if set(seal) != required:
        raise SpecValidationError("data seal fields differ from the versioned contract")
    _validate_request_contract(seal["request"], version=version)
    if version == DATA_SEAL_VERSION and not isinstance(seal["coverage_shortfalls"], list):
        raise SpecValidationError("data seal coverage_shortfalls must be a list")
    if not isinstance(seal["startup_shortfalls"], list):
        raise SpecValidationError("data seal startup_shortfalls must be a list")
    files = seal["files"]
    if not isinstance(files, list) or not files:
        raise SpecValidationError("data seal files must be a non-empty list")
    if _aggregate_files(files) != seal["aggregate_sha256"]:
        raise SpecValidationError("data seal aggregate_sha256 is corrupt")
    if not verify_files:
        return
    root = Path(seal["data_root"]).resolve()
    for record in files:
        target = (root / record["path"]).resolve()
        if not target.is_relative_to(root):
            raise SpecValidationError(f"data seal path escapes root: {record['path']}")
        if not target.is_file():
            raise SpecValidationError(f"sealed data file is missing: {record['path']}")
        if target.stat().st_size != record["bytes"]:
            raise SpecValidationError(f"sealed data file size changed: {record['path']}")
        if sha256_file(target) != record["sha256"]:
            raise SpecValidationError(f"sealed data file hash changed: {record['path']}")
        coverage = _file_coverage(target)
        if coverage != record["coverage"]:
            raise SpecValidationError(f"sealed data file coverage changed: {record['path']}")
    current_shortfalls = find_startup_shortfalls(root, seal["request"])
    if current_shortfalls != seal["startup_shortfalls"]:
        raise SpecValidationError("sealed startup coverage changed")
    if version == DATA_SEAL_VERSION:
        current_gaps = find_coverage_gaps(root, seal["request"])
        if current_gaps != seal["coverage_shortfalls"]:
            raise SpecValidationError("sealed available-history coverage changed")
        blocking = _blocking_coverage_gaps(
            current_gaps,
            seal["request"]["history_coverage_policy"],
        )
        if blocking:
            raise SpecValidationError(_gap_message(blocking))


def _validate_request_contract(request: Any, *, version: str) -> None:
    required = {
        "exchange",
        "trading_mode",
        "pairs",
        "timeframes",
        "timerange",
        "start_timestamp_ms",
        "end_timestamp_ms",
        "startup_candles",
        "startup_coverage_policy",
        "coverage_start_timestamp_ms_by_timeframe",
        "download_timerange",
        "config_path",
        "config_sha256",
        "config_inputs",
    }
    if version == DATA_SEAL_VERSION:
        required.add("history_coverage_policy")
    if not isinstance(request, dict) or set(request) != required:
        raise SpecValidationError("data seal request fields differ from the v1.2 contract")
    timeframes = request["timeframes"]
    coverage_starts = request["coverage_start_timestamp_ms_by_timeframe"]
    if (
        not isinstance(timeframes, list)
        or not timeframes
        or not all(isinstance(value, str) and value for value in timeframes)
        or not isinstance(coverage_starts, dict)
        or set(coverage_starts) != set(timeframes)
    ):
        raise SpecValidationError("data seal startup coverage map is invalid")
    startup_candles = request["startup_candles"]
    start_ms = request["start_timestamp_ms"]
    end_ms = request["end_timestamp_ms"]
    if (
        not isinstance(startup_candles, int)
        or isinstance(startup_candles, bool)
        or startup_candles < 0
        or not isinstance(start_ms, int)
        or not isinstance(end_ms, int)
        or end_ms <= start_ms
    ):
        raise SpecValidationError("data seal timerange or startup count is invalid")
    expected_starts = {
        timeframe: start_ms - startup_candles * timeframe_milliseconds(timeframe)
        for timeframe in timeframes
    }
    if coverage_starts != expected_starts:
        raise SpecValidationError("data seal startup coverage boundaries are corrupt")
    earliest_start = min(expected_starts.values())
    if request["download_timerange"] != f"{earliest_start}-{end_ms}":
        raise SpecValidationError("data seal download timerange is corrupt")
    if request["startup_coverage_policy"] not in {"record", "require"}:
        raise SpecValidationError("data seal startup coverage policy is invalid")
    if version == DATA_SEAL_VERSION and request["history_coverage_policy"] not in {
        "strict",
        "available",
    }:
        raise SpecValidationError("data seal history coverage policy is invalid")


def find_coverage_gaps(data_root: Path, request: dict[str, Any]) -> list[dict[str, Any]]:
    start_ms = request["start_timestamp_ms"]
    end_ms = request["end_timestamp_ms"]
    gaps: list[dict[str, Any]] = []
    files = [path for path in data_root.rglob("*") if _is_data_file(path)]
    for pair in request["pairs"]:
        for timeframe in request["timeframes"]:
            candidates = [
                path
                for path in files
                if _matches_base_candles(path, pair, timeframe, request["trading_mode"])
            ]
            coverages = [_file_coverage(path) for path in candidates]
            earliest = min((item["start_timestamp_ms"] for item in coverages), default=None)
            latest = max((item["end_timestamp_ms"] for item in coverages), default=None)
            candle_ms = timeframe_milliseconds(timeframe)
            start_missing = earliest is None or earliest > start_ms
            end_missing = latest is None or latest + candle_ms < end_ms
            if start_missing or end_missing:
                gaps.append(
                    {
                        "pair": pair,
                        "timeframe": timeframe,
                        "start_missing": start_missing,
                        "end_missing": end_missing,
                        "available_start_timestamp_ms": earliest,
                        "available_end_timestamp_ms": latest,
                    }
                )
    return gaps


def find_startup_shortfalls(
    data_root: Path, request: dict[str, Any]
) -> list[dict[str, Any]]:
    """Record history Freqtrade requested but the local dataset cannot provide.

    Freqtrade allows this condition: base-timeframe execution moves forward,
    while informative frames simply contain fewer startup rows. Recording the
    shortfall preserves that behavior in the seal. Callers may opt into the
    stricter download/fail policy when constructing a new dataset.
    """
    coverage_starts = request["coverage_start_timestamp_ms_by_timeframe"]
    files = [path for path in data_root.rglob("*") if _is_data_file(path)]
    shortfalls: list[dict[str, Any]] = []
    for pair in request["pairs"]:
        for timeframe in request["timeframes"]:
            required_start = coverage_starts[timeframe]
            candidates = [
                path
                for path in files
                if _matches_base_candles(path, pair, timeframe, request["trading_mode"])
            ]
            coverages = [_file_coverage(path) for path in candidates]
            earliest = min(
                (item["start_timestamp_ms"] for item in coverages),
                default=None,
            )
            if earliest is not None and earliest <= required_start:
                continue
            candle_ms = timeframe_milliseconds(timeframe)
            missing_candles = (
                None
                if earliest is None
                else (earliest - required_start + candle_ms - 1) // candle_ms
            )
            shortfalls.append(
                {
                    "pair": pair,
                    "timeframe": timeframe,
                    "required_start_timestamp_ms": required_start,
                    "available_start_timestamp_ms": earliest,
                    "missing_candles": missing_candles,
                }
            )
    return shortfalls


def timeframe_milliseconds(timeframe: str) -> int:
    match = _TIMEFRAME.fullmatch(timeframe)
    if match is None:
        raise SpecValidationError(f"unsupported timeframe: {timeframe!r}")
    multipliers = {
        "s": 1000,
        "m": 60_000,
        "h": 3_600_000,
        "d": 86_400_000,
        "w": 7 * 86_400_000,
        "M": 30 * 86_400_000,
    }
    return int(match.group("count")) * multipliers[match.group("unit")]


def _data_request(
    config: dict[str, Any],
    timerange: str,
    timeframes: list[str],
    *,
    startup_candles: int,
    require_startup_coverage: bool,
    history_coverage_policy: str,
) -> dict[str, Any]:
    try:
        start_ms, end_ms = parse_timerange_milliseconds(timerange)
    except ValueError as exc:
        raise SpecValidationError(
            "data timerange must use closed YYYYMMDD, Unix-second, or Unix-millisecond boundaries"
        ) from exc
    if not timeframes:
        configured = config.get("timeframe")
        if not isinstance(configured, str) or not configured:
            raise SpecValidationError("at least one timeframe is required")
        timeframes = [configured]
    normalized_timeframes = list(dict.fromkeys(timeframes))
    for timeframe in normalized_timeframes:
        timeframe_milliseconds(timeframe)
    if (
        not isinstance(startup_candles, int)
        or isinstance(startup_candles, bool)
        or startup_candles < 0
    ):
        raise SpecValidationError("startup_candles must be a non-negative integer")
    if history_coverage_policy not in {"strict", "available"}:
        raise SpecValidationError(
            "history_coverage_policy must be 'strict' or 'available'"
        )
    exchange = config.get("exchange")
    if not isinstance(exchange, dict):
        raise SpecValidationError("config.exchange must be an object")
    pairs = exchange.get("pair_whitelist")
    if (
        not isinstance(pairs, list)
        or not pairs
        or not all(isinstance(pair, str) and pair for pair in pairs)
    ):
        raise SpecValidationError("config.exchange.pair_whitelist must contain pairs")
    trading_mode = config.get("trading_mode", "spot")
    if trading_mode not in {"spot", "futures"}:
        raise SpecValidationError("data preparation supports spot or futures")
    if end_ms <= start_ms:
        raise SpecValidationError("data timerange end must be after start")
    coverage_starts = {
        timeframe: start_ms - startup_candles * timeframe_milliseconds(timeframe)
        for timeframe in normalized_timeframes
    }
    earliest_start = min(coverage_starts.values(), default=start_ms)
    return {
        "exchange": str(exchange.get("name", "")).lower(),
        "trading_mode": trading_mode,
        "pairs": list(dict.fromkeys(pairs)),
        "timeframes": normalized_timeframes,
        "timerange": timerange,
        "start_timestamp_ms": start_ms,
        "end_timestamp_ms": end_ms,
        "startup_candles": startup_candles,
        "startup_coverage_policy": (
            "require" if require_startup_coverage else "record"
        ),
        "history_coverage_policy": history_coverage_policy,
        "coverage_start_timestamp_ms_by_timeframe": coverage_starts,
        "download_timerange": f"{earliest_start}-{end_ms}",
    }


def _download_data(
    *,
    config_file: Path,
    data_root: Path,
    request: dict[str, Any],
    prepend: bool,
) -> dict[str, Any]:
    docker_config = ensure_docker_config()
    ensure_reference_image(docker_config=docker_config)
    with tempfile.TemporaryDirectory(prefix="nfi-data-") as temporary:
        user_data = Path(temporary) / "user_data"
        user_data.mkdir()
        with managed_docker_run(
            docker_config=docker_config,
            role="data-download",
        ) as lease:
            command = [
                *lease["command_prefix"],
                "--platform",
                REFERENCE_PLATFORM,
                "--volume",
                f"{config_file}:/input/config.json:ro",
                "--volume",
                f"{data_root}:/data",
                "--volume",
                f"{user_data}:/work/user_data",
                REFERENCE_IMAGE_REF,
                "download-data",
                "--config",
                "/input/config.json",
                "--userdir",
                "/work/user_data",
                "--datadir",
                "/data",
                "--timerange",
                request["download_timerange"],
                "--timeframes",
                *request["timeframes"],
                "--pairs",
                *request["pairs"],
                "--trading-mode",
                request["trading_mode"],
                "--data-format-ohlcv",
                "feather",
            ]
            if prepend:
                command.append("--prepend")
            completed = subprocess.run(
                command,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
    if completed.returncode != 0:
        raise BenchmarkError(
            "Freqtrade data download failed: "
            f"{completed.stderr[-2000:].strip() or completed.stdout[-2000:].strip()}"
        )
    return {
        "mode": "prepend" if prepend else "append",
        "exit_code": completed.returncode,
        "command_sha256": hashlib.sha256(
            json.dumps(command, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _seal_data_files(
    data_root: Path,
    *,
    request: dict[str, Any],
) -> list[dict[str, Any]]:
    records = []
    for path in sorted(
        (
            path
            for path in data_root.rglob("*")
            if _is_data_file(path)
            and any(
                _matches_base_candles(
                    path,
                    pair,
                    timeframe,
                    request["trading_mode"],
                )
                or (
                    request["trading_mode"] == "futures"
                    and _matches_futures_funding_input(path, pair)
                )
                for pair in request["pairs"]
                for timeframe in request["timeframes"]
            )
        ),
        key=lambda item: item.relative_to(data_root).as_posix(),
    ):
        records.append(
            {
                "path": path.relative_to(data_root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "coverage": _file_coverage(path),
            }
        )
    return records


def _file_coverage(path: Path) -> dict[str, int]:
    try:
        if path.suffix.lower() == ".feather":
            frame = pl.read_ipc(path, columns=["date"], memory_map=False, rechunk=False)
        else:
            frame = pl.read_parquet(path, columns=["date"], rechunk=False)
    except Exception as exc:
        raise SpecValidationError(f"cannot read candle dates from {path}: {exc}") from exc
    if frame.height == 0:
        raise SpecValidationError(f"candle file is empty: {path}")
    dates = frame.get_column("date")
    raw_minimum = dates.cast(pl.Int64).min()
    raw_maximum = dates.cast(pl.Int64).max()
    if not isinstance(raw_minimum, int) or not isinstance(raw_maximum, int):
        raise SpecValidationError(f"candle date column is not datetime: {path}")
    time_unit = getattr(dates.dtype, "time_unit", None)
    if time_unit == "ns":
        minimum_ms, maximum_ms = raw_minimum // 1_000_000, raw_maximum // 1_000_000
    elif time_unit == "us":
        minimum_ms, maximum_ms = raw_minimum // 1_000, raw_maximum // 1_000
    elif time_unit == "ms":
        minimum_ms, maximum_ms = raw_minimum, raw_maximum
    elif dates.dtype == pl.Date:
        minimum_ms = raw_minimum * 86_400_000
        maximum_ms = raw_maximum * 86_400_000
    else:
        raise SpecValidationError(f"candle date column is not datetime: {path}")
    return {
        "rows": frame.height,
        "start_timestamp_ms": minimum_ms,
        "end_timestamp_ms": maximum_ms,
    }


def _matches_base_candles(path: Path, pair: str, timeframe: str, trading_mode: str) -> bool:
    normalized = pair.replace("/", "_").replace(":", "_")
    stem = path.stem
    prefix = f"{normalized}-{timeframe}"
    if not stem.startswith(prefix):
        return False
    if any(token in stem for token in ("funding_rate", "-mark", "-index", "premiumIndex")):
        return False
    if trading_mode == "futures":
        return stem == f"{prefix}-futures"
    return stem in {prefix, f"{prefix}-spot"}


def _matches_futures_funding_input(path: Path, pair: str) -> bool:
    """Match the exact Binance files consumed by the funding event merger."""
    normalized = pair.replace("/", "_").replace(":", "_")
    return path.stem in {
        f"{normalized}-1h-funding_rate",
        f"{normalized}-1h-mark",
    }


def _is_data_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in _DATA_SUFFIXES


def _aggregate_files(files: list[dict[str, Any]]) -> str:
    identity = [
        {
            "path": record["path"],
            "bytes": record["bytes"],
            "sha256": record["sha256"],
            "coverage": record["coverage"],
        }
        for record in files
    ]
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _gap_message(gaps: list[dict[str, Any]]) -> str:
    rendered = ", ".join(
        f"{gap['pair']} {gap['timeframe']}"
        f" (start_missing={gap['start_missing']}, end_missing={gap['end_missing']})"
        for gap in gaps
    )
    return f"candle coverage is incomplete: {rendered}"


def _blocking_coverage_gaps(
    gaps: list[dict[str, Any]],
    policy: str,
) -> list[dict[str, Any]]:
    """Return gaps that invalidate the selected history contract.

    ``available`` accepts only a leading shortfall for a pair that has real
    candles and reaches the requested end. That is the observable shape of an
    asset listed after a portfolio timerange began. Missing pairs and stale
    tails still fail, so the policy cannot hide a failed or partial download.
    """
    if policy == "strict":
        return gaps
    if policy != "available":
        raise SpecValidationError(f"unsupported history coverage policy: {policy!r}")
    return [
        gap
        for gap in gaps
        if gap["available_start_timestamp_ms"] is None or gap["end_missing"]
    ]


def _startup_gap_message(shortfalls: list[dict[str, Any]]) -> str:
    rendered = ", ".join(
        f"{item['pair']} {item['timeframe']}"
        f" (missing_candles={item['missing_candles']})"
        for item in shortfalls
    )
    return f"startup candle coverage is incomplete: {rendered}"
