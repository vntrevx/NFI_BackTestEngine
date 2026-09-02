"""Bounded, resumable discovery of branch-reaching Spot/Futures workloads.

The discovery lane is deliberately separate from compatibility qualification.
Search heuristics may identify a promising pair and interval, but only the
existing official/Native fixture capture gate may promote that observation.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import (
    BenchmarkError,
    BranchCoverageError,
    DiscoveryInfrastructureError,
    SpecValidationError,
)
from .fixture import sha256_file
from .market_snapshot import MARKET_CATALOG_VERSION, MARKET_SNAPSHOT_VERSION
from .reference.contracts import REFERENCE_INDEX_DIGEST
from .targeted_verification import plan_targeted_verification

DISCOVERY_POLICY_VERSION = "1.3.0"
DISCOVERY_REPORT_VERSION = "1.0.0"
DISCOVERY_CURSOR_VERSION = "1.0.0"
DISCOVERY_REQUEST_VERSION = "1.0.0"

_REPORT_STATES = {
    "no_gap",
    "candidate_found",
    "budget_exhausted",
    "coverage_exhausted",
    "unsupported_semantics",
    "external_data_deferred",
    "infrastructure_failed",
}
_SHARD_OUTCOMES = {
    "miss",
    "candidate",
    "unsupported",
    "external_data_deferred",
    "infrastructure_failed",
}


@dataclass(frozen=True, slots=True)
class SearchShard:
    """One deterministic half-open search interval."""

    index: int
    timerange: str
    start: date
    stop: date


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    """Validated operational bounds loaded from declarative repository data."""

    trading_mode: str
    completed_years: int
    shard_months: int
    pair_limit: int
    template_config: Path
    market_catalog: Path
    market_catalog_sha256: str
    market_snapshot: Path
    market_snapshot_sha256: str
    budget_seconds: int
    workers: int
    archive_workers: int
    archive_source: str
    deferred_http_statuses: frozenset[int]
    external_retry: str
    compact_artifact_retention_days: int
    context_days: int
    max_candidate_bytes: int
    artifact_retention_days: int
    source_path: Path
    source_sha256: str


@dataclass(slots=True)
class DiscoveryContext:
    """Immutable request facts plus run-local paths shared by shard scouts."""

    source: Path
    strategy_diff: dict[str, Any]
    compatibility: dict[str, Any]
    targets: list[dict[str, Any]]
    search_targets: list[dict[str, Any]]
    baseline_source: Path | None
    baseline_upstream_commit: str | None
    policy: DiscoveryPolicy
    output: Path
    class_name: str
    upstream_repository: str
    upstream_commit: str
    engine_commit: str
    profile_path: Path
    workers: int
    fingerprint: str
    all_shards: list[SearchShard]


ShardScout = Callable[[SearchShard, DiscoveryContext], dict[str, Any]]
Clock = Callable[[], float]


def load_discovery_policy(
    policy_path: str | Path,
    *,
    repository_root: str | Path | None = None,
) -> DiscoveryPolicy:
    """Load the strict policy without embedding operational values in code."""
    path = Path(policy_path).resolve()
    document = read_json(path)
    expected = {
        "schema_version",
        "trading_mode",
        "history",
        "universe",
        "execution",
        "external_data",
        "storage",
        "candidate",
    }
    if (
        not isinstance(document, dict)
        or set(document) != expected
        or document.get("schema_version") != DISCOVERY_POLICY_VERSION
        or document.get("trading_mode") not in {"spot", "futures"}
    ):
        raise SpecValidationError("discovery policy fields, mode, or version differ")
    trading_mode = str(document["trading_mode"])
    history = _object(document["history"], "history")
    universe = _object(document["universe"], "universe")
    execution = _object(document["execution"], "execution")
    external_data = _object(document["external_data"], "external_data")
    storage = _object(document["storage"], "storage")
    candidate = _object(document["candidate"], "candidate")
    _require_exact_fields(
        history,
        {"completed_years", "shard_months", "order", "coverage"},
        "history",
    )
    _require_exact_fields(
        universe,
        {"pair_limit", "template_config", "market_catalog", "market_snapshot"},
        "universe",
    )
    _require_exact_fields(
        execution,
        {"budget_seconds", "workers", "archive_workers"},
        "execution",
    )
    _require_exact_fields(
        external_data,
        {"source", "deferred_http_statuses", "retry"},
        "external_data",
    )
    _require_exact_fields(
        storage,
        {"compact_artifact_retention_days"},
        "storage",
    )
    _require_exact_fields(
        candidate,
        {"context_days", "max_bytes", "artifact_retention_days"},
        "candidate",
    )
    if history["order"] != "newest-first" or history["coverage"] != "listing-aware":
        raise SpecValidationError(
            "discovery history must be newest-first and listing-aware"
        )
    completed_years = _positive_int(history["completed_years"], "completed_years")
    shard_months = _positive_int(history["shard_months"], "shard_months")
    if 12 % shard_months:
        raise SpecValidationError("discovery shard_months must divide one calendar year")
    pair_limit = _positive_int(universe["pair_limit"], "pair_limit")
    budget_seconds = _positive_int(execution["budget_seconds"], "budget_seconds")
    workers = _positive_int(execution["workers"], "workers")
    archive_workers = _positive_int(execution["archive_workers"], "archive_workers")
    archive_source = external_data["source"]
    if archive_source != "binance-public-data-monthly":
        raise SpecValidationError(
            "discovery external_data source must be binance-public-data-monthly"
        )
    deferred_http_statuses = _http_statuses(
        external_data["deferred_http_statuses"],
    )
    external_retry = external_data["retry"]
    if external_retry != "identity-change-or-manual":
        raise SpecValidationError(
            "discovery external_data retry must be identity-change-or-manual"
        )
    compact_retention = _positive_int(
        storage["compact_artifact_retention_days"],
        "compact_artifact_retention_days",
    )
    context_days = _positive_int(candidate["context_days"], "context_days")
    max_candidate_bytes = _positive_int(candidate["max_bytes"], "max_bytes")
    retention = _positive_int(
        candidate["artifact_retention_days"],
        "artifact_retention_days",
    )
    template_value = universe["template_config"]
    if not isinstance(template_value, str) or not template_value:
        raise SpecValidationError("discovery template_config must be a repository path")
    root = (
        Path(repository_root).resolve()
        if repository_root is not None
        else path.parent.parent.resolve()
    )
    template = (root / template_value).resolve()
    if not template.is_relative_to(root) or not template.is_file():
        raise SpecValidationError(
            "discovery template_config must resolve to a repository file"
        )
    catalog = _object(universe["market_catalog"], "market_catalog")
    _require_exact_fields(catalog, {"path", "sha256"}, "market_catalog")
    catalog_value = catalog["path"]
    catalog_sha256 = catalog["sha256"]
    if not isinstance(catalog_value, str) or not catalog_value:
        raise SpecValidationError("discovery market_catalog.path must be a repository path")
    if (
        not isinstance(catalog_sha256, str)
        or len(catalog_sha256) != 64
        or any(character not in "0123456789abcdef" for character in catalog_sha256)
    ):
        raise SpecValidationError("discovery market_catalog.sha256 must be lowercase SHA-256")
    catalog_path = (root / catalog_value).resolve()
    if not catalog_path.is_relative_to(root) or not catalog_path.is_file():
        raise SpecValidationError(
            "discovery market_catalog.path must resolve to a repository file"
        )
    if sha256_file(catalog_path) != catalog_sha256:
        raise SpecValidationError("discovery market_catalog SHA-256 differs")
    catalog_document = read_json(catalog_path)
    if (
        not isinstance(catalog_document, dict)
        or catalog_document.get("schema_version") != MARKET_CATALOG_VERSION
        or catalog_document.get("exchange") != "binance"
        or catalog_document.get("trading_mode") != trading_mode
        or not isinstance(catalog_document.get("pairs"), list)
        or not isinstance(catalog_document.get("markets"), dict)
    ):
        raise SpecValidationError(
            "discovery market_catalog identity or structure differs"
        )
    market_snapshot, market_snapshot_sha256 = _load_pinned_market_input(
        universe["market_snapshot"],
        label="market_snapshot",
        root=root,
        schema_version=MARKET_SNAPSHOT_VERSION,
        trading_mode=trading_mode,
    )
    return DiscoveryPolicy(
        trading_mode=trading_mode,
        completed_years=completed_years,
        shard_months=shard_months,
        pair_limit=pair_limit,
        template_config=template,
        market_catalog=catalog_path,
        market_catalog_sha256=catalog_sha256,
        market_snapshot=market_snapshot,
        market_snapshot_sha256=market_snapshot_sha256,
        budget_seconds=budget_seconds,
        workers=workers,
        archive_workers=archive_workers,
        archive_source=archive_source,
        deferred_http_statuses=deferred_http_statuses,
        external_retry=external_retry,
        compact_artifact_retention_days=compact_retention,
        context_days=context_days,
        max_candidate_bytes=max_candidate_bytes,
        artifact_retention_days=retention,
        source_path=path,
        source_sha256=sha256_file(path),
    )


def _load_pinned_market_input(
    value: Any,
    *,
    label: str,
    root: Path,
    schema_version: str,
    trading_mode: str,
) -> tuple[Path, str]:
    record = _object(value, label)
    _require_exact_fields(record, {"path", "sha256"}, label)
    path_value = record["path"]
    digest = record["sha256"]
    if not isinstance(path_value, str) or not path_value:
        raise SpecValidationError(f"discovery {label}.path must be a repository path")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise SpecValidationError(f"discovery {label}.sha256 must be lowercase SHA-256")
    source = (root / path_value).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise SpecValidationError(
            f"discovery {label}.path must resolve to a repository file"
        )
    if sha256_file(source) != digest:
        raise SpecValidationError(f"discovery {label} SHA-256 differs")
    document = read_json(source)
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != schema_version
        or document.get("exchange") != "binance"
        or document.get("trading_mode") != trading_mode
        or not isinstance(document.get("pairs"), list)
        or not isinstance(document.get("markets"), dict)
    ):
        raise SpecValidationError(f"discovery {label} identity or structure differs")
    return source, digest


def search_shards(
    as_of: date,
    *,
    completed_years: int,
    shard_months: int,
) -> list[SearchShard]:
    """Return newest-first calendar shards for completed UTC years."""
    if completed_years <= 0 or shard_months <= 0 or 12 % shard_months:
        raise SpecValidationError("invalid completed-year discovery window")
    shards: list[SearchShard] = []
    index = 0
    final_year = as_of.year - 1
    first_year = final_year - completed_years + 1
    for year in range(final_year, first_year - 1, -1):
        for start_month in range(13 - shard_months, 0, -shard_months):
            stop_year, stop_month = _advance_month(year, start_month, shard_months)
            start = date(year, start_month, 1)
            stop = date(stop_year, stop_month, 1)
            shards.append(
                SearchShard(
                    index=index,
                    timerange=f"{start:%Y%m%d}-{stop:%Y%m%d}",
                    start=start,
                    stop=stop,
                )
            )
            index += 1
    return shards


def build_discovery_request(
    strategy_diff: Mapping[str, Any] | str | Path,
    compatibility_report: Mapping[str, Any] | str | Path,
    fixtures_root: str | Path,
    *,
    policy: DiscoveryPolicy,
    upstream_commit: str,
    engine_commit: str,
    as_of: date,
    baseline_upstream_commit: str | None = None,
    baseline_source_sha256: str | None = None,
) -> dict[str, Any]:
    """Bind one missing-target queue to upstream, engine, policy, and time scope."""
    difference = _document(strategy_diff, "strategy diff")
    compatibility = _document(compatibility_report, "compatibility report")
    _validate_sha(upstream_commit, "upstream")
    _validate_sha(engine_commit, "engine")
    if baseline_upstream_commit is not None:
        _validate_sha(baseline_upstream_commit, "baseline upstream")
    if baseline_source_sha256 is not None:
        _validate_sha256(baseline_source_sha256, "baseline source")
    if (baseline_upstream_commit is None) != (baseline_source_sha256 is None):
        raise SpecValidationError(
            "baseline upstream commit and source digest must be supplied together"
        )
    plan = plan_targeted_verification(
        difference,
        fixtures_root,
        trading_mode=policy.trading_mode,
    )
    missing = plan["missing_targets"]
    searchable = [target for target in missing if _deep_searchable(target)]
    if (
        not searchable
        and baseline_upstream_commit is not None
        and baseline_source_sha256 is not None
    ):
        searchable = [
            target for target in missing if _baseline_deep_searchable(target)
        ]
    searchable_ids = {str(target["id"]) for target in searchable}
    unsearchable = [
        target for target in missing if str(target["id"]) not in searchable_ids
    ]
    shards = search_shards(
        as_of,
        completed_years=policy.completed_years,
        shard_months=policy.shard_months,
    )
    identity = {
        "upstream_commit": upstream_commit,
        "baseline_upstream_commit": baseline_upstream_commit,
        "engine_commit": engine_commit,
        "freqtrade_image_digest": REFERENCE_INDEX_DIGEST,
        "strategy_sha256": difference.get("new", {}).get("sha256"),
        "baseline_strategy_sha256": baseline_source_sha256,
        "trading_mode": policy.trading_mode,
        "policy_sha256": policy.source_sha256,
        "target_ids": sorted(str(target["id"]) for target in missing),
        "completed_through": shards[0].stop.isoformat(),
        "timeranges": [shard.timerange for shard in shards],
    }
    fingerprint = _canonical_sha256(identity)
    return {
        "schema_version": DISCOVERY_REQUEST_VERSION,
        "trading_mode": policy.trading_mode,
        "fingerprint": fingerprint,
        "identity": identity,
        "native_compatible": compatibility.get("native_compatible") is True,
        "plan_status": plan["status"],
        "missing_targets": missing,
        "searchable_targets": searchable,
        "unsearchable_targets": unsearchable,
        "search": {
            "pair_limit": policy.pair_limit,
            "budget_seconds": policy.budget_seconds,
            "workers": policy.workers,
            "archive_workers": policy.archive_workers,
            "timeranges": [shard.timerange for shard in shards],
        },
        "external_data": {
            "source": policy.archive_source,
            "deferred_http_statuses": sorted(policy.deferred_http_statuses),
            "retry": policy.external_retry,
        },
    }


def discover_targets(
    source: str | Path,
    strategy_diff: Mapping[str, Any] | str | Path,
    compatibility_report: Mapping[str, Any] | str | Path,
    fixtures_root: str | Path,
    policy_path: str | Path,
    output_directory: str | Path,
    *,
    class_name: str,
    upstream_repository: str,
    upstream_commit: str,
    engine_commit: str,
    profile_path: str | Path,
    baseline_source: str | Path | None = None,
    baseline_upstream_commit: str | None = None,
    cursor_path: str | Path | None = None,
    as_of: date | None = None,
    workers: int | None = None,
    scout_service: ShardScout | None = None,
    clock: Clock = time.monotonic,
) -> dict[str, Any]:
    """Search missing targets until a candidate, exhaustion, or budget stop."""
    source_path = Path(source).resolve()
    baseline_path = (
        Path(baseline_source).resolve()
        if baseline_source is not None
        else None
    )
    profile = Path(profile_path).resolve()
    if (
        not source_path.is_file()
        or not profile.is_file()
        or (baseline_path is not None and not baseline_path.is_file())
    ):
        raise SpecValidationError("discovery source and execution profile must exist")
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"discovery output must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    policy = load_discovery_policy(policy_path)
    difference = _document(strategy_diff, "strategy diff")
    compatibility = _document(compatibility_report, "compatibility report")
    effective_as_of = as_of or datetime.now(UTC).date()
    request = build_discovery_request(
        difference,
        compatibility,
        fixtures_root,
        policy=policy,
        upstream_commit=upstream_commit,
        engine_commit=engine_commit,
        as_of=effective_as_of,
        baseline_upstream_commit=baseline_upstream_commit,
        baseline_source_sha256=(
            sha256_file(baseline_path) if baseline_path is not None else None
        ),
    )
    write_json(output / "discovery-request.json", request)
    targets = [dict(target) for target in request["searchable_targets"]]
    all_targets = [dict(target) for target in request["missing_targets"]]
    unsearchable_targets = [
        dict(target) for target in request["unsearchable_targets"]
    ]
    shards = search_shards(
        effective_as_of,
        completed_years=policy.completed_years,
        shard_months=policy.shard_months,
    )
    cursor = _load_cursor(
        cursor_path,
        fingerprint=request["fingerprint"],
        shard_count=len(shards),
    )
    selected_workers = workers if workers is not None else policy.workers
    if selected_workers <= 0:
        raise SpecValidationError("discovery worker count must be positive")
    context = DiscoveryContext(
        source=source_path,
        strategy_diff=difference,
        compatibility=compatibility,
        targets=all_targets,
        search_targets=targets,
        baseline_source=baseline_path,
        baseline_upstream_commit=baseline_upstream_commit,
        policy=policy,
        output=output,
        class_name=class_name,
        upstream_repository=upstream_repository,
        upstream_commit=upstream_commit,
        engine_commit=engine_commit,
        profile_path=profile,
        workers=selected_workers,
        fingerprint=request["fingerprint"],
        all_shards=shards,
    )
    started = clock()
    attempts: list[dict[str, Any]] = []
    status = "coverage_exhausted"
    candidate: dict[str, Any] | None = None
    next_shard = cursor["next_shard"]
    deferred_external = (
        dict(cursor["deferred_external"])
        if isinstance(cursor.get("deferred_external"), dict)
        else None
    )
    if compatibility.get("native_compatible") is not True:
        status = "unsupported_semantics"
    elif not targets:
        status = "unsupported_semantics" if unsearchable_targets else "no_gap"
    elif cursor.get("status") == "external_data_deferred":
        status = "external_data_deferred"
    else:
        if scout_service is None:
            from .futures_discovery_runtime import run_shard_scout

            scout = run_shard_scout
        else:
            scout = scout_service
        for shard in shards[next_shard:]:
            if clock() - started >= policy.budget_seconds:
                status = "budget_exhausted"
                break
            try:
                result = scout(shard, context)
                _validate_shard_result(result)
            except DiscoveryInfrastructureError as exc:
                defer = (
                    exc.external_http_status is not None
                    and exc.external_http_status in policy.deferred_http_statuses
                )
                result = {
                    "outcome": (
                        "external_data_deferred"
                        if defer
                        else "infrastructure_failed"
                    ),
                    "message": str(exc),
                    "target_ids": [],
                    "external_http_status": exc.external_http_status,
                }
            except BenchmarkError as exc:
                result = {
                    "outcome": "infrastructure_failed",
                    "message": str(exc),
                    "target_ids": [],
                }
            except (BranchCoverageError, SpecValidationError) as exc:
                result = {
                    "outcome": "unsupported",
                    "message": str(exc),
                    "target_ids": [],
                }
            except Exception as exc:  # pragma: no cover - defensive workflow boundary
                result = {
                    "outcome": "infrastructure_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                    "target_ids": [],
                }
            attempts.append(
                {
                    "index": shard.index,
                    "timerange": shard.timerange,
                    **result,
                }
            )
            next_shard = shard.index + 1
            if result["outcome"] == "candidate":
                raw_candidate = result.get("candidate")
                assert isinstance(raw_candidate, dict)
                candidate = raw_candidate
                status = "candidate_found"
                break
            if result["outcome"] == "unsupported":
                status = "unsupported_semantics"
                break
            if result["outcome"] == "external_data_deferred":
                status = "external_data_deferred"
                next_shard = shard.index
                deferred_external = {
                    "reason": "http_status",
                    "http_status": result["external_http_status"],
                    "retry": policy.external_retry,
                }
                break
            if result["outcome"] == "infrastructure_failed":
                status = "infrastructure_failed"
                next_shard = shard.index
                break
        else:
            status = "coverage_exhausted"
        if (
            status == "coverage_exhausted"
            and next_shard < len(shards)
            and clock() - started >= policy.budget_seconds
        ):
            status = "budget_exhausted"
    cursor_document = {
        "schema_version": DISCOVERY_CURSOR_VERSION,
        "fingerprint": request["fingerprint"],
        "next_shard": next_shard,
        "shard_count": len(shards),
        "complete": status
        in {
            "no_gap",
            "candidate_found",
            "coverage_exhausted",
            "unsupported_semantics",
        },
        "status": status,
        **(
            {"deferred_external": deferred_external}
            if deferred_external is not None
            else {}
        ),
    }
    write_json(output / "cursor.json", cursor_document)
    message = _report_message(
        status,
        attempts=attempts,
        unsearchable_targets=unsearchable_targets,
        native_compatible=compatibility.get("native_compatible") is True,
        deferred_external=deferred_external,
    )
    report = {
        "schema_version": DISCOVERY_REPORT_VERSION,
        "trading_mode": policy.trading_mode,
        "status": status,
        "message": message,
        "complete": cursor_document["complete"],
        "fingerprint": request["fingerprint"],
        "upstream_commit": upstream_commit,
        "baseline_upstream_commit": baseline_upstream_commit,
        "engine_commit": engine_commit,
        "freqtrade_image_digest": REFERENCE_INDEX_DIGEST,
        "strategy_sha256": sha256_file(source_path),
        "policy_sha256": policy.source_sha256,
        "target_count": len(request["missing_targets"]),
        "searchable_target_count": len(targets),
        "unsearchable_target_ids": sorted(
            str(target["id"]) for target in unsearchable_targets
        ),
        "searched_shard_count": len(attempts),
        "next_shard": next_shard,
        "shard_count": len(shards),
        "elapsed_seconds": max(0.0, clock() - started),
        "attempts": attempts,
        "candidate": candidate,
        "external_data": deferred_external,
        "official_fallback_available": True,
    }
    if status not in _REPORT_STATES:
        raise AssertionError(f"unknown discovery report state: {status}")
    write_json(output / "discovery-report.json", report)
    write_json(output / "run.json", report)
    return report


def discover_futures_targets(
    source: str | Path,
    strategy_diff: Mapping[str, Any] | str | Path,
    compatibility_report: Mapping[str, Any] | str | Path,
    fixtures_root: str | Path,
    policy_path: str | Path,
    output_directory: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    """Backward-compatible entry point restricted to a Futures policy."""
    policy = load_discovery_policy(policy_path)
    if policy.trading_mode != "futures":
        raise SpecValidationError("discover-futures requires a Futures policy")
    return discover_targets(
        source,
        strategy_diff,
        compatibility_report,
        fixtures_root,
        policy_path,
        output_directory,
        **kwargs,
    )


def _report_message(
    status: str,
    *,
    attempts: list[dict[str, Any]],
    unsearchable_targets: list[dict[str, Any]],
    native_compatible: bool,
    deferred_external: dict[str, Any] | None,
) -> str:
    if status == "no_gap":
        return "Existing exact fixtures cover every changed behavior target."
    if not native_compatible:
        return (
            "Static Native compatibility is blocked; official Freqtrade fallback "
            "remains available."
        )
    if status == "external_data_deferred":
        http_status = (
            deferred_external.get("http_status")
            if deferred_external is not None
            else None
        )
        suffix = f" HTTP {http_status}" if isinstance(http_status, int) else ""
        return (
            f"External market data returned{suffix}; retry is deferred until "
            "the checked identity changes or a manual retry is requested."
        )
    if attempts:
        return str(attempts[-1]["message"])
    if unsearchable_targets:
        kinds = sorted({str(target["kind"]) for target in unsearchable_targets})
        return (
            "No independently searchable transition partner exists for target kinds "
            f"{', '.join(kinds)}; a paired previous/latest proof is required."
        )
    if status == "budget_exhausted":
        return "The run budget ended before the next shard; resume from the saved cursor."
    return "Discovery completed without a branch-reaching exact candidate."


def _deep_searchable(target: Mapping[str, Any]) -> bool:
    """Return whether a new runtime surface can independently prove this target."""
    if target.get("change") == "removed" or target.get("runtime_observable") is not True:
        return False
    kind = target.get("kind")
    if kind in {"signal", "tag", "grind_level"}:
        return True
    tags = target.get("tags")
    return isinstance(tags, list) and any(
        isinstance(tag, str) and tag.strip() for tag in tags
    )


def _baseline_deep_searchable(target: Mapping[str, Any]) -> bool:
    """Return whether a previous runtime surface can prove a removed target."""
    if (
        target.get("change") != "removed"
        or target.get("runtime_observable") is not True
    ):
        return False
    kind = target.get("kind")
    if kind in {"signal", "tag", "grind_level"}:
        return True
    tags = target.get("tags")
    return isinstance(tags, list) and any(
        isinstance(tag, str) and tag.strip() for tag in tags
    )


def _load_cursor(
    cursor_path: str | Path | None,
    *,
    fingerprint: str,
    shard_count: int,
) -> dict[str, Any]:
    if cursor_path is None:
        return {
            "schema_version": DISCOVERY_CURSOR_VERSION,
            "fingerprint": fingerprint,
            "next_shard": 0,
            "shard_count": shard_count,
        }
    cursor = read_json(Path(cursor_path).resolve())
    deferred = cursor.get("deferred_external") if isinstance(cursor, dict) else None
    if (
        not isinstance(cursor, dict)
        or cursor.get("schema_version") != DISCOVERY_CURSOR_VERSION
        or cursor.get("fingerprint") != fingerprint
        or cursor.get("shard_count") != shard_count
        or not isinstance(cursor.get("next_shard"), int)
        or isinstance(cursor.get("next_shard"), bool)
        or not 0 <= cursor["next_shard"] <= shard_count
        or (
            cursor.get("status") == "external_data_deferred"
            and (
                not isinstance(deferred, dict)
                or deferred.get("reason") != "http_status"
                or not isinstance(deferred.get("http_status"), int)
                or isinstance(deferred.get("http_status"), bool)
                or deferred.get("retry") != "identity-change-or-manual"
            )
        )
    ):
        raise SpecValidationError("discovery cursor identity or bounds differ")
    return cursor


def _validate_shard_result(result: Any) -> None:
    if (
        not isinstance(result, dict)
        or result.get("outcome") not in _SHARD_OUTCOMES
        or not isinstance(result.get("message"), str)
        or not isinstance(result.get("target_ids"), list)
        or not all(isinstance(value, str) for value in result["target_ids"])
    ):
        raise SpecValidationError("discovery shard result is invalid")
    if result["outcome"] == "candidate" and not isinstance(result.get("candidate"), dict):
        raise SpecValidationError("candidate shard result lacks candidate evidence")
    if result["outcome"] == "external_data_deferred" and (
        not isinstance(result.get("external_http_status"), int)
        or isinstance(result.get("external_http_status"), bool)
        or not 400 <= result["external_http_status"] <= 599
    ):
        raise SpecValidationError(
            "deferred external-data result lacks a valid HTTP error status"
        )


def _document(
    value: Mapping[str, Any] | str | Path,
    label: str,
) -> dict[str, Any]:
    document = (
        dict(value)
        if isinstance(value, Mapping)
        else read_json(Path(value).resolve())
    )
    if not isinstance(document, dict):
        raise SpecValidationError(f"{label} must be an object")
    return document


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SpecValidationError(f"discovery policy {label} must be an object")
    return value


def _require_exact_fields(
    value: Mapping[str, Any],
    fields: set[str],
    label: str,
) -> None:
    if set(value) != fields:
        raise SpecValidationError(f"discovery policy {label} fields differ")


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SpecValidationError(f"discovery policy {label} must be positive")
    return value


def _http_statuses(value: Any) -> frozenset[int]:
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(status, int)
            or isinstance(status, bool)
            or not 400 <= status <= 599
            for status in value
        )
        or len(set(value)) != len(value)
    ):
        raise SpecValidationError(
            "discovery deferred_http_statuses must be unique HTTP error statuses"
        )
    return frozenset(value)


def _validate_sha(value: str, label: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise SpecValidationError(
            f"discovery {label} commit must be a lowercase 40-character SHA"
        )


def _validate_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SpecValidationError(
            f"discovery {label} must be a lowercase 64-character SHA-256"
        )


def _advance_month(year: int, month: int, delta: int) -> tuple[int, int]:
    ordinal = year * 12 + (month - 1) + delta
    return ordinal // 12, ordinal % 12 + 1


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def month_end(year: int, month: int) -> date:
    """Public helper used by policy tests without relying on calendar internals."""
    return date(year, month, calendar.monthrange(year, month)[1])
