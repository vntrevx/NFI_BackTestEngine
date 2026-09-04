"""Truthful, capability-derived strategy discovery for the product UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import StrategyAnalysisError
from .fixture import sha256_file
from .legacy_reference import load_legacy_runtime_registry
from .strategy_compatibility import check_strategy_compatibility

CATALOG_SCHEMA_VERSION = "1.0.0"


def discover_strategy_catalog(workspace: str | Path) -> dict[str, Any]:
    """Classify local strategy sources without executing or importing them."""
    root = Path(workspace).resolve()
    sources = _candidate_sources(root)
    candidates = [_classify(root, source) for source in sources]
    candidates.sort(key=_candidate_sort_key)
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "workspace": str(root),
        "candidates": candidates,
        "summary": {
            "total": len(candidates),
            "supported": sum(item["status"] == "supported" for item in candidates),
            "unsupported": sum(item["status"] == "unsupported" for item in candidates),
            "invalid": sum(item["status"] == "invalid" for item in candidates),
        },
    }


def supported_strategy_paths(workspace: str | Path) -> list[Path]:
    catalog = discover_strategy_catalog(workspace)
    return [
        Path(item["path"])
        for item in catalog["candidates"]
        if item["status"] == "supported"
    ]


def _candidate_sources(workspace: Path) -> list[Path]:
    logical: list[Path] = [
        path
        for path in workspace.glob("NostalgiaForInfinity*.py")
        if path.is_file()
    ]
    for root in (workspace / "user_data" / "strategies", workspace / "strategies"):
        if root.is_dir():
            logical.extend(path for path in root.glob("*.py") if path.name != "__init__.py")
    # A symlink and its target describe one source. Preserve the first user-facing path.
    unique: dict[Path, Path] = {}
    for path in logical:
        unique.setdefault(path.resolve(), path)
    return list(unique.values())


def _classify(workspace: Path, source: Path) -> dict[str, Any]:
    resolved = source.resolve()
    display_path = _display_path(workspace, source)
    legacy = "legacy" in {part.lower() for part in resolved.parts}
    if legacy:
        return _legacy_source(
            resolved,
            display_path=display_path,
            family=source.stem,
        )
    try:
        report = check_strategy_compatibility(resolved)
    except (OSError, StrategyAnalysisError, ValueError) as exc:
        return {
            "path": str(resolved),
            "display_path": display_path,
            "name": source.stem,
            "classes": [],
            "status": "invalid",
            "reason_code": "STRATEGY_ANALYSIS_FAILED",
            "reason": str(exc),
            "blockers": [],
            "legacy": legacy,
            "generation": None,
            "fallback_status": None,
            "reference_runtime": None,
        }

    selected_class = report.get("selected_class")
    classes = [selected_class] if isinstance(selected_class, str) else []
    if isinstance(selected_class, str) and _legacy_generation(selected_class) is not None:
        return _legacy_source(resolved, display_path=display_path, family=selected_class)
    blockers = [
        {"code": str(item["code"]), "message": str(item["message"])}
        for item in report["blockers"]
    ]
    if not classes:
        status = "invalid"
        reason_code = blockers[0]["code"] if blockers else "STRATEGY_CLASS_NOT_FOUND"
        reason = blockers[0]["message"] if blockers else "no IStrategy class was found"
    elif not report["native_compatible"]:
        status = "unsupported"
        reason_code = blockers[0]["code"] if blockers else "UNSUPPORTED_SEMANTICS"
        reason = blockers[0]["message"] if blockers else "exact Native lowering is unavailable"
    else:
        status = "supported"
        reason_code = None
        reason = None
    return {
        "path": str(resolved),
        "display_path": display_path,
        "name": source.stem,
        "classes": classes,
        "status": status,
        "reason_code": reason_code,
        "reason": reason,
        "blockers": blockers,
        "legacy": legacy,
        "generation": None,
        "fallback_status": None,
        "reference_runtime": None,
    }


def _unsupported_source(
    path: Path,
    *,
    display_path: str,
    name: str,
    code: str,
    reason: str,
    legacy: bool,
    classes: list[str] | None = None,
    generation: str | None = None,
    fallback_status: str | None = None,
    reference_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "path": str(path),
        "display_path": display_path,
        "name": name,
        "classes": classes or [],
        "status": "unsupported",
        "reason_code": code,
        "reason": reason,
        "blockers": [{"code": code, "message": reason}],
        "legacy": legacy,
        "generation": generation,
        "fallback_status": fallback_status,
        "reference_runtime": reference_runtime,
    }


def _legacy_source(path: Path, *, display_path: str, family: str) -> dict[str, Any]:
    generation = _legacy_generation(family)
    qualified = None
    if generation is not None:
        registry = load_legacy_runtime_registry()
        qualified = next(
            (
                record
                for record in registry["strategies"]
                if record["family"] == family and record["source_sha256"] == sha256_file(path)
            ),
            None,
        )
    fallback_status = "qualified" if qualified is not None else "unavailable"
    reason = (
        "legacy strategy is available only through the qualified Official fallback"
        if qualified is not None
        else "legacy strategy source has no qualified exact-source Official runtime"
    )
    return _unsupported_source(
        path,
        display_path=display_path,
        name=family,
        code="LEGACY_SOURCE",
        reason=reason,
        legacy=True,
        classes=[family] if generation is not None else [],
        generation=generation,
        fallback_status=fallback_status,
        reference_runtime=dict(qualified["runtime"]) if qualified is not None else None,
    )


def _legacy_generation(family: str) -> str | None:
    registry = load_legacy_runtime_registry()
    return next(
        (record["generation"] for record in registry["strategies"] if record["family"] == family),
        None,
    )


def _candidate_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    rank = {"supported": 0, "unsupported": 1, "invalid": 2}[str(item["status"])]
    return rank, str(item["name"]).lower(), str(item["path"]).lower()

def _display_path(workspace: Path, path: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)
