"""Coverage-aware Freqtrade candle preparation and immutable data seals."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from .canonical import read_json, write_json
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file
from .reference_runtime import (
    REFERENCE_IMAGE_REF,
    REFERENCE_PLATFORM,
    REFERENCE_PLATFORM_DIGEST,
    ensure_docker_config,
    ensure_reference_image,
)

DATA_SEAL_VERSION = "1.0.0"
_DATA_SUFFIXES = {".feather", ".parquet"}
_TIMERANGE = re.compile(r"^(?P<start>\d{8})-(?P<end>\d{8})$")
_TIMEFRAME = re.compile(r"^(?P<count>[1-9]\d*)(?P<unit>[smhdwM])$")


def prepare_data(
    *,
    config_path: str | Path,
    data_directory: str | Path,
    timerange: str,
    timeframes: list[str],
    destination: str | Path,
    download_missing: bool = True,
) -> dict[str, Any]:
    """Check coverage, download only missing edges, then seal every input byte."""
    config_file = Path(config_path).resolve()
    data_root = Path(data_directory).resolve()
    config = read_json(config_file)
    request = _data_request(config, timerange, timeframes)
    data_root.mkdir(parents=True, exist_ok=True)
    gaps = find_coverage_gaps(data_root, request)
    downloads: list[dict[str, Any]] = []
    if gaps and not download_missing:
        raise BenchmarkError(_gap_message(gaps))
    if gaps:
        needs_append = any(gap["end_missing"] for gap in gaps)
        needs_prepend = any(gap["start_missing"] for gap in gaps)
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
        if gaps:
            raise BenchmarkError(
                "download completed but coverage is still incomplete: "
                f"{_gap_message(gaps)}"
            )

    files = _seal_data_files(data_root)
    if not files:
        raise BenchmarkError(f"no Feather or Parquet candle files found under {data_root}")
    seal = {
        "schema_version": DATA_SEAL_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "reference": {
            "image": REFERENCE_IMAGE_REF.split("@", 1)[0],
            "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
            "platform": REFERENCE_PLATFORM,
        },
        "request": {
            **request,
            "config_path": str(config_file),
            "config_sha256": sha256_file(config_file),
        },
        "data_root": str(data_root),
        "downloads": downloads,
        "files": files,
        "aggregate_sha256": _aggregate_files(files),
    }
    validate_data_seal_document(seal, source=Path(destination), verify_files=True)
    write_json(destination, seal)
    return seal


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
    if not isinstance(seal, dict) or seal.get("schema_version") != DATA_SEAL_VERSION:
        raise SpecValidationError("unsupported or invalid data seal")
    required = {
        "schema_version",
        "created_at",
        "reference",
        "request",
        "data_root",
        "downloads",
        "files",
        "aggregate_sha256",
    }
    if set(seal) != required:
        raise SpecValidationError("data seal fields differ from the v1 contract")
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
) -> dict[str, Any]:
    match = _TIMERANGE.fullmatch(timerange)
    if match is None:
        raise SpecValidationError("data timerange must use closed-open YYYYMMDD-YYYYMMDD")
    if not timeframes:
        configured = config.get("timeframe")
        if not isinstance(configured, str) or not configured:
            raise SpecValidationError("at least one timeframe is required")
        timeframes = [configured]
    normalized_timeframes = list(dict.fromkeys(timeframes))
    for timeframe in normalized_timeframes:
        timeframe_milliseconds(timeframe)
    exchange = config.get("exchange")
    if not isinstance(exchange, dict):
        raise SpecValidationError("config.exchange must be an object")
    pairs = exchange.get("pair_whitelist")
    if not isinstance(pairs, list) or not pairs or not all(
        isinstance(pair, str) and pair for pair in pairs
    ):
        raise SpecValidationError("config.exchange.pair_whitelist must contain pairs")
    trading_mode = config.get("trading_mode", "spot")
    if trading_mode not in {"spot", "futures"}:
        raise SpecValidationError("data preparation supports spot or futures")
    start = datetime.strptime(match.group("start"), "%Y%m%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(match.group("end"), "%Y%m%d").replace(tzinfo=timezone.utc)
    if end <= start:
        raise SpecValidationError("data timerange end must be after start")
    return {
        "exchange": str(exchange.get("name", "")).lower(),
        "trading_mode": trading_mode,
        "pairs": list(dict.fromkeys(pairs)),
        "timeframes": normalized_timeframes,
        "timerange": timerange,
        "start_timestamp_ms": int(start.timestamp() * 1000),
        "end_timestamp_ms": int(end.timestamp() * 1000),
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
    docker = shutil.which("docker")
    if docker is None:
        raise BenchmarkError("Docker CLI is not installed or not on PATH")
    with tempfile.TemporaryDirectory(prefix="nfi-data-") as temporary:
        user_data = Path(temporary) / "user_data"
        user_data.mkdir()
        command = [
            docker,
            "--config",
            str(docker_config),
            "run",
            "--rm",
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
            request["timerange"],
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


def _seal_data_files(data_root: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(
        (path for path in data_root.rglob("*") if _is_data_file(path)),
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
            frame = pl.read_ipc(path, columns=["date"], memory_map=True, rechunk=False)
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
