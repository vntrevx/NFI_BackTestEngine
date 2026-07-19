"""Parallel, cache-aware orchestration for X7 vector methods."""

from __future__ import annotations

import json
import os
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from .cache import ContentCache, cache_key
from .canonical import read_json, write_json
from .config_loader import config_sha256
from .errors import StrategyAnalysisError
from .fixture import sha256_file
from .hash_index import FileHashIndex
from .runtime_versions import vector_dependency_versions
from .strategy_ir import STRATEGY_IR_VERSION, analyze_strategy
from .workload_calibration import (
    calibrated_admission,
    calibration_key,
    calibration_path,
    create_workload_calibration,
    load_workload_calibration,
)

VECTOR_PIPELINE_VERSION = "1.12.0"


def load_strategy_analysis(
    strategy_path: str | Path,
    *,
    class_name: str,
    cache_directory: str | Path | None,
) -> dict[str, Any]:
    """Load the large X7 AST inventory through the persistent content cache."""
    source = Path(strategy_path).resolve()
    if cache_directory is None:
        return analyze_strategy(source, class_name=class_name)
    cache_root = Path(cache_directory).resolve()
    cache = ContentCache(cache_root)
    with FileHashIndex(cache_root / "file-hashes.sqlite") as hash_index:
        source_file_sha = hash_index.hash_file(source)
    return _load_strategy_analysis(
        source,
        class_name=class_name,
        source_file_sha=source_file_sha,
        cache=cache,
    )


