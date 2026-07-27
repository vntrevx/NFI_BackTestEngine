"""Deterministic Full X7 pair-universe selection and release input locking."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
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
    timeframe_milliseconds,
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

RELEASE_INPUT_LOCK_VERSION = "1.3.0"
PREVIOUS_RELEASE_INPUT_LOCK_VERSION = "1.2.0"
LEGACY_RELEASE_INPUT_LOCK_VERSION = "1.1.0"
DEFAULT_RELEASE_PAIR_COUNT = 80
STRICT_RELEASE_HISTORY = "strict"
LISTING_AWARE_RELEASE_HISTORY = "listing-aware"
RELEASE_HISTORY_POLICIES = frozenset(
    {STRICT_RELEASE_HISTORY, LISTING_AWARE_RELEASE_HISTORY}
)


@dataclass(frozen=True, slots=True)
class ReleaseCandidateSource:
    """Pair order plus the frozen market facts needed by its history contract."""

    pairs: tuple[str, ...]
    market_onboarding_ms: dict[str, int]
    market_snapshot_sha256: str | None


def discover_release_universe(
    *,
    config_path: str | Path,
    market_snapshot_path: str | Path,
    timerange: str,
    destination: str | Path,
    history_coverage_policy: str = STRICT_RELEASE_HISTORY,
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
    _validate_release_history_policy(history_coverage_policy, contract=contract)
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
    start_ms, end_ms = parse_timerange_milliseconds(timerange)
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
    market_onboarding_ms: dict[str, int] = {}
    for pair in candidates:
        market = markets.get(pair)
        reason = _market_candidate_rejection(
            pair,
            market,
            contract=contract,
            timerange_start_ms=start_ms,
            timerange_end_ms=end_ms,
            history_coverage_policy=history_coverage_policy,
            blacklist=blacklist,
        )
        if reason is None:
            selected.append(pair)
            created = _market_created_ms(market)
            assert created is not None
            market_onboarding_ms[pair] = created
        else:
            rejected.append({"pair": pair, "reason": reason})
    candidate_order = "snapshot"
    if history_coverage_policy == LISTING_AWARE_RELEASE_HISTORY:
        # Oldest markets maximize the amount of real five-year history while
        # still allowing the final slots to join exactly at their listing date.
        selected.sort(key=lambda pair: (market_onboarding_ms[pair], pair))
        candidate_order = "onboarding-ascending-then-pair"
    document = {
        "schema_version": "1.1.0",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mode_contract": contract.contract_id,
        "timerange": timerange,
        "history_coverage_policy": history_coverage_policy,
        "candidate_order": candidate_order,
        "market_snapshot": {
            "path": str(snapshot_file),
            "sha256": sha256_file(snapshot_file),
        },
        "market_onboarding_ms": market_onboarding_ms,
        "config_sha256": loaded["sha256"],
        "pairs": selected,
        "rejected": rejected,
    }
    write_json(target, document)
    return document


def materialize_release_candidate_config(
    *,
    candidates_path: str | Path,
    config_path: str | Path,
    timerange: str,
    destination: str | Path,
    pair_count: int = DEFAULT_RELEASE_PAIR_COUNT,
    history_coverage_policy: str = STRICT_RELEASE_HISTORY,
) -> dict[str, Any]:
    """Write a download config for the first frozen release candidates."""
    if pair_count < 1:
        raise SpecValidationError("release pair count must be positive")
    candidates_file = Path(candidates_path).resolve()
    config_file = Path(config_path).resolve()
    target = Path(destination).resolve()
    if target.exists():
        raise BenchmarkError(f"release candidate config already exists: {target}")
    loaded = load_effective_config(config_file)
    effective = sanitize_config(loaded["config"])
    if not isinstance(effective, dict):
        raise SpecValidationError("effective release config must be an object")
    contract = release_contract_for_config(effective)
    _validate_release_history_policy(history_coverage_policy, contract=contract)
    source = _load_candidate_source(
        candidates_file,
        contract=contract,
        timerange=timerange,
        history_coverage_policy=history_coverage_policy,
    )
    if len(source.pairs) < pair_count:
        raise BenchmarkError(
            f"candidate source has {len(source.pairs)} pairs; {pair_count} are required"
        )
    selected = list(source.pairs[:pair_count])
    exchange = effective.get("exchange")
    if not isinstance(exchange, dict):
        raise SpecValidationError("effective release config exchange must be an object")
    exchange["pair_whitelist"] = selected
    write_json(target, effective)
    return {
        "mode_contract": contract.contract_id,
        "history_coverage_policy": history_coverage_policy,
        "timerange": timerange,
        "pair_count": len(selected),
        "pairs": selected,
        "config_sha256": config_sha256(effective),
        "path": str(target),
    }


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
    history_coverage_policy: str = STRICT_RELEASE_HISTORY,
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
    _validate_release_history_policy(history_coverage_policy, contract=contract)

    candidate_source = _load_candidate_source(
        candidates_file,
        contract=contract,
        timerange=timerange,
        history_coverage_policy=history_coverage_policy,
    )
    candidates = list(candidate_source.pairs)
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
        history_coverage_policy=_data_history_policy(history_coverage_policy),
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
        reasons = _release_coverage_rejections(
            pair,
            coverage_by_pair[pair],
            request=request,
            history_coverage_policy=history_coverage_policy,
            market_onboarding_ms=candidate_source.market_onboarding_ms,
            maximum_activation_delay_ms=(
                contract.maximum_listing_activation_delay_ms
            ),
        )
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
                history_coverage_policy,
            ),
        )
        raise BenchmarkError(
            f"only {len(selected)} candidates satisfy {history_coverage_policy} coverage; "
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
        history_coverage_policy=_data_history_policy(history_coverage_policy),
    )
    pairlist = freeze_pairlist(selected_config)
    write_json(output / "pairlist.json", pairlist)
    selected_onboarding = {
        pair: candidate_source.market_onboarding_ms[pair]
        for pair in selected
        if pair in candidate_source.market_onboarding_ms
    }
    role_counts = validate_release_data_roles(
        data_seal,
        contract=contract,
        history_coverage_policy=history_coverage_policy,
        market_onboarding_ms=selected_onboarding,
    )
    report = _selection_report(
        candidates_file,
        candidates,
        selected,
        rejected,
        blacklisted,
        quality,
        pair_count,
        contract,
        history_coverage_policy,
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
            "history_coverage_policy": history_coverage_policy,
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
            "coverage_shortfalls": data_seal["coverage_shortfalls"],
            "seal_history_coverage_policy": _data_history_policy(
                history_coverage_policy
            ),
            "startup_shortfall_count": len(data_seal["startup_shortfalls"]),
            "startup_coverage_policy": "record",
            "role_counts": role_counts,
            "market_onboarding_ms": selected_onboarding,
        },
        "selection": {
            "candidate_sha256": sha256_file(candidates_file),
            "report_sha256": sha256_file(output / "selection-report.json"),
            "market_snapshot_sha256": candidate_source.market_snapshot_sha256,
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
        PREVIOUS_RELEASE_INPUT_LOCK_VERSION,
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
    history_policy = release_history_coverage_policy(document)
    _validate_release_history_policy(history_policy, contract=contract)
    coverage_shortfall_count = data.get("coverage_shortfall_count")
    if (
        not isinstance(coverage_shortfall_count, int)
        or isinstance(coverage_shortfall_count, bool)
        or coverage_shortfall_count < 0
    ):
        raise SpecValidationError(
            "release input lock coverage shortfall count is invalid"
        )
    if version == RELEASE_INPUT_LOCK_VERSION:
        if data.get("seal_history_coverage_policy") != _data_history_policy(
            history_policy
        ):
            raise SpecValidationError(
                "release input lock data history contract is invalid"
            )
        coverage_shortfalls = data.get("coverage_shortfalls")
        if (
            not isinstance(coverage_shortfalls, list)
            or len(coverage_shortfalls) != coverage_shortfall_count
        ):
            raise SpecValidationError(
                "release input lock coverage shortfalls differ from their count"
            )
        onboarding = data.get("market_onboarding_ms")
        if not isinstance(onboarding, dict):
            raise SpecValidationError(
                "release input lock market onboarding map is invalid"
            )
        if history_policy == LISTING_AWARE_RELEASE_HISTORY:
            selection = document.get("selection")
            snapshot_sha = (
                selection.get("market_snapshot_sha256")
                if isinstance(selection, dict)
                else None
            )
            if (
                not isinstance(snapshot_sha, str)
                or re.fullmatch(r"[0-9a-f]{64}", snapshot_sha) is None
            ):
                raise SpecValidationError(
                    "listing-aware release input lock lacks a market snapshot digest"
                )
            _validate_listing_aware_shortfalls(
                pairs=pairs,
                timeframes=scope.get("timeframes"),
                timerange=scope.get("timerange"),
                shortfalls=coverage_shortfalls,
                market_onboarding_ms=onboarding,
                maximum_activation_delay_ms=(
                    contract.maximum_listing_activation_delay_ms
                ),
            )
        elif coverage_shortfalls or onboarding:
            raise SpecValidationError(
                "strict release input lock contains listing-aware history data"
            )
    elif coverage_shortfall_count != 0:
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
    if version != LEGACY_RELEASE_INPUT_LOCK_VERSION:
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


def release_history_coverage_policy(lock: dict[str, Any]) -> str:
    """Return the public history contract, including legacy strict locks."""
    if lock.get("schema_version") == RELEASE_INPUT_LOCK_VERSION:
        scope = lock.get("scope")
        if isinstance(scope, dict) and isinstance(
            scope.get("history_coverage_policy"), str
        ):
            return scope["history_coverage_policy"]
    return STRICT_RELEASE_HISTORY


def release_data_history_coverage_policy(lock: dict[str, Any]) -> str:
    """Map the public release contract to the research runner data policy."""
    return _data_history_policy(release_history_coverage_policy(lock))


def validate_listing_aware_market_snapshot(
    lock: dict[str, Any],
    snapshot: Any,
) -> None:
    """Cross-check portable onboarding facts against the runtime market view."""
    if release_history_coverage_policy(lock) != LISTING_AWARE_RELEASE_HISTORY:
        return
    markets = snapshot.get("markets") if isinstance(snapshot, dict) else None
    onboarding = lock.get("data", {}).get("market_onboarding_ms")
    if not isinstance(markets, dict) or not isinstance(onboarding, dict):
        raise SpecValidationError(
            "listing-aware runtime market snapshot is malformed"
        )
    for pair in lock["pairlist"]["pairs"]:
        created = _market_created_ms(markets.get(pair))
        if created != onboarding.get(pair):
            raise SpecValidationError(
                f"listing-aware runtime market onboarding differs for {pair}"
            )


def validate_release_data_roles(
    seal: dict[str, Any],
    *,
    contract: ReleaseModeContract,
    history_coverage_policy: str = STRICT_RELEASE_HISTORY,
    market_onboarding_ms: dict[str, int] | None = None,
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
    _validate_release_history_policy(history_coverage_policy, contract=contract)
    onboarding = market_onboarding_ms or {}

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
                or coverage["end_timestamp_ms"] + interval_ms < end_ms
            ):
                raise SpecValidationError(
                    f"release {role} coverage is incomplete for {pair}"
                )
            if coverage["start_timestamp_ms"] > start_ms:
                created = onboarding.get(pair)
                if (
                    history_coverage_policy != LISTING_AWARE_RELEASE_HISTORY
                    or not isinstance(created, int)
                    or created <= start_ms
                    or coverage["start_timestamp_ms"]
                    > _latest_listing_data_start(
                        created,
                        interval_ms,
                        contract.maximum_listing_activation_delay_ms,
                    )
                ):
                    raise SpecValidationError(
                        f"release {role} leading coverage is not justified "
                        f"by market onboarding for {pair}"
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


def _load_candidate_source(
    path: Path,
    *,
    contract: ReleaseModeContract,
    timerange: str,
    history_coverage_policy: str,
) -> ReleaseCandidateSource:
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
    declared_policy = (
        document.get("history_coverage_policy", STRICT_RELEASE_HISTORY)
        if isinstance(document, dict)
        else STRICT_RELEASE_HISTORY
    )
    if declared_policy != history_coverage_policy:
        raise SpecValidationError(
            "candidate history coverage contract differs from selection"
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
    if history_coverage_policy == STRICT_RELEASE_HISTORY:
        return ReleaseCandidateSource(tuple(pairs), {}, None)
    if not isinstance(document, dict):
        raise SpecValidationError(
            "listing-aware selection requires a frozen discovery document"
        )
    if (
        document.get("timerange") != timerange
        or document.get("mode_contract") != contract.contract_id
        or document.get("candidate_order")
        != "onboarding-ascending-then-pair"
    ):
        raise SpecValidationError(
            "candidate discovery scope differs from listing-aware selection"
        )
    snapshot_record = document.get("market_snapshot")
    declared_onboarding = document.get("market_onboarding_ms")
    if (
        not isinstance(snapshot_record, dict)
        or not isinstance(snapshot_record.get("path"), str)
        or not isinstance(snapshot_record.get("sha256"), str)
        or not isinstance(declared_onboarding, dict)
    ):
        raise SpecValidationError(
            "listing-aware candidates lack frozen market onboarding evidence"
        )
    snapshot_path = Path(snapshot_record["path"]).expanduser()
    if not snapshot_path.is_absolute():
        snapshot_path = path.parent / snapshot_path
    snapshot_path = snapshot_path.resolve()
    if (
        not snapshot_path.is_file()
        or sha256_file(snapshot_path) != snapshot_record["sha256"]
    ):
        raise SpecValidationError(
            "listing-aware candidate market snapshot is missing or changed"
        )
    snapshot = read_json(snapshot_path)
    markets = snapshot.get("markets") if isinstance(snapshot, dict) else None
    if (
        not isinstance(markets, dict)
        or snapshot.get("exchange") != contract.exchange
        or snapshot.get("trading_mode") not in {None, contract.trading_mode}
    ):
        raise SpecValidationError(
            "listing-aware candidate market snapshot has the wrong mode"
        )
    onboarding: dict[str, int] = {}
    for pair in pairs:
        created = _market_created_ms(markets.get(pair))
        if created is None:
            raise SpecValidationError(
                f"listing-aware market onboarding is missing for {pair}"
            )
        if declared_onboarding.get(pair) != created:
            raise SpecValidationError(
                f"listing-aware candidate onboarding differs for {pair}"
            )
        onboarding[pair] = created
    if set(declared_onboarding) != set(pairs):
        raise SpecValidationError(
            "listing-aware candidate onboarding pair set differs"
        )
    expected_order = sorted(pairs, key=lambda pair: (onboarding[pair], pair))
    if pairs != expected_order:
        raise SpecValidationError(
            "listing-aware candidate order differs from its onboarding contract"
        )
    return ReleaseCandidateSource(
        tuple(pairs),
        onboarding,
        snapshot_record["sha256"],
    )


def _market_candidate_rejection(
    pair: str,
    market: Any,
    *,
    contract: ReleaseModeContract,
    timerange_start_ms: int,
    timerange_end_ms: int,
    history_coverage_policy: str,
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
    created = _market_created_ms(market)
    if created is None:
        return "ONBOARD_DATE_MISSING"
    if (
        history_coverage_policy == STRICT_RELEASE_HISTORY
        and created > timerange_start_ms
    ):
        return "LISTED_AFTER_TIMERANGE_START"
    if created >= timerange_end_ms:
        return "LISTED_AFTER_TIMERANGE_END"
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
    history_coverage_policy: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.1.0",
        "mode_contract": contract.contract_id,
        "history_coverage_policy": history_coverage_policy,
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


def _validate_release_history_policy(
    policy: str,
    *,
    contract: ReleaseModeContract,
) -> None:
    if policy not in RELEASE_HISTORY_POLICIES:
        raise SpecValidationError(
            f"unsupported release history coverage policy: {policy!r}"
        )
    if (
        policy == LISTING_AWARE_RELEASE_HISTORY
        and contract.trading_mode != "futures"
    ):
        raise SpecValidationError(
            "listing-aware release history is supported only for Futures"
        )


def _data_history_policy(release_policy: str) -> str:
    if release_policy == STRICT_RELEASE_HISTORY:
        return "strict"
    if release_policy == LISTING_AWARE_RELEASE_HISTORY:
        return "available"
    raise SpecValidationError(
        f"unsupported release history coverage policy: {release_policy!r}"
    )


def _market_created_ms(market: Any) -> int | None:
    if not isinstance(market, dict):
        return None
    created = market.get("created")
    if not isinstance(created, int) or isinstance(created, bool):
        info = market.get("info")
        created = info.get("onboardDate") if isinstance(info, dict) else None
        if isinstance(created, str) and created.isdecimal():
            created = int(created)
    return (
        created
        if isinstance(created, int) and not isinstance(created, bool) and created >= 0
        else None
    )


def _release_coverage_rejections(
    pair: str,
    reasons: list[dict[str, Any]],
    *,
    request: dict[str, Any],
    history_coverage_policy: str,
    market_onboarding_ms: dict[str, int],
    maximum_activation_delay_ms: int,
) -> list[dict[str, Any]]:
    if history_coverage_policy == STRICT_RELEASE_HISTORY:
        return list(reasons)
    rejected: list[dict[str, Any]] = []
    created = market_onboarding_ms.get(pair)
    for reason in reasons:
        problem = _listing_shortfall_problem(
            reason,
            pair=pair,
            created=created,
            start_ms=request["start_timestamp_ms"],
            end_ms=request["end_timestamp_ms"],
            maximum_activation_delay_ms=maximum_activation_delay_ms,
        )
        if problem is not None:
            rejected.append({**reason, "listing_aware_rejection": problem})
    return rejected


def _validate_listing_aware_shortfalls(
    *,
    pairs: list[str],
    timeframes: Any,
    timerange: Any,
    shortfalls: list[Any],
    market_onboarding_ms: dict[str, Any],
    maximum_activation_delay_ms: int,
) -> None:
    if not isinstance(timeframes, list) or not all(
        isinstance(timeframe, str) for timeframe in timeframes
    ):
        raise SpecValidationError(
            "listing-aware release input lock timeframes are invalid"
        )
    if not isinstance(timerange, str):
        raise SpecValidationError(
            "listing-aware release input lock timerange is invalid"
        )
    start_ms, end_ms = parse_timerange_milliseconds(timerange)
    if set(market_onboarding_ms) != set(pairs):
        raise SpecValidationError(
            "listing-aware market onboarding pair set differs"
        )
    for pair, created in market_onboarding_ms.items():
        if (
            not isinstance(created, int)
            or isinstance(created, bool)
            or created < 0
        ):
            raise SpecValidationError(
                f"listing-aware market onboarding is invalid for {pair}"
            )
    seen: set[tuple[str, str]] = set()
    for shortfall in shortfalls:
        if not isinstance(shortfall, dict):
            raise SpecValidationError(
                "listing-aware coverage shortfall is malformed"
            )
        pair = shortfall.get("pair")
        timeframe = shortfall.get("timeframe")
        if (
            not isinstance(pair, str)
            or not isinstance(timeframe, str)
            or pair not in market_onboarding_ms
            or timeframe not in timeframes
        ):
            raise SpecValidationError(
                "listing-aware coverage shortfall is outside the locked scope"
            )
        identity = (pair, timeframe)
        if identity in seen:
            raise SpecValidationError(
                "listing-aware coverage shortfall is duplicated"
            )
        seen.add(identity)
        problem = _listing_shortfall_problem(
            shortfall,
            pair=pair,
            created=market_onboarding_ms[pair],
            start_ms=start_ms,
            end_ms=end_ms,
            maximum_activation_delay_ms=maximum_activation_delay_ms,
        )
        if problem is not None:
            raise SpecValidationError(
                f"listing-aware coverage shortfall is invalid for "
                f"{pair} {timeframe}: {problem}"
            )


def _listing_shortfall_problem(
    shortfall: dict[str, Any],
    *,
    pair: str,
    created: int | None,
    start_ms: int,
    end_ms: int,
    maximum_activation_delay_ms: int,
) -> str | None:
    timeframe = shortfall.get("timeframe")
    if (
        shortfall.get("code", "EDGE_COVERAGE") != "EDGE_COVERAGE"
        or not isinstance(timeframe, str)
    ):
        return "unexpected coverage reason"
    if (
        shortfall.get("start_missing") is not True
        or shortfall.get("end_missing") is not False
    ):
        return "only a leading edge shortfall is permitted"
    available_start = shortfall.get("available_start_timestamp_ms")
    available_end = shortfall.get("available_end_timestamp_ms")
    if not isinstance(available_start, int) or not isinstance(available_end, int):
        return "real candle boundaries are required"
    if not isinstance(created, int) or created <= start_ms:
        return "market was already listed at the requested start"
    candle_ms = timeframe_milliseconds(timeframe)
    if available_start > _latest_listing_data_start(
        created,
        candle_ms,
        maximum_activation_delay_ms,
    ):
        return "first candle begins too long after market onboarding"
    if available_end + candle_ms < end_ms:
        return "candle history does not reach the requested end"
    return None


def _latest_listing_data_start(
    created_ms: int,
    interval_ms: int,
    maximum_activation_delay_ms: int,
) -> int:
    """Bound exchange onboarding delay, then allow one complete data interval."""
    activation_deadline = created_ms + maximum_activation_delay_ms
    first_boundary = (
        (activation_deadline + interval_ms - 1) // interval_ms
    ) * interval_ms
    return first_boundary + interval_ms
