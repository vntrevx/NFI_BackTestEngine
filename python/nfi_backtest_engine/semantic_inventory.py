"""Deterministic ownership and Native-boundary inventory for strategy semantics."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import read_json, write_json
from .config_loader import load_effective_config
from .errors import StrategyAnalysisError
from .hot_ir import build_hot_callback_ir
from .semantic_registry import (
    build_semantic_obligation_registry as _build_semantic_obligation_registry,
)
from .semantic_registry import (
    load_semantic_obligation_registry as load_semantic_obligation_registry,
)
from .semantic_registry import (
    validate_semantic_obligation_registry as validate_semantic_obligation_registry,
)
from .semantic_registry import (
    write_semantic_obligation_registry as write_semantic_obligation_registry,
)
from .specs import SEMANTIC_INVENTORY_SCHEMA, validate_schema
from .strategy_ir import analyze_strategy

SEMANTIC_INVENTORY_VERSION = "semantic-inventory-v1"

_OWNERSHIP_CONTRACT = (
    {
        "domain": "indicator-and-signal-conditions",
        "semantic_owner": "nfi-strategy",
        "runtime_responsibility": "evaluate strategy dataframe expressions",
        "native_target": "vector-and-scalar-ir",
    },
    {
        "domain": "entry-and-exit-tag-creation",
        "semantic_owner": "nfi-strategy",
        "runtime_responsibility": "preserve source order and emitted tag text",
        "native_target": "tag-ir",
    },
    {
        "domain": "tag-storage-and-callback-delivery",
        "semantic_owner": "freqtrade",
        "runtime_responsibility": "store tags and expose them to callbacks unchanged",
        "native_target": "freqtrade-semantic-kernel",
    },
    {
        "domain": "grind-derisk-and-buyback-decisions",
        "semantic_owner": "nfi-strategy",
        "runtime_responsibility": "execute stateful source-defined transitions",
        "native_target": "state-machine-ir",
    },
    {
        "domain": "orders-wallet-fees-and-precision",
        "semantic_owner": "freqtrade",
        "runtime_responsibility": "apply event ordering and accounting contracts",
        "native_target": "freqtrade-semantic-kernel",
    },
    {
        "domain": "funding-liquidation-and-protections",
        "semantic_owner": "freqtrade-and-exchange",
        "runtime_responsibility": "apply pinned Freqtrade and market rules",
        "native_target": "futures-and-protection-kernel",
    },
)

_KERNEL_DOMAINS = (
    {
        "domain": "candle-and-pair-scheduler",
        "implementation": "rust/crates/nfi-sim-core/src/simulation",
        "verification_requirement": "official event-order fixture",
    },
    {
        "domain": "callback-projection-and-evaluation",
        "implementation": "rust/crates/nfi-sim-core/src/callbacks",
        "verification_requirement": "callback trace and full-state exact",
    },
    {
        "domain": "orders-wallet-stake-fees-and-precision",
        "implementation": "rust/crates/nfi-sim-core/src/execution",
        "verification_requirement": "order, wallet, and full-state exact",
    },
    {
        "domain": "portfolio-and-shared-wallet",
        "implementation": "rust/crates/nfi-sim-core/src/portfolio.rs",
        "verification_requirement": "same-timestamp multi-pair exact",
    },
    {
        "domain": "funding-and-liquidation",
        "implementation": "rust/crates/nfi-sim-core/src/futures.rs",
        "verification_requirement": "official Futures long and short exact",
    },
    {
        "domain": "protections-and-pair-locks",
        "implementation": "rust/crates/nfi-sim-core/src/protections",
        "verification_requirement": "protection chronology and full-state exact",
    },
)

# This is an inventory of existing engine adapters, not a strategy dispatch table.
# Runtime selection remains inside the validated lowering pipeline.
_SOURCE_BOUND_ADAPTERS = (
    {
        "backend": "rust-nfi-x7-trade-manager",
        "callbacks": ["custom_exit"],
        "implementation": "python/nfi_backtest_engine/x7 and rust/crates/nfi-sim-core/src/nfi",
        "migration_target": "generic-state-machine-ir",
    },
    {
        "backend": "rust-nfi-x7-position-adjustment",
        "callbacks": ["adjust_trade_position"],
        "implementation": "python/nfi_backtest_engine/x7 and rust/crates/nfi-sim-core/src/nfi",
        "migration_target": "generic-state-machine-ir",
    },
    {
        "backend": "rust-nfi-x7-leverage",
        "callbacks": ["leverage"],
        "implementation": "python/nfi_backtest_engine/callback_lowering.py",
        "migration_target": "generic-callback-ir",
    },
)


def build_semantic_inventory(
    source: str | Path,
    *,
    class_name: str | None = None,
    trading_mode: str | None = None,
    config_path: str | Path | None = None,
    fixtures_root: str | Path | None = None,
    source_root: str | Path | None = None,
    upstream_repository: str | None = None,
    upstream_commit: str | None = None,
    upstream_ref: str | None = None,
    upstream_source_path: str | None = None,
    upstream_fetch_timeout_seconds: int = 180,
    output_path: str | Path | None = None,
    _upstream_observation: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Inventory current execution ownership without executing strategy Python."""
    if upstream_ref is not None and _upstream_observation is None:
        logical_source = upstream_source_path or Path(source).name
        if upstream_repository is None or not upstream_repository:
            _upstream_observation = _failed_upstream_observation(
                upstream_ref,
                logical_source,
                method="invalid-upstream-configuration-v1",
                status="invalid-configuration",
                blocker_code="INVALID_UPSTREAM_CONFIGURATION",
            )
        elif not _is_exact_upstream_ref(upstream_ref):
            _upstream_observation = _failed_upstream_observation(
                upstream_ref,
                logical_source,
                method="invalid-upstream-ref-v1",
                status="invalid-ref",
                blocker_code="INVALID_UPSTREAM_REF",
            )
        elif upstream_commit is not None and re.fullmatch(
            r"[0-9a-f]{40}", upstream_commit
        ) is None:
            _upstream_observation = _failed_upstream_observation(
                upstream_ref,
                logical_source,
                method="invalid-upstream-commit-v1",
                status="invalid-commit",
                blocker_code="INVALID_UPSTREAM_COMMIT",
            )
        elif upstream_source_path is None:
            _upstream_observation = _failed_upstream_observation(
                upstream_ref,
                logical_source,
                method="invalid-upstream-configuration-v1",
                status="invalid-configuration",
                blocker_code="INVALID_UPSTREAM_CONFIGURATION",
            )
        else:
            with tempfile.TemporaryDirectory(prefix="nfi-semantic-upstream-") as temporary:
                checkout, observation = _fetch_upstream_ref_once(
                    Path(temporary),
                    repository=upstream_repository,
                    ref=upstream_ref,
                    source_path=upstream_source_path,
                    timeout_seconds=upstream_fetch_timeout_seconds,
                )
                if checkout is not None:
                    return build_semantic_inventory(
                        checkout / observation["source_path"],
                        class_name=class_name,
                        trading_mode=trading_mode,
                        config_path=config_path,
                        fixtures_root=fixtures_root,
                        source_root=checkout,
                        upstream_repository=upstream_repository,
                        upstream_commit=upstream_commit,
                        upstream_ref=upstream_ref,
                        upstream_source_path=observation["source_path"],
                        upstream_fetch_timeout_seconds=upstream_fetch_timeout_seconds,
                        output_path=output_path,
                        _upstream_observation=observation,
                    )
                _upstream_observation = observation

    analysis = analyze_strategy(source, class_name=class_name)
    _require_selected_static_strategy(analysis)
    strategy = analysis["strategies"][0]

    config: dict[str, Any] = {}
    config_identity: dict[str, Any] | None = None
    if config_path is not None:
        loaded = load_effective_config(config_path)
        config = loaded["config"]
        config_identity = {
            "path": str(Path(config_path).resolve()),
            "sha256": loaded["sha256"],
        }
    effective_mode = trading_mode or str(config.get("trading_mode", "spot"))
    if effective_mode not in {"spot", "futures"}:
        raise StrategyAnalysisError("semantic inventory trading mode must be spot or futures")

    compilation_errors: list[dict[str, str]] = []
    try:
        hot_ir = build_hot_callback_ir(
            analysis,
            trading_mode=effective_mode,
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

    fixture_coverage = _fixture_coverage(
        fixtures_root,
        source_sha256=analysis["source"]["sha256"],
        trading_mode=effective_mode,
    )
    exact_fixtures = fixture_coverage["exact_source_fixtures"]
    callbacks = _callback_inventory(strategy, hot_ir, exact_fixtures)
    routes = _route_inventory(hot_ir, exact_fixtures)
    source_boundaries = _source_boundary_inventory(callbacks, hot_ir, exact_fixtures)
    lowering_complete = hot_ir is not None
    del hot_ir
    vector_methods = [
        {
            "name": method["name"],
            "owner": "nfi-strategy",
            "current_lane": "python-batch-vector-worker",
            "native_target": "rust-vector-and-scalar-ir",
            "source_sha256": method["source_sha256"],
            "location": method["location"],
            "calls": method["calls"],
        }
        for method in strategy["methods"]
        if method["name"] in set(strategy.get("vector_methods", []))
    ]
    obligation_registry = _build_semantic_obligation_registry(
        source,
        class_name=strategy["name"],
        analysis=analysis,
        strategy=strategy,
        runtime_inventory={
            "vector_methods": vector_methods,
            "callbacks": callbacks,
            "routes": routes,
            "compilation_errors": compilation_errors,
        },
        source_root=source_root,
        upstream_repository=upstream_repository,
        upstream_commit=upstream_commit,
        upstream_ref=upstream_ref,
        upstream_source_path=upstream_source_path,
        upstream_observation=_upstream_observation,
    )
    obligation_summary = obligation_registry["summary"]

    report: dict[str, Any] = {
        "schema_version": SEMANTIC_INVENTORY_VERSION,
        "source": analysis["source"],
        "selected_class": strategy["name"],
        "trading_mode": effective_mode,
        "config": config_identity,
        "static_safe": analysis["static_safe"],
        "ownership_contract": [dict(item) for item in _OWNERSHIP_CONTRACT],
        "vector_methods": vector_methods,
        "callbacks": callbacks,
        "routes": routes,
        "kernel_domains": [dict(item) for item in _KERNEL_DOMAINS],
        "registered_source_bound_adapters": [dict(item) for item in _SOURCE_BOUND_ADAPTERS],
        "active_source_boundaries": source_boundaries,
        "fixture_coverage": fixture_coverage,
        "compilation_errors": compilation_errors,
        "obligation_registry": obligation_registry,
        "blockers": obligation_registry["blockers"],
        "summary": {
            "vector_method_count": len(vector_methods),
            "callback_count": len(callbacks),
            "active_callback_count": sum(item["active_for_run"] for item in callbacks),
            "rust_callback_count": sum(
                item["active_for_run"] and item["executable_in_rust"] for item in callbacks
            ),
            "source_bound_callback_count": sum(
                item["active_for_run"]
                and item["native_boundary"] == "source-bound-rust-adapter"
                for item in callbacks
            ),
            "route_count": len(routes),
            "exact_source_fixture_count": len(exact_fixtures),
            "obligation_count": obligation_summary["total_obligations"],
            "unknown_obligation_count": obligation_summary["unknown_obligations"],
            "native_promotion": bool(
                lowering_complete and obligation_summary["native_promotion"]
            ),
            "inventory_complete": bool(
                lowering_complete
                and obligation_summary["unknown_obligations"] == 0
                and obligation_summary["source_closure_complete"]
            ),
        },
    }
    report["fingerprint"] = _fingerprint(report)
    validate_schema(report, SEMANTIC_INVENTORY_SCHEMA)
    if output_path is not None:
        write_json(output_path, report)
    return report


def build_semantic_obligation_registry(
    source: str | Path,
    *,
    class_name: str | None = None,
    trading_mode: str | None = None,
    config_path: str | Path | None = None,
    source_root: str | Path | None = None,
    upstream_repository: str | None = None,
    upstream_commit: str | None = None,
    upstream_ref: str | None = None,
    upstream_source_path: str | None = None,
    upstream_fetch_timeout_seconds: int = 180,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Generate the standalone typed registry through the reviewed lowering path."""
    inventory = build_semantic_inventory(
        source,
        class_name=class_name,
        trading_mode=trading_mode,
        config_path=config_path,
        fixtures_root=Path(source).resolve(),
        source_root=source_root,
        upstream_repository=upstream_repository,
        upstream_commit=upstream_commit,
        upstream_ref=upstream_ref,
        upstream_source_path=upstream_source_path,
        upstream_fetch_timeout_seconds=upstream_fetch_timeout_seconds,
    )
    registry = inventory["obligation_registry"]
    if output_path is not None:
        write_semantic_obligation_registry(output_path, registry)
    return registry


def _failed_upstream_observation(
    ref: str,
    source_path: str,
    *,
    method: str,
    status: str,
    blocker_code: str,
) -> dict[str, str]:
    return {
        "ref": ref,
        "source_path": source_path or "@unconfigured",
        "observation_method": method,
        "observation_status": status,
        "blocker_code": blocker_code,
    }


def _is_exact_upstream_ref(ref: str) -> bool:
    if not ref.startswith("refs/") or ref.endswith(("/", ".")):
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in ref):
        return False
    if any(character in ref for character in " ~^:?*[\\"):
        return False
    if "//" in ref or ".." in ref or "@{" in ref:
        return False
    return all(
        part
        and not part.startswith(".")
        and not part.endswith((".", ".lock"))
        for part in ref.split("/")
    )


class _UpstreamGitFailure(Exception):
    def __init__(self, method: str, status: str, blocker_code: str) -> None:
        super().__init__(blocker_code)
        self.method = method
        self.status = status
        self.blocker_code = blocker_code


def _fetch_upstream_ref_once(
    temporary_root: Path,
    *,
    repository: str,
    ref: str,
    source_path: str,
    timeout_seconds: int = 180,
) -> tuple[Path | None, dict[str, str]]:
    logical_path = PurePosixPath(source_path)
    if (
        not logical_path.parts
        or logical_path.is_absolute()
        or logical_path.as_posix() != source_path
        or "\\" in source_path
        or any(ord(character) < 32 or ord(character) == 127 for character in source_path)
        or any(part in {"", ".", ".."} for part in logical_path.parts)
    ):
        return None, _failed_upstream_observation(
            ref,
            source_path,
            method="invalid-upstream-source-v1",
            status="invalid-source-path",
            blocker_code="INVALID_UPSTREAM_SOURCE_PATH",
        )
    checkout = temporary_root / "checkout"
    try:
        checkout.mkdir(parents=True)
    except OSError:
        return None, _failed_upstream_observation(
            ref,
            logical_path.as_posix(),
            method="upstream-fetch-failed-v1",
            status="fetch-failed",
            blocker_code="UPSTREAM_FETCH_FAILED",
        )

    def git(
        *arguments: str,
        failure: tuple[str, str, str] = (
            "upstream-fetch-failed-v1",
            "fetch-failed",
            "UPSTREAM_FETCH_FAILED",
        ),
    ) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(checkout), *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise _UpstreamGitFailure(
                "upstream-fetch-timeout-v1",
                "fetch-timeout",
                "UPSTREAM_FETCH_TIMEOUT",
            ) from exc
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
            raise _UpstreamGitFailure(*failure) from exc
        if not isinstance(completed.stdout, str):
            raise _UpstreamGitFailure(*failure)
        return completed.stdout.strip()

    internal_ref = "refs/nfi-semantic-observation/requested"
    try:
        git("init", "--quiet")
        git(
            "fetch",
            "--quiet",
            "--depth=1",
            "--no-tags",
            "--",
            repository,
            f"+{ref}:{internal_ref}",
        )
        observed_object = git(
            "show-ref",
            "--verify",
            "--hash",
            internal_ref,
            failure=(
                "unresolved-upstream-ref-v1",
                "requested-object-missing",
                "UNOBSERVED_UPSTREAM_REF",
            ),
        )
        if re.fullmatch(r"[0-9a-f]{40}", observed_object) is None:
            raise _UpstreamGitFailure(
                "unresolved-upstream-ref-v1",
                "requested-object-missing",
                "UNOBSERVED_UPSTREAM_REF",
            )
        observed_commit = git(
            "rev-parse",
            "--verify",
            f"{internal_ref}^{{commit}}",
            failure=(
                "upstream-ref-not-commit-v1",
                "not-a-commit",
                "UPSTREAM_REF_NOT_COMMIT",
            ),
        )
        object_type = git(
            "cat-file",
            "-t",
            observed_commit,
            failure=(
                "upstream-ref-not-commit-v1",
                "not-a-commit",
                "UPSTREAM_REF_NOT_COMMIT",
            ),
        )
        if re.fullmatch(r"[0-9a-f]{40}", observed_commit) is None or object_type != "commit":
            raise _UpstreamGitFailure(
                "upstream-ref-not-commit-v1",
                "not-a-commit",
                "UPSTREAM_REF_NOT_COMMIT",
            )
        git("checkout", "--quiet", "--detach", observed_commit)
        observed_timestamp = git("show", "-s", "--format=%cI", observed_commit)
    except _UpstreamGitFailure as failure:
        return None, _failed_upstream_observation(
            ref,
            logical_path.as_posix(),
            method=failure.method,
            status=failure.status,
            blocker_code=failure.blocker_code,
        )

    resolved_source = checkout.joinpath(*logical_path.parts)
    source_parts = [
        checkout.joinpath(*logical_path.parts[:index])
        for index in range(1, len(logical_path.parts) + 1)
    ]
    if (
        not resolved_source.is_file()
        or any(part.is_symlink() for part in source_parts)
    ):
        return None, _failed_upstream_observation(
            ref,
            logical_path.as_posix(),
            method="upstream-source-missing-v1",
            status="source-missing",
            blocker_code="UPSTREAM_SOURCE_MISSING",
        )
    return checkout, {
        "ref": ref,
        "observed_commit": observed_commit,
        "observed_commit_timestamp": observed_timestamp,
        "source_path": logical_path.as_posix(),
        "observation_method": "git-fetch-depth-1-v1",
    }


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
        raise StrategyAnalysisError("semantic inventory requires exactly one selected strategy")


def _callback_inventory(
    strategy: dict[str, Any],
    hot_ir: dict[str, Any] | None,
    exact_fixtures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    hot_callbacks = {
        item["name"]: item
        for item in (hot_ir or {}).get("callbacks", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    methods = {
        item["name"]: item
        for item in strategy["methods"]
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    records = []
    selected = strategy.get("strategy_callbacks", strategy.get("hot_callbacks", []))
    for name in selected:
        method = methods[name]
        hot = hot_callbacks.get(name)
        active = bool(hot.get("active_for_run")) if hot else name != "leverage"
        backend = str(hot.get("backend")) if hot else "lowering-review-required"
        executable = bool(hot.get("executable_in_rust")) if hot else False
        records.append(
            {
                "name": name,
                "kind": str(hot.get("kind", "strategy-callback")) if hot else "strategy-callback",
                "semantic_owner": "nfi-strategy",
                "dispatch_owner": "freqtrade",
                "active_for_run": active,
                "backend": backend,
                "executable_in_rust": executable,
                "native_boundary": _native_boundary(backend, executable),
                "source_sha256": method["source_sha256"],
                "location": method["location"],
                "calls": method["calls"],
                "exact_fixture_ids": _fixtures_covering_callback(exact_fixtures, name),
            }
        )
    return records


def _native_boundary(backend: str, executable: bool) -> str:
    if backend.startswith("rust-nfi-x7-"):
        return "source-bound-rust-adapter"
    if executable:
        return "generic-rust-ir"
    if backend == "uncompiled-python-source":
        return "uncompiled-python-source"
    return "lowering-review-required"


def _route_inventory(
    hot_ir: dict[str, Any] | None,
    exact_fixtures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    manager = (hot_ir or {}).get("nfi_trade_manager")
    if not isinstance(manager, dict):
        return []
    operation = manager.get("operation")
    if not isinstance(operation, dict):
        return []
    routes: list[dict[str, Any]] = []
    for side, field, order_field in (
        ("long", "supported_routes", "route_order"),
        ("short", "supported_short_routes", "short_route_order"),
    ):
        route_map = operation.get(field)
        route_order = operation.get(order_field)
        if not isinstance(route_map, dict) or not isinstance(route_order, list):
            continue
        for key in route_order:
            route = route_map.get(key)
            if not isinstance(key, str) or not isinstance(route, dict):
                continue
            tags = _ordered_strings(route.get("entry_tags"))
            routes.append(
                {
                    "side": side,
                    "key": key,
                    "profile": (
                        route.get("profile")
                        if isinstance(route.get("profile"), str)
                        else None
                    ),
                    "mode_name": (
                        route.get("mode_name") if isinstance(route.get("mode_name"), str) else None
                    ),
                    "entry_tags": tags,
                    "backend": str(manager.get("backend", "unknown")),
                    "exact_fixture_ids": _fixtures_covering_tags(exact_fixtures, tags),
                }
            )
    return routes


def _source_boundary_inventory(
    callbacks: list[dict[str, Any]],
    hot_ir: dict[str, Any] | None,
    exact_fixtures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    manager = (hot_ir or {}).get("nfi_trade_manager")
    proof = manager.get("proof") if isinstance(manager, dict) else None
    stateful_methods = proof.get("stateful_methods") if isinstance(proof, dict) else None
    method_names = sorted(stateful_methods) if isinstance(stateful_methods, dict) else []
    result = []
    for callback in callbacks:
        if (
            not callback["active_for_run"]
            or callback["native_boundary"] != "source-bound-rust-adapter"
        ):
            continue
        result.append(
            {
                "callback": callback["name"],
                "backend": callback["backend"],
                "stateful_methods": method_names if callback["name"] == "custom_exit" else [],
                "exact_source_fixture_ids": [item["fixture_id"] for item in exact_fixtures],
                "branch_coverage_fixture_ids": callback["exact_fixture_ids"],
                "migration_target": (
                    "generic-state-machine-ir"
                    if callback["name"] in {"custom_exit", "adjust_trade_position"}
                    else "generic-callback-ir"
                ),
            }
        )
    return result


def _fixture_coverage(
    fixtures_root: str | Path | None,
    *,
    source_sha256: str,
    trading_mode: str,
) -> dict[str, Any]:
    root = Path(fixtures_root or "benchmarks/fixtures/captured").resolve()
    exact: list[dict[str, Any]] = []
    related: list[dict[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    if root.is_dir():
        for manifest_path in sorted(root.rglob("manifest.json")):
            try:
                manifest = read_json(manifest_path)
                record = _fixture_record(root, manifest_path, manifest, source_sha256, trading_mode)
            except (OSError, TypeError, ValueError) as exc:
                diagnostics.append(
                    {
                        "manifest": manifest_path.relative_to(root).as_posix(),
                        "message": str(exc),
                    }
                )
                continue
            if record is None:
                continue
            if record["identity_match"] == "effective-source-exact":
                exact.append(record)
            else:
                related.append(record)
    return {
        "root": str(root),
        "root_exists": root.is_dir(),
        "exact_source_fixtures": exact,
        "related_base_source_fixtures": related,
        "diagnostics": diagnostics,
    }


def _fixture_record(
    root: Path,
    manifest_path: Path,
    manifest: Any,
    source_sha256: str,
    trading_mode: str,
) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        raise TypeError("fixture manifest must be an object")
    freqtrade = manifest.get("freqtrade")
    if not isinstance(freqtrade, dict) or freqtrade.get("trading_mode") != trading_mode:
        return None
    provenance = manifest.get("strategy_provenance")
    effective_sha = None
    base_sha = None
    if isinstance(provenance, dict):
        effective_sha = provenance.get("effective_source_sha256")
        base_sha = provenance.get("base_source_sha256")
    if effective_sha is None:
        inputs = manifest.get("inputs")
        if isinstance(inputs, list):
            effective_sha = next(
                (
                    item.get("sha256")
                    for item in inputs
                    if isinstance(item, dict) and item.get("role") == "strategy"
                ),
                None,
            )
    if effective_sha == source_sha256:
        identity_match = "effective-source-exact"
    elif base_sha == source_sha256:
        identity_match = "base-source-with-transformations"
    else:
        return None
    coverage = manifest.get("required_coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    return {
        "fixture_id": str(manifest.get("fixture_id", manifest_path.parent.name)),
        "manifest": manifest_path.relative_to(root).as_posix(),
        "identity_match": identity_match,
        "evidence_status": str(manifest.get("evidence_status", "unknown")),
        "callbacks": _string_list(coverage.get("callbacks")),
        "entry_tags": _string_list(coverage.get("entry_tags")),
        "protection_methods": _string_list(coverage.get("protection_methods")),
        "sides": _string_list(coverage.get("sides")),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item for item in value if isinstance(item, str) and item})


def _ordered_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item))


def _fixtures_covering_callback(fixtures: list[dict[str, Any]], callback: str) -> list[str]:
    return sorted(item["fixture_id"] for item in fixtures if callback in item["callbacks"])


def _fixtures_covering_tags(fixtures: list[dict[str, Any]], tags: list[str]) -> list[str]:
    tag_set = set(tags)
    return sorted(item["fixture_id"] for item in fixtures if tag_set & set(item["entry_tags"]))


def _fingerprint(report: dict[str, Any]) -> str:
    fixture_coverage = report["fixture_coverage"]
    identity = {
        "schema_version": report["schema_version"],
        "source": {
            "bytes": report["source"]["bytes"],
            "sha256": report["source"]["sha256"],
        },
        "selected_class": report["selected_class"],
        "trading_mode": report["trading_mode"],
        "config_sha256": report["config"]["sha256"] if report["config"] else None,
        "ownership_contract": report["ownership_contract"],
        "vector_methods": report["vector_methods"],
        "callbacks": report["callbacks"],
        "routes": report["routes"],
        "kernel_domains": report["kernel_domains"],
        "registered_source_bound_adapters": report["registered_source_bound_adapters"],
        "active_source_boundaries": report["active_source_boundaries"],
        "fixture_coverage": {
            "root_exists": fixture_coverage["root_exists"],
            "exact_source_fixtures": fixture_coverage["exact_source_fixtures"],
            "related_base_source_fixtures": fixture_coverage["related_base_source_fixtures"],
            "diagnostics": fixture_coverage["diagnostics"],
        },
        "compilation_errors": report["compilation_errors"],
        "obligation_registry": report["obligation_registry"],
        "blockers": report["blockers"],
        "summary": report["summary"],
    }
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