def prepare_vector_signals(
    *,
    strategy_path: str | Path,
    class_name: str,
    config: dict[str, Any],
    pairs: list[str],
    data_directory: str | Path,
    timerange: str,
    output_directory: str | Path,
    workers: int,
    cache_directory: str | Path | None = None,
    memory_cap_bytes: int | None = None,
    hardware_fingerprint: str | None = None,
    calibration_directory: str | Path | None = None,
    recalibrate: bool = False,
) -> dict[str, Any]:
    if workers <= 0:
        raise StrategyAnalysisError("vector worker count must be positive")
    source = Path(strategy_path).resolve()
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise StrategyAnalysisError(f"vector output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    cache = ContentCache(cache_directory) if cache_directory is not None else None
    hash_index = (
        FileHashIndex(Path(cache_directory).resolve() / "file-hashes.sqlite")
        if cache_directory is not None
        else None
    )
    source_file_sha = (
        hash_index.hash_file(source) if hash_index is not None else sha256_file(source)
    )
    analysis = _load_strategy_analysis(
        source,
        class_name=class_name,
        source_file_sha=source_file_sha,
        cache=cache,
    )
    if not analysis["static_safe"]:
        raise StrategyAnalysisError("strategy failed static vector preflight")
    selected = analysis["strategies"][0]
    timeframes = selected["required_timeframes"]
    if not timeframes:
        raise StrategyAnalysisError("strategy does not declare required timeframes")
    base_timeframe = selected["constants"].get("timeframe")
    if not isinstance(base_timeframe, str):
        raise StrategyAnalysisError("strategy base timeframe is not a literal string")
    strategy_sha = analysis["source"]["sha256"]
    raw_startup_candles = selected["constants"].get("startup_candle_count", 0)
    startup_candles = (
        raw_startup_candles
        if isinstance(raw_startup_candles, int) and not isinstance(raw_startup_candles, bool)
        else 0
    )
    effective_config_sha = config_sha256(config)
    runtime_versions = vector_dependency_versions()
    data_root = Path(data_directory).resolve()
    data_index: dict[str, list[Path]] = {}
    requests: list[dict[str, Any]] = []
    cache_hits: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        frame_paths = _resolve_pair_frames(
            data_root,
            pair=pair,
            pairs=pairs,
            timeframes=timeframes,
            config=config,
            data_index=data_index,
        )
        frame_hashes = {
            key: hash_index.hash_file(path) if hash_index is not None else sha256_file(path)
            for key, path in frame_paths.items()
        }
        funding_paths = (
            _resolve_pair_funding_data(
                data_root,
                pair=pair,
                data_index=data_index,
            )
            if config.get("trading_mode") == "futures"
            else None
        )
        funding_hashes = (
            {
                key: hash_index.hash_file(path)
                if hash_index is not None
                else sha256_file(path)
                for key, path in funding_paths.items()
            }
            if funding_paths is not None
            else None
        )
        identity = {
            "vector_pipeline_version": VECTOR_PIPELINE_VERSION,
            "strategy_ir_version": STRATEGY_IR_VERSION,
            "strategy_sha256": strategy_sha,
            "config_sha256": effective_config_sha,
            "pair": pair,
            "base_timeframe": base_timeframe,
            "startup_candles": startup_candles,
            "timeframes": timeframes,
            "timerange": timerange,
            "frames": frame_hashes,
            "funding_data": funding_hashes,
            "runtime_versions": runtime_versions,
        }
        key = cache_key("vectors", identity)
        record_key = cache_key(
            "vector-records",
            {
                "vector_cache_key": key,
                "vector_pipeline_version": VECTOR_PIPELINE_VERSION,
            },
        )
        destination = output / f"{_pair_filename(pair)}.feather"
        if cache is not None:
            cached = cache.get(key)
            cached_record_path = cache.get(record_key)
            if cached is not None and cached_record_path is not None:
                cached_record = read_json(cached_record_path)
                if not isinstance(cached_record, dict):
                    raise StrategyAnalysisError(
                        f"cached vector metadata is not an object for {pair}"
                    )
                _link_or_copy(cached, destination)
                record = {
                    **cached_record,
                    "pair": pair,
                    "path": str(destination),
                    "bytes": destination.stat().st_size,
                    "sha256": sha256_file(destination),
                    "cache_key": key,
                    "cache_hit": True,
                }
                write_json(destination.with_suffix(f"{destination.suffix}.json"), record)
                cache_hits[pair] = record
                continue
        requests.append(
            {
                "schema_version": "1.2.0",
                "strategy_path": str(source),
                "strategy_sha256": strategy_sha,
                "class_name": class_name,
                "config": config,
                "config_sha256": effective_config_sha,
                "pair": pair,
                "pairs": pairs,
                "base_timeframe": base_timeframe,
                "startup_candles": startup_candles,
                "timeframes": timeframes,
                "timerange": timerange,
                "frames": {key: str(path) for key, path in frame_paths.items()},
                "frame_sha256": frame_hashes,
                "funding_data": (
                    {
                        "funding_rate_path": str(funding_paths["funding_rate"]),
                        "funding_rate_sha256": funding_hashes["funding_rate"],
                        "mark_path": str(funding_paths["mark"]),
                        "mark_sha256": funding_hashes["mark"],
                    }
                    if funding_paths is not None and funding_hashes is not None
                    else None
                ),
                "runtime_versions": runtime_versions,
                "output_path": str(destination),
                "_cache_key": key,
                "_cache_record_key": record_key,
            }
        )
    if hash_index is not None:
        hash_index.close()
    records: list[dict[str, Any]] = list(cache_hits.values())
    calibration: dict[str, Any] | None = None
    selected_workers = min(workers, len(pairs))
    if requests:
        from .vector_worker import run_vector_request

        public_requests = [
            {key: value for key, value in item.items() if not key.startswith("_cache_")}
            for item in requests
        ]
        if hardware_fingerprint is not None and calibration_directory is not None:
            identity = _calibration_identity(
                requests,
                strategy_sha=strategy_sha,
                config_sha=effective_config_sha,
                timerange=timerange,
                runtime_versions=runtime_versions,
            )
            key = calibration_key(identity)
            calibration_file = calibration_path(calibration_directory, key)
            if calibration_file.is_file() and not recalibrate:
                calibration = load_workload_calibration(
                    calibration_file,
                    expected_key=key,
                    hardware_fingerprint=hardware_fingerprint,
                )
            else:
                probe_index = _probe_request_index(public_requests)
                probe_request = public_requests.pop(probe_index)
                private_probe = requests.pop(probe_index)
                probe_record = _run_isolated_probe(probe_request)
                _publish_vector_record(
                    probe_record,
                    probe_request,
                    private_probe["_cache_key"],
                    private_probe["_cache_record_key"],
                    cache=cache,
                )
                records.append(
                    {
                        **probe_record,
                        "path": str(Path(probe_request["output_path"])),
                        "cache_key": private_probe["_cache_key"],
                        "cache_hit": False,
                    }
                )
                calibration = create_workload_calibration(
                    calibration_file,
                    key=key,
                    identity=identity,
                    hardware_fingerprint=hardware_fingerprint,
                    probe_pair=str(probe_request["pair"]),
                    probe_peak_rss_bytes=int(probe_record["peak_rss_bytes"]),
                    probe_wall_time_seconds=float(probe_record["wall_time_seconds"]),
                    requested_cpu_processes=min(workers, len(pairs)),
                    memory_cap_bytes=memory_cap_bytes,
                )
            runtime_admission = calibrated_admission(
                probe_peak_rss_bytes=int(calibration["probe"]["peak_rss_bytes"]),
                requested_cpu_processes=min(workers, len(pairs)),
                memory_cap_bytes=memory_cap_bytes,
            )
            calibration = {
                **calibration,
                "runtime_admission": runtime_admission,
            }
            selected_workers = min(
                workers,
                len(pairs),
                int(runtime_admission["safe_processes"]),
            )
        if not public_requests:
            records.sort(key=lambda item: pairs.index(item["pair"]))
            return _vector_report(
                records=records,
                pairs=pairs,
                selected_workers=selected_workers,
                cache_hits=cache_hits,
                strategy_sha=strategy_sha,
                config_sha=effective_config_sha,
                runtime_versions=runtime_versions,
                calibration=calibration,
            )
        with ProcessPoolExecutor(
            max_workers=min(selected_workers, len(public_requests)),
            mp_context=get_context("spawn"),
            # A process owns exactly one full-range pair.  This gives every job
            # an attributable OS peak and prevents dataframe allocator growth
            # from leaking into the next pair.
            max_tasks_per_child=1,
        ) as executor:
            futures = {
                executor.submit(run_vector_request, request): (
                    request,
                    private["_cache_key"],
                    private["_cache_record_key"],
                )
                for request, private in zip(public_requests, requests, strict=True)
            }
            for future in as_completed(futures):
                request, key, record_key = futures[future]
                try:
                    record = future.result()
                except Exception as exc:
                    pair = request["pair"]
                    raise StrategyAnalysisError(
                        f"vector worker failed for {pair}: {type(exc).__name__}: {exc}"
                    ) from exc
                destination = _publish_vector_record(
                    record,
                    request,
                    key,
                    record_key,
                    cache=cache,
                )
                records.append(
                    {
                        **record,
                        "path": str(destination),
                        "cache_key": key,
                        "cache_hit": False,
                    }
                )
    records.sort(key=lambda item: pairs.index(item["pair"]))
    return _vector_report(
        records=records,
        pairs=pairs,
        selected_workers=selected_workers,
        cache_hits=cache_hits,
        strategy_sha=strategy_sha,
        config_sha=effective_config_sha,
        runtime_versions=runtime_versions,
        calibration=calibration,
    )


def _vector_report(
    *,
    records: list[dict[str, Any]],
    pairs: list[str],
    selected_workers: int,
    cache_hits: dict[str, dict[str, Any]],
    strategy_sha: str,
    config_sha: str,
    runtime_versions: dict[str, str],
    calibration: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "pipeline_version": VECTOR_PIPELINE_VERSION,
        "strategy_sha256": strategy_sha,
        "config_sha256": config_sha,
        "runtime_versions": runtime_versions,
        "pair_count": len(pairs),
        "worker_count": selected_workers,
        "cache_hits": len(cache_hits),
        "calibration": calibration,
        "outputs": records,
    }


def _cacheable_vector_record(record: dict[str, Any]) -> dict[str, Any]:
    """Remove host-specific telemetry from content-addressed metadata.

    Two processes can legitimately compute identical vector bytes with
    different runtimes and RSS values. Those measurements belong to each run,
    not to the immutable cache identity.
    """
    return {
        key: value
        for key, value in record.items()
        if key not in {"wall_time_seconds", "peak_rss_bytes", "resident_bytes_at_completion"}
    }


def _calibration_identity(
    requests: list[dict[str, Any]],
    *,
    strategy_sha: str,
    config_sha: str,
    timerange: str,
    runtime_versions: dict[str, str],
) -> dict[str, Any]:
    return {
        "vector_pipeline_version": VECTOR_PIPELINE_VERSION,
        "strategy_sha256": strategy_sha,
        "config_sha256": config_sha,
        "timerange": timerange,
        "runtime_versions": runtime_versions,
        "pairs": [
            {
                "pair": request["pair"],
                "frames": request["frame_sha256"],
                "funding_data": _funding_content_identity(request),
                "input_bytes": _request_input_bytes(request),
            }
            for request in requests
        ],
    }


def _funding_content_identity(request: dict[str, Any]) -> dict[str, str] | None:
    funding = request.get("funding_data")
    if not isinstance(funding, dict):
        return None
    return {
        "funding_rate_sha256": str(funding["funding_rate_sha256"]),
        "mark_sha256": str(funding["mark_sha256"]),
    }


def _request_input_bytes(request: dict[str, Any]) -> int:
    paths = [Path(path) for path in request["frames"].values()]
    funding = request.get("funding_data")
    if isinstance(funding, dict):
        paths.extend(
            [
                Path(funding["funding_rate_path"]),
                Path(funding["mark_path"]),
            ]
        )
    return sum(path.stat().st_size for path in paths)


def _probe_request_index(requests: list[dict[str, Any]]) -> int:
    """Select the full-range pair with the largest sealed input footprint."""
    return max(
        range(len(requests)),
        key=lambda index: (
            _request_input_bytes(requests[index]),
            str(requests[index]["pair"]),
        ),
    )


def _run_isolated_probe(request: dict[str, Any]) -> dict[str, Any]:
    from .vector_worker import run_vector_request

    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=get_context("spawn"),
        max_tasks_per_child=1,
    ) as executor:
        return executor.submit(run_vector_request, request).result()


