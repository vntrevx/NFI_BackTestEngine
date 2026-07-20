"""Branch-reaching evidence checks for official X7 probe fixtures.

Parity alone proves that two outputs match; it does not prove that the branch a
fixture was designed for actually ran.  This module binds the observer report to
the immutable official artifacts, independently derives every observable field,
and then checks the manifest's required coverage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import read_json
from .errors import SpecValidationError
from .state_trace import iter_validated_trace_events, trace_summary

COVERAGE_REPORT_VERSION = "1.0.0"
_OBSERVED_FIELDS = {
    "callbacks",
    "entry_tags",
    "compound_tags",
    "protection_methods",
    "exit_reasons",
    "sides",
    "leverages",
    "lock_count",
    "rejected_locked_entry",
}


def validate_fixture_coverage(
    manifest_path: str | Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Validate a v3 observer report and require every declared branch."""
    root = Path(manifest_path).resolve().parent
    artifacts = manifest["artifacts"]
    report_path = root / artifacts["coverage_report"]["path"]
    report = read_json(report_path)
    _validate_report_shape(report)
    expected_bindings = {
        "trade_surface_sha256": artifacts["trade_surface"]["sha256"],
        "state_trace_sha256": artifacts["state_trace"]["sha256"],
    }
    if report["bindings"] != expected_bindings:
        raise SpecValidationError(
            "coverage report is not bound to the fixture trade surface and state trace"
        )

    surface = read_json(root / artifacts["trade_surface"]["path"])
    trace_path = root / artifacts["state_trace"]["path"]
    trace = trace_summary(trace_path)
    if not trace["include_state"]:
        raise SpecValidationError("v3 branch fixture requires a materialized full-state trace")

    protection_methods = configured_protection_methods(
        root / _one_input(manifest, "strategy")["path"],
        root / _one_input(manifest, "config")["path"],
        class_name=manifest["freqtrade"]["strategy"],
    )
    derived = derive_fixture_observed(
        surface,
        trace_path,
        configured_protection_methods=protection_methods,
    )
    observed = report["observed"]
    if len(observed["protection_methods"]) > observed["lock_count"]:
        raise SpecValidationError(
            "coverage report cannot observe more protection methods than locks"
        )
    if observed["rejected_locked_entry"] and observed["lock_count"] == 0:
        raise SpecValidationError(
            "coverage report cannot reject a locked entry without an observed lock"
        )
    for field, value in derived.items():
        if observed[field] != value:
            raise SpecValidationError(
                f"coverage report {field} differs from immutable official artifacts"
            )

    assessment = assess_required_coverage(manifest["required_coverage"], observed)
    if not assessment["met"]:
        missing = ", ".join(assessment["missing"])
        raise SpecValidationError(f"fixture did not reach required branch coverage: {missing}")
    return {
        **assessment,
        "observed": observed,
        "report_path": str(report_path),
    }


def assess_required_coverage(
    required: dict[str, Any],
    observed: dict[str, Any],
) -> dict[str, Any]:
    """Return a deterministic, human-readable missing-branch list."""
    missing: list[str] = []
    for field in (
        "callbacks",
        "entry_tags",
        "compound_tags",
        "protection_methods",
        "exit_reasons",
        "sides",
    ):
        absent = sorted(set(required[field]) - set(observed[field]))
        missing.extend(f"{field}:{value}" for value in absent)
    if observed["lock_count"] < required["minimum_lock_count"]:
        missing.append(
            "lock_count:"
            f"{observed['lock_count']}<{required['minimum_lock_count']}"
        )
    distinct_leverages = len(set(observed["leverages"]))
    if distinct_leverages < required["minimum_distinct_leverages"]:
        missing.append(
            "distinct_leverages:"
            f"{distinct_leverages}<{required['minimum_distinct_leverages']}"
        )
    if required["require_rejected_locked_entry"] and not observed[
        "rejected_locked_entry"
    ]:
        missing.append("rejected_locked_entry:false")
    return {
        "met": not missing,
        "missing": missing,
    }


def derive_fixture_observed(
    surface: dict[str, Any],
    trace_path: Path,
    *,
    configured_protection_methods: list[str] | None = None,
) -> dict[str, Any]:
    """Derive coverage only from sealed inputs and official runtime artifacts."""
    trades = surface["trades"]
    complete_tags = sorted(
        {
            tag.strip()
            for trade in trades
            if isinstance((tag := trade.get("entry_tag")), str) and tag.strip()
        }
    )
    tokens = sorted(
        {
            token
            for tag in complete_tags
            for token in tag.split()
            if token
        }
    )
    events = list(iter_validated_trace_events(trace_path))
    callbacks = sorted(
        {
            callback
            for event in events
            if isinstance((callback := event.get("callback")), str) and callback
        }
    )
    locks = _observed_locks(surface, events)
    configured_methods = sorted(set(configured_protection_methods or []))
    # A lock proves one configured method ran only when that fixture enables
    # exactly one method. Multi-method configurations cannot attribute the lock
    # without instrumenting Freqtrade internals and therefore fail closed.
    methods = configured_methods if locks and len(configured_methods) == 1 else []
    return {
        "callbacks": callbacks,
        "entry_tags": tokens,
        "compound_tags": sorted(tag for tag in complete_tags if len(tag.split()) > 1),
        "exit_reasons": sorted(
            {
                reason
                for trade in trades
                if isinstance((reason := trade.get("exit_reason")), str) and reason
            }
        ),
        "sides": sorted(
            {
                direction
                for trade in trades
                if isinstance((direction := trade.get("direction")), str) and direction
            }
        ),
        "leverages": sorted(
            {
                leverage
                for trade in trades
                if isinstance((leverage := trade.get("leverage")), str) and leverage
            }
        ),
        "lock_count": len(locks),
        "protection_methods": methods,
        "rejected_locked_entry": any(
            event.get("phase") == "entry.lock_rejected" for event in events
        ),
    }


