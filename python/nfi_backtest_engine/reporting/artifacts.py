"""Machine-readable verification and evidence-index artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..evidence_bundle import artifact_record
from .contracts import (
    EQUITY_FILENAME,
    EVIDENCE_INDEX_FILENAME,
    HTML_FILENAME,
    ORDERS_FILENAME,
    SUMMARY_FILENAME,
    TRADES_FILENAME,
    VERIFICATION_FILENAME,
)
from .values import _mapping


def build_verification_artifact(
    run_report: Mapping[str, Any],
    summary: Mapping[str, Any],
    *,
    verification_document: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Project verification stages and candidate identities without new claims."""

    run = _mapping(summary, "run")
    verification = dict(_mapping(summary, "verification"))
    inputs = _mapping(run_report, "inputs")
    strategy = _mapping(inputs, "strategy")
    pipeline = _mapping(inputs, "pipeline")
    result = _mapping(run_report, "result")
    execution = _mapping(result, "execution")
    build = _mapping(execution, "build")
    surface = _mapping(result, "trade_surface")
    simulation = _mapping(result, "simulation_result")
    pipeline_evidence = _mapping(run_report, "pipeline_evidence")

    run_status = str(run_report.get("status", "unknown"))
    native_status = (
        "passed"
        if run_report.get("complete") is True and run_status == "complete"
        else "not_run"
        if run_status == "prepared"
        else "blocked"
        if run_status == "blocked_unsupported_semantics"
        else "failed"
    )
    integrity_status = "passed" if surface else "not_run"
    official_status = {
        "exact_match": "passed",
        "confirmed_exact": "passed",
        "mismatch": "failed",
        "reference_incomplete": "blocked",
    }.get(str(verification.get("status")), "not_run")
    release_certified = (
        verification_document.get("release_certified")
        if verification_document is not None
        else None
    )
    release_status = (
        "passed"
        if release_certified is True
        else "failed"
        if release_certified is False
        else "not_run"
    )

    proof_inputs = (
        _mapping(verification_document, "inputs")
        if verification_document is not None
        else {}
    )
    package_sha256 = _first_string(
        pipeline.get("package_sha256"),
        inputs.get("package_sha256"),
        build.get("package_sha256"),
    )
    certified_package_sha256 = _first_string(
        proof_inputs.get("package_sha256"),
        proof_inputs.get("wheel_sha256"),
    )
    verification.update(
        {
            "stages": [
                {
                    "id": "native_execution",
                    "status": native_status,
                    "detail": f"research run status: {run_status}",
                },
                {
                    "id": "artifact_integrity",
                    "status": integrity_status,
                    "detail": (
                        "trade surface is hash-bound to run.json"
                        if surface
                        else "no trade surface exists for this run state"
                    ),
                },
                {
                    "id": "official_verification",
                    "status": official_status,
                    "detail": f"official parity status: {verification.get('status')}",
                },
                {
                    "id": "release_certification",
                    "status": release_status,
                    "detail": (
                        "release_certified=true"
                        if release_certified is True
                        else "release certification failed"
                        if release_certified is False
                        else "no release certificate is bound"
                    ),
                },
            ],
            "identities": {
                "strategy_sha256": _first_string(strategy.get("file_sha256")),
                "certified_strategy_sha256": _first_string(
                    proof_inputs.get("strategy_sha256")
                ),
                "package_version": _first_string(pipeline.get("package_version")),
                "package_sha256": package_sha256,
                "certified_package_sha256": certified_package_sha256,
                "native_binary_sha256": _first_string(build.get("binary_sha256")),
                "native_target": _first_string(build.get("target")),
                "trade_surface_sha256": _first_string(surface.get("sha256")),
                "simulation_result_sha256": _first_string(simulation.get("sha256")),
            },
            "boundaries": {
                "equity_source": "closed_trade_profit",
                "candle_level_equity_available": False,
                "native_performance_cache_state": (
                    "cold"
                    if pipeline_evidence.get("cold") is True
                    else "warm"
                    if pipeline_evidence.get("cold") is False
                    else "unknown"
                ),
                "vector_cache_hits": _optional_integer(
                    pipeline_evidence.get("vector_cache_hits")
                ),
                "official_performance_included": False,
                "performance_note": (
                    "Native run timing and official parity are separate evidence lanes."
                ),
            },
        }
    )
    return {
        "schema_version": "1.0.0",
        "run": {
            "id": run.get("id"),
            "status": run.get("status"),
            "complete": run.get("complete"),
        },
        "verification": verification,
    }


def build_evidence_index(
    root: Path,
    *,
    run_id: Any,
    include_surface: bool,
) -> dict[str, Any]:
    """Index local source and derived files with portable paths and hashes."""

    definitions = [
        ("run", "source", True, Path("run.json")),
        ("trade_surface", "source", True, Path("trade-surface.json"))
        if include_surface
        else None,
        ("summary", "derived", False, Path(SUMMARY_FILENAME)),
        ("trades_csv", "derived", False, Path(TRADES_FILENAME)),
        ("orders_csv", "derived", False, Path(ORDERS_FILENAME)),
        ("equity_csv", "derived", False, Path(EQUITY_FILENAME)),
        ("verification", "derived", False, Path(VERIFICATION_FILENAME)),
    ]
    entries = []
    for definition in definitions:
        if definition is None:
            continue
        role, provenance, immutable, relative = definition
        path = root / relative
        if not path.is_file():
            continue
        entries.append(
            {
                "role": role,
                "provenance": provenance,
                "immutable_source": immutable,
                **artifact_record(path, relative_to=root),
            }
        )
    return {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "index_path": EVIDENCE_INDEX_FILENAME,
        "source_evidence_immutable": True,
        "entries": entries,
        "excluded_from_index": [
            {
                "path": EVIDENCE_INDEX_FILENAME,
                "reason": "self-referential hash is intentionally excluded",
            },
            {
                "path": HTML_FILENAME,
                "reason": (
                    "HTML renders this index; excluding its hash avoids a circular dependency"
                ),
            },
        ],
    }


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _optional_integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None