def _publish_vector_record(
    record: dict[str, Any],
    request: dict[str, Any],
    key: str,
    record_key: str,
    *,
    cache: ContentCache | None,
) -> Path:
    destination = Path(request["output_path"])
    if cache is not None:
        cache.put_file(key, destination)
        cache.put_bytes(
            record_key,
            json.dumps(
                _cacheable_vector_record(record),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        )
    return destination


def _resolve_pair_frames(
    data_root: Path,
    *,
    pair: str,
    pairs: list[str],
    timeframes: list[str],
    config: dict[str, Any],
    data_index: dict[str, list[Path]],
) -> dict[str, Path]:
    stake = str(config.get("stake_currency", "USDT"))
    futures = config.get("trading_mode") in {"futures", "margin"}
    btc_pair = f"BTC/{stake}:{stake}" if futures else f"BTC/{stake}"
    requested_pairs = [pair]
    if btc_pair not in requested_pairs:
        requested_pairs.append(btc_pair)
    frames: dict[str, Path] = {}
    for requested_pair in requested_pairs:
        for timeframe in timeframes:
            path = _find_candle_file(
                data_root,
                requested_pair,
                timeframe,
                futures=futures,
                data_index=data_index,
            )
            frames[f"{requested_pair}|{timeframe}"] = path
    return frames


def _find_candle_file(
    data_root: Path,
    pair: str,
    timeframe: str,
    *,
    futures: bool,
    data_index: dict[str, list[Path]] | None = None,
) -> Path:
    stem = _pair_filename(pair)
    suffixes = (
        (f"{stem}-{timeframe}-futures.feather", f"{stem}-{timeframe}-futures.parquet")
        if futures
        else (f"{stem}-{timeframe}.feather", f"{stem}-{timeframe}.parquet")
    )
    direct = [
        candidate
        for directory in (data_root, data_root / "futures")
        for suffix in suffixes
        if (candidate := directory / suffix).is_file()
    ]
    if len(direct) == 1:
        return direct[0]
    if len(direct) > 1:
        raise StrategyAnalysisError(
            f"expected one candle file for {pair} {timeframe}, found {len(direct)}"
        )
    index = data_index if data_index is not None else {}
    marker = "__nfi_complete_data_index__"
    if marker not in index:
        index.update(_build_data_index(data_root))
        index[marker] = []
    matches = [path for suffix in suffixes for path in index.get(suffix, [])]
    if len(matches) != 1:
        raise StrategyAnalysisError(
            f"expected one candle file for {pair} {timeframe}, found {len(matches)}"
        )
    return matches[0]


def _resolve_pair_funding_data(
    data_root: Path,
    *,
    pair: str,
    data_index: dict[str, list[Path]],
) -> dict[str, Path]:
    """Resolve the two Binance 1h inputs used by Freqtrade funding math.

    Freqtrade 2026.5.1 combines sparse funding-rate events with mark-price
    opens at identical timestamps. Keeping these paths outside the strategy
    timeframe map prevents them from being exposed to `populate_indicators`.
    """
    return {
        "funding_rate": _find_named_data_file(
            data_root,
            f"{_pair_filename(pair)}-1h-funding_rate",
            data_index=data_index,
        ),
        "mark": _find_named_data_file(
            data_root,
            f"{_pair_filename(pair)}-1h-mark",
            data_index=data_index,
        ),
    }


def _find_named_data_file(
    data_root: Path,
    stem: str,
    *,
    data_index: dict[str, list[Path]],
) -> Path:
    suffixes = (f"{stem}.feather", f"{stem}.parquet")
    direct = [
        candidate
        for directory in (data_root, data_root / "futures")
        for suffix in suffixes
        if (candidate := directory / suffix).is_file()
    ]
    if len(direct) == 1:
        return direct[0]
    if len(direct) > 1:
        raise StrategyAnalysisError(
            f"expected one futures data file for {stem}, found {len(direct)}"
        )
    marker = "__nfi_complete_data_index__"
    if marker not in data_index:
        data_index.update(_build_data_index(data_root))
        data_index[marker] = []
    matches = [path for suffix in suffixes for path in data_index.get(suffix, [])]
    if len(matches) != 1:
        raise StrategyAnalysisError(
            f"expected one futures data file for {stem}, found {len(matches)}"
        )
    return matches[0]


def _build_data_index(data_root: Path) -> dict[str, list[Path]]:
    """Index the normal Freqtrade exchange layout without a recursive tree walk."""
    index: dict[str, list[Path]] = {}
    directories = [data_root]
    directories.extend(path for path in data_root.iterdir() if path.is_dir())
    for directory in directories:
        for path in directory.iterdir():
            if path.is_file() and path.suffix.lower() in {".feather", ".parquet"}:
                index.setdefault(path.name, []).append(path)
    return index


def _load_strategy_analysis(
    source: Path,
    *,
    class_name: str,
    source_file_sha: str,
    cache: ContentCache | None,
) -> dict[str, Any]:
    identity = {
        "strategy_ir_version": STRATEGY_IR_VERSION,
        "source_file_sha256": source_file_sha,
        "class_name": class_name,
    }
    key = cache_key("strategy-analysis", identity)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            result = read_json(cached)
            if isinstance(result, dict):
                return result
            raise StrategyAnalysisError("cached strategy analysis is not an object")
    result = analyze_strategy(source, class_name=class_name)
    if cache is not None:
        cache.put_bytes(
            key,
            (
                json.dumps(
                    result,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode(),
        )
    return result


def _pair_filename(pair: str) -> str:
    return pair.replace("/", "_").replace(":", "_")


def _link_or_copy(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)
