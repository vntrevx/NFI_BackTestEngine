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
from .release_contract import (
    ReleaseModeContract,
    data_role_for_path,
    release_contract_for_config,
    release_contract_for_scope,
)
from .strategy_ir import analyze_strategy
from .timerange import parse_timerange_milliseconds

RELEASE_INPUT_LOCK_VERSION = "1.2.0"
LEGACY_RELEASE_INPUT_LOCK_VERSION = "1.1.0"
DEFAULT_RELEASE_PAIR_COUNT = 80


def discover_release_universe(
    *,
    config_path: str | Path,
    market_snapshot_path: str | Path,
    timerange: str,
    destination: str | Path,
) -> dict[str, Any]:
    """Select historically eligible release candidates from one frozen market view."""
    config_file = Path(config_path).resolve()
    snapshot_file = Path(market_snapshot_path).resolve()
    target = Path(destination).resolve()
    if target.exists():
        raise BenchmarkError(f"release candidate output already exists: {target}")
    loaded = load_effective_config(config_file)
    effective = sanitize_config(loaded["config"])
    if not isinstance(effective, dict):
        raise SpecValidationError("effective release config must be an object")
    contract = release_contract_for_config(effective)
    snapshot = read_json(snapshot_file)
    if not isinstance(snapshot, dict):
        raise SpecValidationError("release market snapshot must be an object")
    if (
        snapshot.get("exchange") != contract.exchange
        or snapshot.get("trading_mode") not in {None, contract.trading_mode}
        or not isinstance(snapshot.get("markets"), dict)
    ):
        raise SpecValidationError(
            "release market snapshot differs from the selected mode contract"
        )
    start_ms, _ = parse_timerange_milliseconds(timerange)
    markets = snapshot["markets"]
    declared_pairs = snapshot.get("pairs")
    candidates = (
        declared_pairs
        if isinstance(declared_pairs, list)
        and all(isinstance(pair, str) for pair in declared_pairs)
        else list(markets)
    )
    exchange = effective["exchange"]
    assert isinstance(exchange, dict)
    blacklist = _compile_blacklist(exchange.get("pair_blacklist", []))
    selected: list[str] = []
    rejected: list[dict[str, str]] = []
    for pair in candidates:
        market = markets.get(pair)
        reason = _market_candidate_rejection(
            pair,
            market,
            contract=contract,
            timerange_start_ms=start_ms,
            blacklist=blacklist,
        )
        if reason is None:
            selected.append(pair)
        else:
            rejected.append({"pair": pair, "reason": reason})
    document = {
        "schema_version": "1.0.0",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mode_contract": contract.contract_id,
        "timerange": timerange,
        "market_snapshot": {
            "path": str(snapshot_file),
            "sha256": sha256_file(snapshot_file),
        },
        "config_sha256": loaded["sha256"],
        "pairs": selected,
        "rejected": rejected,
    }
    write_json(target, document)
    return document


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
    contract = release_contract_for_config(effective)

    candidates = _load_candidates(candidates_file, contract=contract)
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
                trading_mode=contract.trading_mode,
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
                contract,
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
        # Freqtrade requests the strategy startup count independently for each
        # informative timeframe. Missing pre-listing candles are not fabricated:
        # it loads the available prefix and records the shorter context. Requiring
        # the full count on a one-day frame can make a broad release universe
        # impossible for market-history reasons unrelated to the tested interval.
        require_startup_coverage=False,
        history_coverage_policy="strict",
    )
    pairlist = freeze_pairlist(selected_config)
    write_json(output / "pairlist.json", pairlist)
    role_counts = validate_release_data_roles(data_seal, contract=contract)
    report = _selection_report(
        candidates_file,
        candidates,
        selected,
        rejected,
        blacklisted,
        quality,
        pair_count,
        contract,
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
            **contract.scope_fields(),
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
            "startup_coverage_policy": "record",
            "role_counts": role_counts,
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
    if not isinstance(document, dict) or document.get("schema_version") not in {
        RELEASE_INPUT_LOCK_VERSION,
        LEGACY_RELEASE_INPUT_LOCK_VERSION,
    }:
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
    version = document["schema_version"]
    contract = release_contract_for_scope(
        scope,
        legacy_spot=version == LEGACY_RELEASE_INPUT_LOCK_VERSION,
    )
    pairs = pairlist.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != required_pair_count:
        raise SpecValidationError(
            f"release input lock requires exactly {required_pair_count} pairs"
        )
    if scope.get("pair_count") != len(pairs):
        raise SpecValidationError("release input lock pair counts differ")
    if not all(isinstance(pair, str) for pair in pairs):
        raise SpecValidationError("release input lock pairs must be strings")
    contract.validate_pairs(pairs)
    if data.get("coverage_shortfall_count") != 0:
        raise SpecValidationError("release input lock has history coverage shortfalls")
    startup_shortfalls = data.get("startup_shortfall_count")
    if (
        not isinstance(startup_shortfalls, int)
        or isinstance(startup_shortfalls, bool)
        or startup_shortfalls < 0
        or data.get("startup_coverage_policy") != "record"
    ):
        raise SpecValidationError(
            "release input lock startup coverage contract is invalid"
        )
    if version == RELEASE_INPUT_LOCK_VERSION:
        timeframes = scope.get("timeframes")
        if not isinstance(timeframes, list) or not all(
            isinstance(timeframe, str) for timeframe in timeframes
        ):
            raise SpecValidationError("release input lock timeframes are invalid")
        expected_roles = _expected_role_counts(
            contract,
            pair_count=len(pairs),
            timeframe_count=len(timeframes),
        )
        if data.get("role_counts") != expected_roles:
            raise SpecValidationError("release input lock data-role counts differ")
    expected_identity = _identity_sha256(
        {key: value for key, value in document.items() if key != "identity_sha256"}
    )
    if document.get("identity_sha256") != expected_identity:
        raise SpecValidationError("release input lock identity is corrupt")


def validate_release_data_roles(
    seal: dict[str, Any],
    *,
    contract: ReleaseModeContract,
) -> dict[str, int]:
    """Require one complete set of mode-specific files for every sealed pair."""
    request = seal.get("request")
    files = seal.get("files")
    if not isinstance(request, dict) or not isinstance(files, list):
        raise SpecValidationError("release data seal request or files are invalid")
    pairs = request.get("pairs")
    timeframes = request.get("timeframes")
    start_ms = request.get("start_timestamp_ms")
    end_ms = request.get("end_timestamp_ms")
    if (
        not isinstance(pairs, list)
        or not all(isinstance(pair, str) for pair in pairs)
        or not isinstance(timeframes, list)
        or not all(isinstance(timeframe, str) for timeframe in timeframes)
        or not isinstance(start_ms, int)
        or not isinstance(end_ms, int)
    ):
        raise SpecValidationError("release data seal scope is invalid")
    if (
        request.get("trading_mode") != contract.trading_mode
        or request.get("exchange") != contract.exchange
    ):
        raise SpecValidationError("release data seal mode differs from its contract")
    contract.validate_pairs(pairs)

    per_pair = {
        pair: {role: [] for role in contract.required_data_roles}
        for pair in pairs
    }
    for record in files:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise SpecValidationError("release data seal contains a malformed file record")
        for pair in pairs:
            role = data_role_for_path(
                record["path"],
                pair=pair,
                timeframes=timeframes,
                contract=contract,
            )
            if role is not None:
                per_pair[pair][role].append(record)
                break

    side_intervals = dict(contract.side_channel_intervals_ms)
    for pair, roles in per_pair.items():
        candle_records = roles["candles"]
        if len(candle_records) != len(timeframes):
            raise SpecValidationError(
                f"release data requires one candle file per timeframe for {pair}"
            )
        for timeframe in timeframes:
            matches = [
                record
                for record in candle_records
                if data_role_for_path(
                    record["path"],
                    pair=pair,
                    timeframes=[timeframe],
                    contract=contract,
                )
                == "candles"
            ]
            if len(matches) != 1:
                raise SpecValidationError(
                    f"release data requires exactly one {timeframe} candle file "
                    f"for {pair}"
                )
        for role, interval_ms in side_intervals.items():
            records = roles[role]
            if len(records) != 1:
                raise SpecValidationError(
                    f"release data requires exactly one {role} file for {pair}"
                )
            coverage = records[0].get("coverage")
            if (
                not isinstance(coverage, dict)
                or not isinstance(coverage.get("start_timestamp_ms"), int)
                or not isinstance(coverage.get("end_timestamp_ms"), int)
                or coverage["start_timestamp_ms"] > start_ms
                or coverage["end_timestamp_ms"] + interval_ms < end_ms
            ):
                raise SpecValidationError(
                    f"release {role} coverage is incomplete for {pair}"
                )

    return _expected_role_counts(
        contract,
        pair_count=len(pairs),
        timeframe_count=len(timeframes),
    )


def _expected_role_counts(
    contract: ReleaseModeContract,
    *,
    pair_count: int,
    timeframe_count: int,
) -> dict[str, int]:
    return {
        role: (
            pair_count * timeframe_count
            if role == "candles"
            else pair_count
        )
        for role in contract.required_data_roles
    }


def _coverage_by_pair(
    data_root: Path,
    request: dict[str, Any],
    pairs: list[str],
) -> dict[str, list[dict[str, Any]]]:
    gaps = find_coverage_gaps(data_root, request)
    result: dict[str, list[dict[str, Any]]] = {pair: [] for pair in pairs}
    for item in gaps:
        result[item["pair"]].append({"code": "EDGE_COVERAGE", **item})
    for pair in pairs:
        result[pair].sort(
            key=lambda item: (
                request["timeframes"].index(item["timeframe"]),
                item["code"],
            )
        )
    return result


def _load_candidates(
    path: Path,
    *,
    contract: ReleaseModeContract,
) -> list[str]:
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
        if not isinstance(pair, str) or pair.strip() != pair:
            raise SpecValidationError(f"candidate pair {index} is not canonical CCXT")
        contract.validate_pair(pair)
        if pair in seen:
            raise SpecValidationError(f"candidate list contains duplicate pair: {pair}")
        seen.add(pair)
        pairs.append(pair)
    if not pairs:
        raise SpecValidationError("candidate list must not be empty")
    return pairs


def _market_candidate_rejection(
    pair: str,
    market: Any,
    *,
    contract: ReleaseModeContract,
    timerange_start_ms: int,
    blacklist: list[re.Pattern[str]],
) -> str | None:
    try:
        contract.validate_pair(pair)
    except SpecValidationError:
        return "PAIR_CONTRACT"
    if any(pattern.fullmatch(pair) for pattern in blacklist):
        return "BLACKLISTED"
    if not isinstance(market, dict):
        return "MARKET_MISSING"
    if market.get("active") is not True:
        return "INACTIVE"
    created = market.get("created")
    if not isinstance(created, int):
        info = market.get("info")
        created = info.get("onboardDate") if isinstance(info, dict) else None
        if isinstance(created, str) and created.isdecimal():
            created = int(created)
    if not isinstance(created, int):
        return "ONBOARD_DATE_MISSING"
    if created > timerange_start_ms:
        return "LISTED_AFTER_TIMERANGE_START"
    if contract.trading_mode == "spot":
        return None if market.get("spot") is True else "NOT_SPOT"
    margin_modes = market.get("marginModes")
    if (
        market.get("settle") != contract.settlement_currency
        or market.get("swap") is not True
        or market.get("contract") is not True
        or market.get("linear") is not True
        or market.get("inverse") is not False
        or not isinstance(margin_modes, dict)
        or margin_modes.get("isolated") is not True
    ):
        return "NOT_BINANCE_USDTM_ISOLATED"
    return None


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
    contract: ReleaseModeContract,
) -> dict[str, Any]:
    return {
        "schema_version": "1.1.0",
        "mode_contract": contract.contract_id,
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