def configured_protection_methods(
    strategy_path: str | Path,
    config_path: str | Path,
    *,
    class_name: str,
) -> list[str]:
    """Return static protection methods enabled by the sealed official inputs."""
    # Imported lazily because fixture validation calls this module while
    # strategy_ir imports the shared fixture hashing helpers.
    from .strategy_ir import analyze_strategy

    config = read_json(config_path)
    if not isinstance(config, dict) or config.get("enable_protections") is not True:
        return []
    analysis = analyze_strategy(strategy_path, class_name=class_name)
    strategies = analysis.get("strategies")
    if not isinstance(strategies, list) or len(strategies) != 1:
        raise SpecValidationError("protection probe must select exactly one strategy")
    strategy = strategies[0]
    definitions = strategy.get("protections")
    if strategy.get("protections_static") is not True or not isinstance(
        definitions,
        list,
    ):
        raise SpecValidationError("protection probe requires static strategy protections")
    methods: list[str] = []
    for index, definition in enumerate(definitions):
        method = definition.get("method") if isinstance(definition, dict) else None
        if not isinstance(method, str) or not method:
            raise SpecValidationError(
                f"protection probe definition {index} has no static method"
            )
        methods.append(method)
    return sorted(set(methods))


def _observed_locks(
    surface: dict[str, Any],
    events: list[dict[str, Any]],
) -> list[tuple[Any, ...]]:
    """Collect unique locks from the official surface and materialized states."""
    locks: set[tuple[Any, ...]] = set()
    for lock in surface.get("locks", []):
        locks.add(_lock_identity(lock))
    for event in events:
        state = event.get("state")
        state_locks = state.get("locks", []) if isinstance(state, dict) else []
        for lock in state_locks:
            locks.add(_lock_identity(lock))
    return sorted(locks, key=lambda item: tuple(str(value) for value in item))


def _lock_identity(lock: Any) -> tuple[Any, ...]:
    if not isinstance(lock, dict):
        raise SpecValidationError("official lock state must contain objects")
    return (
        lock.get("pair"),
        lock.get("side"),
        lock.get("lock_timestamp_ms", lock.get("lock_timestamp")),
        lock.get("lock_end_timestamp_ms", lock.get("lock_end_timestamp")),
        lock.get("reason"),
    )


def _one_input(manifest: dict[str, Any], role: str) -> dict[str, Any]:
    candidates = [item for item in manifest["inputs"] if item["role"] == role]
    if len(candidates) != 1:
        raise SpecValidationError(f"fixture requires exactly one {role!r} input")
    return candidates[0]


def _validate_report_shape(report: Any) -> None:
    required = {"schema_version", "source", "bindings", "observed"}
    if not isinstance(report, dict) or set(report) != required:
        raise SpecValidationError("coverage report fields differ from the v1 contract")
    if report["schema_version"] != COVERAGE_REPORT_VERSION:
        raise SpecValidationError("unsupported coverage report version")
    if report["source"] != "official-freqtrade-observer":
        raise SpecValidationError("coverage report must come from the official observer")
    bindings = report["bindings"]
    if not isinstance(bindings, dict) or set(bindings) != {
        "trade_surface_sha256",
        "state_trace_sha256",
    }:
        raise SpecValidationError("coverage report bindings are invalid")
    observed = report["observed"]
    if not isinstance(observed, dict) or set(observed) != _OBSERVED_FIELDS:
        raise SpecValidationError("coverage report observed fields are invalid")
    for field in _OBSERVED_FIELDS - {"lock_count", "rejected_locked_entry"}:
        values = observed[field]
        if (
            not isinstance(values, list)
            or values != sorted(set(values))
            or not all(isinstance(value, str) and value for value in values)
        ):
            raise SpecValidationError(f"coverage report {field} must be sorted unique strings")
    if (
        not isinstance(observed["lock_count"], int)
        or isinstance(observed["lock_count"], bool)
        or observed["lock_count"] < 0
    ):
        raise SpecValidationError("coverage report lock_count must be non-negative")
    if not isinstance(observed["rejected_locked_entry"], bool):
        raise SpecValidationError(
            "coverage report rejected_locked_entry must be boolean"
        )
