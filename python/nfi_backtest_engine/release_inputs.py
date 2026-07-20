"""Deterministic Full X7 pair-universe selection and release input locking."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .config_loader import config_sha256, freeze_pairlist, load_effective_config, sanitize_config
from .data_seal import (
    DATA_SEAL_VERSION,
    build_data_request,
    candle_files_for,
    find_coverage_gaps,
    find_startup_shortfalls,
    inspect_candle_quality,
    prepare_data,
)
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file
from .reference_runtime import (
    REFERENCE_IMAGE,
    REFERENCE_INDEX_DIGEST,
    REFERENCE_PLATFORM,
    REFERENCE_PLATFORM_DIGEST,
    REFERENCE_VERSION,
)
from .strategy_ir import analyze_strategy

RELEASE_INPUT_LOCK_VERSION = "1.0.0"
DEFAULT_RELEASE_PAIR_COUNT = 80


def select_release_universe(
    *,
    candidates_path: str | Path,
    strategy_path: str | Path,
    class_name: str,
    config_path: str | Path,
    data_directory: str | Path,
    timerange: str,
    output_directory: str | Path,
    pair_count: int = DEFAULT_RELEASE_PAIR_COUNT,
    upstream_repository: str,
    upstream_commit: str,
) -> dict[str, Any]:
    """Select the first fully covered candidates and seal the exact release inputs."""
    if pair_count < 1:
        raise SpecValidationError("release pair count must be positive")
    if not re.fullmatch(r"[0-9a-f]{40}", upstream_commit):
        raise SpecValidationError("upstream commit must be a 40-character lowercase Git SHA")
    source = Path(strategy_path).resolve()
    config_file = Path(config_path).resolve()
    data_root = Path(data_directory).resolve()
    candidates_file = Path(candidates_path).resolve()
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"release input output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    analysis = analyze_strategy(source, class_name=class_name)
    if not analysis["static_safe"] or len(analysis["strategies"]) != 1:
        raise SpecValidationError("release universe requires one static-safe strategy class")
    strategy = analysis["strategies"][0]
    timeframes = strategy["required_timeframes"]
    raw_startup = strategy["constants"].get("startup_candle_count", 0)
    startup_candles = (
        raw_startup
        if isinstance(raw_startup, int) and not isinstance(raw_startup, bool)
        else 0
    )

    loaded = load_effective_config(config_file)
    effective = sanitize_config(loaded["config"])
    if not isinstance(effective, dict):
        raise SpecValidationError("effective release config must be an object")
    exchange = effective.get("exchange")
    if not isinstance(exchange, dict):
        raise SpecValidationError("effective release config exchange must be an object")
    if effective.get("trading_mode", "spot") != "spot":
        raise SpecValidationError("the representative Full X7 universe must use spot mode")

    candidates = _load_candidates(candidates_file)
    blacklist = _compile_blacklist(exchange.get("pair_blacklist", []))
    accepted_candidates = [
        pair for pair in candidates if not any(pattern.fullmatch(pair) for pattern in blacklist)
    ]
    selection_request = deepcopy(effective)
    selection_exchange = selection_request["exchange"]
    assert isinstance(selection_exchange, dict)
    selection_exchange["pair_whitelist"] = accepted_candidates
    request = build_data_request(
        selection_request,
        timerange,
        timeframes,
        startup_candles=startup_candles,
        require_startup_coverage=True,
        history_coverage_policy="strict",
    )
    coverage_by_pair = _coverage_by_pair(
        data_root,
        request,
        accepted_candidates,
        timeframes,
    )

    selected: list[str] = []
    rejected: list[dict[str, Any]] = []
    quality: dict[str, list[dict[str, Any]]] = {}
    for pair in accepted_candidates:
        reasons = coverage_by_pair[pair]
        pair_quality: list[dict[str, Any]] = []
        for timeframe in timeframes:
            matches = candle_files_for(
                data_root,
                pair=pair,
                timeframe=timeframe,
                trading_mode="spot",
            )
            if len(matches) != 1:
                reasons.append(
                    {
                        "code": "AMBIGUOUS_CANDLE_FILE",
                        "timeframe": timeframe,
                        "file_count": len(matches),
                    }
                )
                continue
            inspected = inspect_candle_quality(matches[0], timeframe=timeframe)
            pair_quality.append(
                {
                    "timeframe": timeframe,
                    "path": matches[0].relative_to(data_root).as_posix(),
                    **inspected,
                }
            )
            if inspected["duplicate_timestamp_count"]:
                reasons.append(
                    {
                        "code": "DUPLICATE_TIMESTAMPS",
                        "timeframe": timeframe,
                        "count": inspected["duplicate_timestamp_count"],
                    }
                )
            if inspected["out_of_order_timestamp_count"]:
                reasons.append(
                    {
                        "code": "OUT_OF_ORDER_TIMESTAMPS",
                        "timeframe": timeframe,
                        "count": inspected["out_of_order_timestamp_count"],
                    }
                )
        quality[pair] = pair_quality
        if reasons:
            rejected.append({"pair": pair, "reasons": reasons})
        elif len(selected) < pair_count:
            selected.append(pair)

    blacklisted = sorted(set(candidates) - set(accepted_candidates))
    if len(selected) < pair_count:
        write_json(
            output / "selection-report.json",
            _selection_report(
                candidates_file,
                candidates,
                selected,
                rejected,
                blacklisted,
                quality,
                pair_count,
            ),
        )
        raise BenchmarkError(
            f"only {len(selected)} candidates have strict complete coverage; "
            f"{pair_count} are required"
        )

    selected_config = deepcopy(effective)
    selected_exchange = selected_config["exchange"]
    assert isinstance(selected_exchange, dict)
    selected_exchange["pair_whitelist"] = selected
    selected_config_path = output / "selected-config.json"
    write_json(selected_config_path, selected_config)
    data_seal = prepare_data(
        config_path=selected_config_path,
        data_directory=data_root,
        timerange=timerange,
        timeframes=timeframes,
        destination=output / "data-seal.json",
        download_missing=False,
        startup_candles=startup_candles,
        require_startup_coverage=True,
        history_coverage_policy="strict",
    )
    pairlist = freeze_pairlist(selected_config)
    write_json(output / "pairlist.json", pairlist)
    report = _selection_report(
        candidates_file,
        candidates,
        selected,
        rejected,
        blacklisted,
        quality,
        pair_count,
    )
    write_json(output / "selection-report.json", report)

    lock = {
        "schema_version": RELEASE_INPUT_LOCK_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "sealed",
        "strategy": {
            "class_name": class_name,
            "source_sha256": sha256_file(source),
            "capability_fingerprint": strategy["capability_fingerprint"],
            "upstream_repository": upstream_repository,
            "upstream_commit": upstream_commit,
        },
        "reference": {
            "version": REFERENCE_VERSION,
            "image": REFERENCE_IMAGE,
            "image_index_digest": REFERENCE_INDEX_DIGEST,
            "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
            "platform": REFERENCE_PLATFORM,
        },
        "config": {
            "source_sha256": loaded["sha256"],
            "selected_sha256": config_sha256(selected_config),
        },
        "scope": {
            "trading_mode": "spot",
            "timerange": timerange,
            "pair_count": len(selected),
            "timeframes": timeframes,
            "startup_candles": startup_candles,
        },
        "pairlist": {
            "sha256": pairlist["sha256"],
            "pairs": selected,
        },
        "data": {
            "seal_version": DATA_SEAL_VERSION,
            "aggregate_sha256": data_seal["aggregate_sha256"],
            "file_count": len(data_seal["files"]),
            "coverage_shortfall_count": len(data_seal["coverage_shortfalls"]),
            "startup_shortfall_count": len(data_seal["startup_shortfalls"]),
        },
        "selection": {
            "candidate_sha256": sha256_file(candidates_file),
            "report_sha256": sha256_file(output / "selection-report.json"),
        },
    }
    lock["identity_sha256"] = _identity_sha256(lock)
    validate_release_input_lock(lock, required_pair_count=pair_count)
    write_json(output / "release-input-lock.json", lock)
    return lock


def validate_release_input_lock(
    document: Any,
    *,
    required_pair_count: int = DEFAULT_RELEASE_PAIR_COUNT,
) -> None:
    """Validate the release-critical invariants without machine-specific paths."""
    if not isinstance(document, dict) or document.get("schema_version") != (
        RELEASE_INPUT_LOCK_VERSION
    ):
        raise SpecValidationError("unsupported release input lock")
    if document.get("status") != "sealed":
        raise SpecValidationError("release input lock is not sealed")
    scope = document.get("scope")
    pairlist = document.get("pairlist")
    data = document.get("data")
    if not all(isinstance(value, dict) for value in (scope, pairlist, data)):
        raise SpecValidationError("release input lock sections are invalid")
    assert isinstance(scope, dict)
    assert isinstance(pairlist, dict)
    assert isinstance(data, dict)
    if scope.get("trading_mode") != "spot":
        raise SpecValidationError("Full X7 representative lock must use spot mode")
    pairs = pairlist.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != required_pair_count:
        raise SpecValidationError(
            f"release input lock requires exactly {required_pair_count} pairs"
        )
    if scope.get("pair_count") != len(pairs):
        raise SpecValidationError("release input lock pair counts differ")
    if data.get("coverage_shortfall_count") != 0:
        raise SpecValidationError("release input lock has history coverage shortfalls")
    if data.get("startup_shortfall_count") != 0:
        raise SpecValidationError("release input lock has startup coverage shortfalls")
    expected_identity = _identity_sha256(
        {key: value for key, value in document.items() if key != "identity_sha256"}
    )
    if document.get("identity_sha256") != expected_identity:
        raise SpecValidationError("release input lock identity is corrupt")


def _coverage_by_pair(
    data_root: Path,
    request: dict[str, Any],
    pairs: list[str],
    timeframes: list[str],
) -> dict[str, list[dict[str, Any]]]:
    gaps = find_coverage_gaps(data_root, request)
    startup = find_startup_shortfalls(data_root, request)
    result: dict[str, list[dict[str, Any]]] = {pair: [] for pair in pairs}
    for item in gaps:
        result[item["pair"]].append({"code": "EDGE_COVERAGE", **item})
    for item in startup:
        result[item["pair"]].append({"code": "STARTUP_COVERAGE", **item})
    for pair in pairs:
        result[pair].sort(
            key=lambda item: (
                timeframes.index(item["timeframe"]),
                item["code"],
            )
        )
    return result


def _load_candidates(path: Path) -> list[str]:
    document = read_json(path)
    if isinstance(document, list):
        raw = document
    elif isinstance(document, dict) and isinstance(document.get("pairs"), list):
        raw = document["pairs"]
    elif (
        isinstance(document, dict)
        and isinstance(document.get("exchange"), dict)
        and isinstance(document["exchange"].get("pair_whitelist"), list)
    ):
        raw = document["exchange"]["pair_whitelist"]
    else:
        raise SpecValidationError(
            "candidate file must be a pair list, a {pairs: [...]} document, "
            "or a Freqtrade config"
        )
    pairs: list[str] = []
    seen: set[str] = set()
    for index, pair in enumerate(raw):
        if (
            not isinstance(pair, str)
            or "/" not in pair
            or ":" in pair
            or pair.strip() != pair
        ):
            raise SpecValidationError(f"candidate pair {index} is not canonical spot CCXT")
        if pair in seen:
            raise SpecValidationError(f"candidate list contains duplicate pair: {pair}")
        seen.add(pair)
        pairs.append(pair)
    if not pairs:
        raise SpecValidationError("candidate list must not be empty")
    return pairs


def _compile_blacklist(value: Any) -> list[re.Pattern[str]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SpecValidationError("exchange.pair_blacklist must contain regex strings")
    try:
        return [re.compile(item) for item in value]
    except re.error as exc:
        raise SpecValidationError(f"invalid pair blacklist expression: {exc}") from exc


def _selection_report(
    candidates_file: Path,
    candidates: list[str],
    selected: list[str],
    rejected: list[dict[str, Any]],
    blacklisted: list[str],
    quality: dict[str, list[dict[str, Any]]],
    required: int,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "candidate_source": {
            "path": str(candidates_file),
            "sha256": sha256_file(candidates_file),
            "count": len(candidates),
        },
        "required_pair_count": required,
        "selected_pairs": selected,
        "blacklisted_pairs": blacklisted,
        "rejected_candidates": rejected,
        "quality": quality,
    }


def _identity_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
