"""Fail-closed reachability audit for source-defined stateful strategy routes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .callback_source_ir import compile_callback_source_ir
from .canonical import write_json
from .config_loader import load_effective_config
from .errors import StrategyAnalysisError
from .hot_ir import build_hot_callback_ir
from .specs import STATEFUL_COVERAGE_SCHEMA, validate_schema
from .stateful_coverage_contracts import (
    callback_coverage,
    manager_operation,
    native_contracts,
    reachable_route_gaps,
)
from .stateful_coverage_source import (
    active_entry_tags,
    compile_entry_signals,
    dormant_unsupported_routes,
    empty_entry_signals,
    live_only_exclusions,
    source_routes,
)
from .strategy_ir import analyze_strategy

STATEFUL_COVERAGE_VERSION = "stateful-coverage-v1"


def build_stateful_coverage(
    source: str | Path,
    *,
    class_name: str | None = None,
    trading_mode: str = "spot",
    config_path: str | Path | None = None,
    upstream_repository: str | None = None,
    upstream_commit: str | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Prove that every backtest-reachable stateful route has a Native contract.

    Signal numbers and route values remain source data. The auditor proves the
    source's entry-tag emission shape, intersects it with callback route data,
    and then checks the serialized Native programs. A newly reachable tag is
    therefore a gap until a source-derived program covers it.
    """
    if trading_mode not in {"spot", "futures"}:
        raise StrategyAnalysisError("stateful coverage mode must be spot or futures")
    if (upstream_repository is None) != (upstream_commit is None):
        raise StrategyAnalysisError(
            "upstream repository and commit must be supplied together"
        )
    if upstream_commit is not None and re.fullmatch(r"[0-9a-f]{40}", upstream_commit) is None:
        raise StrategyAnalysisError("upstream commit must be a 40-character lowercase SHA")

    path = Path(source).resolve()
    analysis = analyze_strategy(path, class_name=class_name)
    _require_selected_static_strategy(analysis)
    strategy = analysis["strategies"][0]
    callback_source = compile_callback_source_ir(
        path,
        class_name=class_name,
        trading_mode=trading_mode,
        analysis=analysis,
    )
    config, config_identity = _load_config(config_path)

    compilation_errors: list[dict[str, Any]] = []
    try:
        entry_signals = compile_entry_signals(path, strategy)
    except StrategyAnalysisError as exc:
        entry_signals = empty_entry_signals()
        compilation_errors.append(
            {
                "code": "ENTRY_SIGNAL_PROOF_MISSING",
                "message": str(exc),
            }
        )

    try:
        hot_ir = build_hot_callback_ir(
            analysis,
            trading_mode=trading_mode,
            run_mode="backtest",
            config=config,
        )
    except StrategyAnalysisError as exc:
        hot_ir = None
        compilation_errors.append(
            {
                "code": "EXACT_LOWERING_REVIEW_REQUIRED",
                "message": str(exc),
            }
        )

    callbacks, callback_gaps = callback_coverage(callback_source, hot_ir)
    operation, manager_identity = manager_operation(hot_ir)
    native_programs, exit_tags, adjustment_tags = native_contracts(operation)
    active_tags = active_entry_tags(entry_signals, trading_mode)
    route_records = source_routes(
        callback_source,
        active_tags=active_tags,
        exit_tags=exit_tags,
        adjustment_tags=adjustment_tags,
    )
    dormant_routes = dormant_unsupported_routes(
        route_records,
        entry_signals=entry_signals,
        trading_mode=trading_mode,
    )
    live_exclusions, live_gaps = live_only_exclusions(
        path,
        strategy_name=strategy["name"],
        callback_source=callback_source,
        operation=operation,
    )
    route_gaps = reachable_route_gaps(
        active_tags,
        exit_tags=exit_tags,
        adjustment_tags=adjustment_tags,
        adjustment_enabled=strategy.get("constants", {}).get("position_adjustment_enable")
        is True,
    )
    compilation_gaps = [
        {
            "code": item["code"],
            "callback": None,
            "side": None,
            "tags": [],
            "message": item["message"],
        }
        for item in compilation_errors
    ]
    reachable_gaps = [
        *compilation_gaps,
        *callback_gaps,
        *route_gaps,
        *live_gaps,
    ]
    source_route_tags = {
        side: sorted(
            {
                tag
                for route in route_records
                if route["side"] == side
                for tag in route["declared_tags"]
            }
        )
        for side in ("long", "short")
    }
    emitted_tags = {side: list(active_tags[side]) for side in ("long", "short")}
    closure_complete = not reachable_gaps
    report: dict[str, Any] = {
        "schema_version": STATEFUL_COVERAGE_VERSION,
        "source": analysis["source"],
        "selected_class": strategy["name"],
        "trading_mode": trading_mode,
        "run_mode": "backtest",
        "config": config_identity,
        "upstream": (
            {
                "repository": upstream_repository,
                "commit": upstream_commit,
            }
            if upstream_repository is not None and upstream_commit is not None
            else None
        ),
        "callback_source_ir": {
            "schema_version": callback_source["schema_version"],
            "fingerprint": callback_source["fingerprint"],
            "call_edge_count": len(callback_source["call_edges"]),
        },
        "native_runtime": manager_identity,
        "entry_signals": entry_signals,
        "callbacks": callbacks,
        "source_routes": route_records,
        "native_contracts": native_programs,
        "dormant_stateful_routes": dormant_routes,
        "live_only_exclusions": live_exclusions,
        "reachable_stateful_gaps": reachable_gaps,
        "summary": {
            "active_callback_count": sum(item["active_for_mode"] for item in callbacks),
            "callback_graph_method_count": len(
                {
                    method
                    for callback in callbacks
                    if callback["active_for_mode"]
                    for method in callback["reachable_methods"]
                }
            ),
            "source_route_tag_count": sum(len(tags) for tags in source_route_tags.values()),
            "emitted_entry_tag_count": sum(len(tags) for tags in emitted_tags.values()),
            "native_exit_tag_count": sum(len(tags) for tags in exit_tags.values()),
            "native_adjustment_tag_count": sum(
                len(tags) for tags in adjustment_tags.values()
            ),
            "dormant_unsupported_tag_count": len(dormant_routes),
            "live_only_exclusion_count": len(live_exclusions),
            "reachable_stateful_gap_count": len(reachable_gaps),
            "closure_complete": closure_complete,
        },
        "qualification": {
            "level": "latest-checked-native-closure",
            "native_compatible": closure_complete,
            "full_state_certified": False,
            "basis": "static-reachability-and-serialized-native-contracts",
        },
        "compilation_errors": compilation_errors,
    }
    report["fingerprint"] = _fingerprint(report)
    validate_schema(report, STATEFUL_COVERAGE_SCHEMA)
    if output_path is not None:
        write_json(output_path, report)
    return report


