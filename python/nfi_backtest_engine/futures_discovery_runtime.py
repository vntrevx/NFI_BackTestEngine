"""Production shard execution and paired exact candidate capture."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .canonical import read_json, write_json
from .config_loader import load_effective_config, sanitize_config
from .errors import BenchmarkError, BranchCoverageError, SpecValidationError
from .fixture import sha256_file
from .market_snapshot import capture_market_catalog
from .release_inputs import discover_release_universe
from .targeted_verification import assess_targeted_coverage, target_observed

if TYPE_CHECKING:
    from .futures_discovery import DiscoveryContext, SearchShard

_GRIND_LEVEL = re.compile(
    r"(?i)(?:grind|derisk|buyback|rebuy|(?:sg|gd|gm|gmd|dd|ddl|g|d))"
    r"(?:[_ -]*(?:level)?[_ -]*)?(\d+)"
)


def run_shard_scout(
    shard: SearchShard,
    context: DiscoveryContext,
) -> dict[str, Any]:
    """Run one real listing-aware Native shard and seal a candidate if it hits."""
    from .research_runner import run_research_backtest

    shared = context.output / "work" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    generated_config, pairs = _ensure_universe(context, shared)
    shard_root = context.output / "work" / f"shard-{shard.index:03d}"
    engine_output = shard_root / "engine"
    engine = run_research_backtest(
        strategy_path=context.source,
        class_name=context.class_name,
        config_path=generated_config,
        data_directory=shared / "data",
        timerange=shard.timerange,
        output_directory=engine_output,
        pairs=pairs,
        workers=context.workers,
        cache_directory=shared / "cache",
        profile_path=context.profile_path,
        resume=False,
        prepare_only=False,
        download_missing=True,
        market_metadata_path=None,
        registry_path=shared / "runs.sqlite",
        download_market_metadata=True,
        recalibrate=True,
        history_coverage_policy="available",
        trace_engine_events=True,
    )
    if not engine["complete"]:
        return {
            "outcome": "unsupported",
            "message": "Native shard remained blocked; official fallback stays available",
            "target_ids": [],
            "native_report": str(engine_output / "run.json"),
        }
    surface_path = engine_output / "trade-surface.json"
    if not surface_path.is_file():
        raise BenchmarkError("completed discovery shard has no trade surface")
    surface = read_json(surface_path)
    features = _surface_features(surface)
    reached = [
        str(target["id"])
        for target in context.search_targets
        if target_observed(target, features)
    ]
    if not reached:
        return {
            "outcome": "miss",
            "message": "shard completed without a missing target",
            "target_ids": [],
            "native_report": str(engine_output / "run.json"),
        }
    hit = _locate_hit(context.search_targets, reached, surface)
    if hit is None:
        return {
            "outcome": "unsupported",
            "message": "target was observable but no pair-bound event could be minimized",
            "target_ids": reached,
            "native_report": str(engine_output / "run.json"),
        }
    candidate = _capture_candidate(
        shard,
        context,
        generated_config=generated_config,
        data_directory=shared / "data",
        engine_market_path=engine_output / "market-metadata.json",
        hit=hit,
        target_ids=reached,
    )
    if candidate is None:
        return {
            "outcome": "miss",
            "message": (
                "a partial route hit did not prove every transition target; "
                "continue with the next shard"
            ),
            "target_ids": reached,
            "native_report": str(engine_output / "run.json"),
        }
    return {
        "outcome": "candidate",
        "message": "branch-reaching official/Native exact fixture candidate found",
        "target_ids": reached,
        "native_report": str(engine_output / "run.json"),
        "candidate": candidate,
    }


def _ensure_universe(
    context: DiscoveryContext,
    shared: Path,
) -> tuple[Path, list[str]]:
    config_path = shared / "discovery-config.json"
    pairs_path = shared / "pairs.json"
    if config_path.is_file() and pairs_path.is_file():
        pairs_document = read_json(pairs_path)
        if not isinstance(pairs_document, dict) or not isinstance(
            pairs_document.get("pairs"),
            list,
        ):
            raise SpecValidationError("cached discovery pairs are invalid")
        return config_path, list(pairs_document["pairs"])
    loaded = load_effective_config(context.policy.template_config)
    effective = sanitize_config(loaded["config"])
    if not isinstance(effective, dict):
        raise SpecValidationError("discovery template config must be an object")
    catalog_path = shared / "market-catalog.json"
    capture_market_catalog(effective, catalog_path)
    candidates_path = shared / "universe.json"
    overall_timerange = (
        f"{context.all_shards[-1].start:%Y%m%d}-"
        f"{context.all_shards[0].stop:%Y%m%d}"
    )
    universe = discover_release_universe(
        config_path=context.policy.template_config,
        market_snapshot_path=catalog_path,
        timerange=overall_timerange,
        destination=candidates_path,
        history_coverage_policy="listing-aware",
    )
    raw_pairs = universe.get("pairs")
    if not isinstance(raw_pairs, list) or not all(
        isinstance(pair, str) and pair for pair in raw_pairs
    ):
        raise SpecValidationError("discovery universe did not produce canonical pairs")
    if not raw_pairs:
        raise BenchmarkError(
            f"discovery universe contains no eligible {context.policy.trading_mode} pairs"
        )
    pairs = list(raw_pairs[: context.policy.pair_limit])
    exchange = effective.get("exchange")
    if not isinstance(exchange, dict):
        raise SpecValidationError("discovery config exchange must be an object")
    exchange["pair_whitelist"] = pairs
    write_json(config_path, effective)
    write_json(
        pairs_path,
        {
            "schema_version": "1.0.0",
            "pairs": pairs,
            "catalog_sha256": sha256_file(catalog_path),
            "universe_sha256": sha256_file(candidates_path),
        },
    )
    return config_path, pairs


def _capture_candidate(
    shard: SearchShard,
    context: DiscoveryContext,
    *,
    generated_config: Path,
    data_directory: Path,
    engine_market_path: Path,
    hit: dict[str, Any],
    target_ids: list[str],
) -> dict[str, Any] | None:
    from .probe_capture import capture_x7_probe
    from .research_runner import required_data_pairs

    pair = str(hit["pair"])
    selected_targets = list(context.targets)
    required = _required_coverage(selected_targets, hit)
    candidate_root = context.output / "candidate-fixture"
    capture_work_root = context.output / "work" / "candidate-capture"
    for attempt, timerange in enumerate(
        _candidate_timeranges(shard, hit, context.policy.context_days),
        start=1,
    ):
        output = context.output / "work" / f"candidate-fixture-attempt-{attempt}"
        work = capture_work_root / f"latest-attempt-{attempt}"
        spec_path = context.output / "work" / f"candidate-probe-{attempt}.json"
        market_path = context.output / "work" / f"candidate-market-{attempt}.json"
        _filter_market_snapshot(engine_market_path, market_path, [pair])
        config = read_json(generated_config)
        exchange = config.get("exchange") if isinstance(config, dict) else None
        if not isinstance(exchange, dict):
            raise SpecValidationError("generated discovery config is invalid")
        exchange["pair_whitelist"] = [pair]
        candidate_config = context.output / "work" / f"candidate-config-{attempt}.json"
        write_json(candidate_config, config)
        data_pairs = required_data_pairs({"pairs": [pair]}, config)
        informative_pairs = [item for item in data_pairs if item != pair]
        spec = {
            "schema_version": "1.0.0",
            "fixture": {
                "id": (
                    f"future-nfi-{context.policy.trading_mode}-"
                    f"{context.fingerprint[:16]}"
                ),
                "description": (
                    "Automatically discovered branch fixture with independent "
                    "official and Native exact evidence."
                ),
                "probe_kind": "future-nfi-target",
                "required_coverage": required,
            },
            "upstream": {
                "repository": context.upstream_repository,
                "commit": context.upstream_commit,
            },
            "strategy": {
                "source": str(context.source),
                "class_name": context.class_name,
            },
            "config": {
                "source": str(candidate_config),
                "overrides": {},
                "remove_paths": [],
            },
            "data": {
                "directory": str(data_directory),
                "timerange": timerange,
                "pairs": [pair],
                "informative_pairs": informative_pairs,
            },
            "markets": {
                "engine": str(market_path),
                "reference": None,
            },
            "execution": {
                "profile": str(context.profile_path),
                "audit_timestamps_ms": [],
            },
        }
        write_json(spec_path, spec)
        try:
            capture = capture_x7_probe(
                spec_path,
                output,
                work,
                timeout_seconds=max(1, context.policy.budget_seconds),
                workers=context.workers,
            )
            baseline_manifest = _capture_transition_baseline(
                context,
                attempt=attempt,
                latest_spec=spec,
                latest_spec_path=spec_path,
                required=_baseline_required_coverage(hit),
                timeout_seconds=max(1, context.policy.budget_seconds),
                workers=context.workers,
            )
            coverage = assess_targeted_coverage(
                selected_targets,
                baseline_manifest=baseline_manifest or output / "manifest.json",
                candidate_manifest=output / "manifest.json",
            )
        except (BenchmarkError, BranchCoverageError, SpecValidationError):
            continue
        if not coverage["complete"] or not coverage["changed_branch_reached"]:
            continue
        baseline_output = (
            baseline_manifest.parent
            if baseline_manifest is not None
            else None
        )
        logical_bytes = sum(
            path.stat().st_size
            for root in (output, baseline_output)
            if root is not None
            for path in root.rglob("*")
            if path.is_file()
        )
        if logical_bytes > context.policy.max_candidate_bytes:
            return None
        output.rename(candidate_root)
        baseline_record = None
        if baseline_output is not None:
            baseline_destination = candidate_root / "transition-baseline"
            baseline_output.rename(baseline_destination)
            baseline_manifest = baseline_destination / "manifest.json"
            baseline_record = {
                "manifest_path": str(baseline_manifest),
                "manifest_sha256": sha256_file(baseline_manifest),
                "upstream_commit": context.baseline_upstream_commit,
            }
        return {
            "fixture_id": capture["fixture_id"],
            "path": str(candidate_root),
            "manifest_path": str(candidate_root / "manifest.json"),
            "manifest_sha256": sha256_file(candidate_root / "manifest.json"),
            "logical_bytes": logical_bytes,
            "target_ids": coverage["reached_target_ids"],
            "proved_target_ids": coverage["reached_target_ids"],
            "target_proofs": coverage["target_proofs"],
            "pair": pair,
            "timerange": timerange,
            "trade_surface_exact": True,
            "full_state_exact": True,
            "transition_baseline": baseline_record,
        }
    return None


def _capture_transition_baseline(
    context: DiscoveryContext,
    *,
    attempt: int,
    latest_spec: Mapping[str, Any],
    latest_spec_path: Path,
    required: dict[str, Any],
    timeout_seconds: int,
    workers: int,
) -> Path | None:
    if not any(
        target.get("change") in {"removed", "changed"}
        for target in context.targets
    ):
        return None
    if (
        context.baseline_source is None
        or context.baseline_upstream_commit is None
    ):
        raise SpecValidationError(
            "changed or removed targets require a previous strategy source and commit"
        )
    from .probe_capture import capture_x7_probe

    baseline_spec = {
        **dict(latest_spec),
        "fixture": {
            **dict(latest_spec["fixture"]),
            "id": f"{latest_spec['fixture']['id']}-baseline",
            "required_coverage": required,
        },
        "upstream": {
            "repository": context.upstream_repository,
            "commit": context.baseline_upstream_commit,
        },
        "strategy": {
            "source": str(context.baseline_source),
            "class_name": context.class_name,
        },
    }
    baseline_spec_path = latest_spec_path.with_name(
        f"candidate-baseline-probe-{attempt}.json"
    )
    write_json(baseline_spec_path, baseline_spec)
    output = context.output / "work" / f"candidate-baseline-attempt-{attempt}"
    work = context.output / "work" / "candidate-capture" / f"baseline-attempt-{attempt}"
    capture_x7_probe(
        baseline_spec_path,
        output,
        work,
        timeout_seconds=timeout_seconds,
        workers=workers,
    )
    return output / "manifest.json"


def _surface_features(surface: Any) -> dict[str, set[str] | set[int]]:
    if not isinstance(surface, Mapping):
        raise SpecValidationError("discovery trade surface must be an object")
    trades = surface.get("trades")
    if not isinstance(trades, list):
        raise SpecValidationError("discovery trade surface has no trades")
    tags: set[str] = set()
    for trade in trades:
        if not isinstance(trade, Mapping):
            continue
        entry = trade.get("entry_tag")
        if isinstance(entry, str) and entry.strip():
            tags.add(entry.strip())
        exit_reason = trade.get("exit_reason")
        if isinstance(exit_reason, str) and exit_reason.strip():
            tags.add(exit_reason.strip())
        orders = trade.get("orders")
        if isinstance(orders, list):
            for order in orders:
                tag = order.get("tag") if isinstance(order, Mapping) else None
                if isinstance(tag, str) and tag.strip():
                    tags.add(tag.strip())
    tokens = {token for tag in tags for token in tag.split() if token}
    grind_levels = {
        int(match.group(1)) for tag in tags for match in _GRIND_LEVEL.finditer(tag)
    }
    return {
        "callbacks": set(),
        "tags": tags,
        "tokens": tokens,
        "grind_levels": grind_levels,
    }


def _locate_hit(
    targets: list[dict[str, Any]],
    reached: list[str],
    surface: Any,
) -> dict[str, Any] | None:
    reached_targets = [
        target for target in targets if str(target["id"]) in reached
    ]
    trades = surface.get("trades") if isinstance(surface, Mapping) else None
    if not isinstance(trades, list):
        return None
    for trade in trades:
        if not isinstance(trade, Mapping):
            continue
        features = _surface_features({"trades": [trade]})
        if not any(target_observed(target, features) for target in reached_targets):
            continue
        orders = trade.get("orders")
        order_records = orders if isinstance(orders, list) else []
        timestamps = [
            value
            for value in (
                trade.get("open_timestamp_ms"),
                trade.get("close_timestamp_ms"),
                *(
                    order.get("filled_timestamp_ms")
                    for order in order_records
                    if isinstance(order, Mapping)
                ),
            )
            if isinstance(value, int) and not isinstance(value, bool)
        ]
        pair = trade.get("pair")
        opened = trade.get("open_timestamp_ms")
        if isinstance(pair, str) and pair and isinstance(opened, int) and timestamps:
            return {
                "pair": pair,
                "open_timestamp_ms": opened,
                "event_timestamp_ms": max(timestamps),
                "entry_tag": str(trade.get("entry_tag", "")).strip(),
                "exit_reason": str(trade.get("exit_reason", "")).strip(),
            }
    return None


def _required_coverage(
    targets: list[dict[str, Any]],
    hit: Mapping[str, Any],
) -> dict[str, Any]:
    entry_tag = str(hit.get("entry_tag", "")).strip()
    entry_tokens = set(entry_tag.split())
    requested = {
        str(value).strip()
        for target in targets
        if target.get("change") != "removed"
        for value in (
            [target.get("value")]
            if target.get("kind") in {"signal", "tag"}
            else target.get("tags", [])
        )
        if isinstance(value, str) and value.strip()
    }
    return {
        "callbacks": [],
        "entry_tags": sorted(requested & entry_tokens),
        "compound_tags": (
            [entry_tag]
            if len(entry_tokens) > 1 and requested & entry_tokens
            else []
        ),
        "protection_methods": [],
        "exit_reasons": (
            [exit_reason]
            if (exit_reason := str(hit.get("exit_reason", "")).strip()) in requested
            else []
        ),
        "sides": [],
        "minimum_lock_count": 0,
        "minimum_distinct_leverages": 1,
        "minimum_funded_trades": 0,
        "require_rejected_locked_entry": False,
    }


def _baseline_required_coverage(hit: Mapping[str, Any]) -> dict[str, Any]:
    entry_tag = str(hit.get("entry_tag", "")).strip()
    return {
        "callbacks": [],
        "entry_tags": sorted(set(entry_tag.split())),
        "compound_tags": [entry_tag] if len(entry_tag.split()) > 1 else [],
        "protection_methods": [],
        "exit_reasons": [],
        "sides": [],
        "minimum_lock_count": 0,
        "minimum_distinct_leverages": 1,
        "minimum_funded_trades": 0,
        "require_rejected_locked_entry": False,
    }


def _candidate_timeranges(
    shard: SearchShard,
    hit: Mapping[str, Any],
    context_days: int,
) -> list[str]:
    opened = datetime.fromtimestamp(int(hit["open_timestamp_ms"]) / 1000, tz=UTC).date()
    event = datetime.fromtimestamp(int(hit["event_timestamp_ms"]) / 1000, tz=UTC).date()
    start = max(shard.start, opened - timedelta(days=context_days))
    stop = min(shard.stop, event + timedelta(days=context_days + 1))
    if stop <= start:
        stop = min(shard.stop, start + timedelta(days=1))
    minimal = f"{start:%Y%m%d}-{stop:%Y%m%d}"
    return [minimal] if minimal == shard.timerange else [minimal, shard.timerange]


def _filter_market_snapshot(source: Path, destination: Path, pairs: list[str]) -> None:
    document = read_json(source)
    markets = document.get("markets") if isinstance(document, dict) else None
    if not isinstance(markets, dict) or not all(pair in markets for pair in pairs):
        raise SpecValidationError("discovery market snapshot lacks candidate pair")
    document["pairs"] = pairs
    document["markets"] = {pair: markets[pair] for pair in pairs}
    write_json(destination, document)