def _require_selected_static_strategy(analysis: dict[str, Any]) -> None:
    errors = [item for item in analysis["diagnostics"] if item["severity"] == "error"]
    if errors:
        first = errors[0]
        location = first["location"]
        raise StrategyAnalysisError(
            f"{location['path']}:{location['line']}:{location['column']}: "
            f"{first['code']}: {first['message']}"
        )
    if len(analysis["strategies"]) != 1:
        raise StrategyAnalysisError("stateful coverage requires one selected strategy")


def _load_config(
    config_path: str | Path | None,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    if config_path is None:
        return {}, None
    loaded = load_effective_config(config_path)
    return loaded["config"], {
        "path": str(Path(config_path).resolve()),
        "sha256": loaded["sha256"],
    }


def _fingerprint(report: dict[str, Any]) -> str:
    identity = {
        key: _without_checkout_paths(value)
        for key, value in report.items()
        if key != "fingerprint"
    }
    source = identity.get("source")
    if isinstance(source, dict):
        source.pop("path", None)
    config = identity.get("config")
    if isinstance(config, dict):
        config.pop("path", None)
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _without_checkout_paths(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _without_checkout_paths(item)
            for key, item in value.items()
            if key != "path"
        }
    if isinstance(value, list):
        return [_without_checkout_paths(item) for item in value]
    return value
