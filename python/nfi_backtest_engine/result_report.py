"""Human and machine-friendly presentation files for research-run results."""

# The self-contained HTML template intentionally keeps related markup and CSS rules
# on single lines so the generated file stays easy to inspect without a build step.
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError
from .fixture import sha256_file
from .reporting.artifacts import (
    build_evidence_index,
    build_verification_artifact,
)
from .reporting.contracts import (
    EQUITY_FILENAME,
    EVIDENCE_INDEX_FILENAME,
    HTML_FILENAME,
    ORDERS_FILENAME,
    SUMMARY_FILENAME,
    TRADES_FILENAME,
    VERIFICATION_FILENAME,
)
from .reporting.csv_export import (
    _write_equity_csv,
    _write_orders_csv,
    _write_trades_csv,
)
from .reporting.html_render import _render_html
from .reporting.model import (
    _load_bound_surface,
    _resolve_verification,
    _with_adjacent_resource_measurement,
    load_result_summary,
)
from .reporting.terminal import (
    format_run_list,
    format_run_record,
    format_terminal_breakdowns,
    format_terminal_summary,
)
from .result_summary import build_closed_trade_equity_rows, build_result_summary
from .selected_result import load_selected_run_view
from .specs import (
    RESULT_EVIDENCE_INDEX_SCHEMA,
    RESULT_VERIFICATION_SCHEMA,
    validate_schema,
)

__all__ = [
    "HTML_FILENAME",
    "EQUITY_FILENAME",
    "EVIDENCE_INDEX_FILENAME",
    "ORDERS_FILENAME",
    "SUMMARY_FILENAME",
    "TRADES_FILENAME",
    "VERIFICATION_FILENAME",
    "format_run_list",
    "format_run_record",
    "format_terminal_breakdowns",
    "format_terminal_summary",
    "load_result_summary",
    "write_result_presentation",
]


def write_result_presentation(
    run_directory: str | Path,
    *,
    verification: Mapping[str, Any] | None = None,
    verification_path: str | Path | None = None,
) -> dict[str, Any]:
    """Generate all derived result files without modifying parity evidence.

    The function deliberately writes beside ``run.json`` and never rewrites it.
    This allows official confirmation to refresh the user-facing badge without
    invalidating a run report that may already be referenced by certification.
    """

    root = Path(run_directory).resolve()
    run_path = root / "run.json"
    if not run_path.is_file():
        raise BenchmarkError(f"research run.json does not exist: {run_path}")
    source_run_report = read_json(run_path)
    if not isinstance(source_run_report, dict):
        raise BenchmarkError(f"research run report must be an object: {run_path}")
    run_report, selected_result = load_selected_run_view(root, source_run_report)
    protected_sources = _source_evidence_snapshots(
        root,
        source_run_report,
        verification_path=verification_path,
        selected_result=selected_result,
    )
    run_report = _with_adjacent_resource_measurement(root, run_report)

    surface = _load_bound_surface(root, run_report)
    verification_document = _resolve_verification(
        root,
        run_report,
        verification=verification,
        verification_path=verification_path,
    )
    summary = build_result_summary(
        run_report,
        surface,
        verification=verification_document,
    )
    verification_artifact = build_verification_artifact(
        run_report,
        summary,
        verification_document=verification_document,
    )
    validate_schema(verification_artifact, RESULT_VERIFICATION_SCHEMA)
    summary["verification"] = verification_artifact["verification"]
    equity_rows = build_closed_trade_equity_rows(surface)

    write_json(root / SUMMARY_FILENAME, summary)
    _write_trades_csv(root / TRADES_FILENAME, surface)
    _write_orders_csv(root / ORDERS_FILENAME, surface)
    _write_equity_csv(root / EQUITY_FILENAME, equity_rows)
    write_json(root / VERIFICATION_FILENAME, verification_artifact)
    _assert_source_evidence_unchanged(protected_sources)

    evidence_index = build_evidence_index(
        root,
        run_id=run_report.get("run_id"),
        include_surface=surface is not None,
        selected_result=selected_result,
    )
    validate_schema(evidence_index, RESULT_EVIDENCE_INDEX_SCHEMA)
    write_json(root / EVIDENCE_INDEX_FILENAME, evidence_index)
    (root / HTML_FILENAME).write_text(
        _render_html(summary, surface, evidence_index),
        encoding="utf-8",
        newline="\n",
    )
    _assert_source_evidence_unchanged(protected_sources)
    return summary


def _source_evidence_snapshots(
    root: Path,
    run_report: Mapping[str, Any],
    *,
    verification_path: str | Path | None,
    selected_result: Mapping[str, Any] | None,
) -> dict[Path, tuple[int, str]]:
    derived_destinations = {
        (root / filename).resolve()
        for filename in (
            SUMMARY_FILENAME,
            TRADES_FILENAME,
            ORDERS_FILENAME,
            EQUITY_FILENAME,
            VERIFICATION_FILENAME,
            EVIDENCE_INDEX_FILENAME,
            HTML_FILENAME,
        )
    }
    candidates = [root / "run.json", root / "trade-surface.json"]
    result = run_report.get("result")
    surface_record = result.get("trade_surface") if isinstance(result, Mapping) else None
    recorded_surface = surface_record.get("path") if isinstance(surface_record, Mapping) else None
    if isinstance(recorded_surface, str):
        candidates.append(Path(recorded_surface))
    if verification_path is not None:
        proof = Path(verification_path).resolve()
        if proof in derived_destinations:
            raise BenchmarkError(
                f"confirmation source collides with a derived result artifact: {proof}"
            )
        candidates.append(proof)
    if selected_result is not None:
        candidates.append(root / "selected-result.json")
        official = selected_result.get("official")
        if isinstance(official, Mapping):
            for record in official.values():
                if isinstance(record, Mapping) and isinstance(record.get("path"), str):
                    candidates.append(root / record["path"])

    snapshots: dict[Path, tuple[int, str]] = {}
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_file() or resolved in snapshots:
            continue
        snapshots[resolved] = (resolved.stat().st_size, sha256_file(resolved))
    return snapshots


def _assert_source_evidence_unchanged(
    snapshots: Mapping[Path, tuple[int, str]],
) -> None:
    for path, expected in snapshots.items():
        if not path.is_file():
            raise BenchmarkError(f"source evidence disappeared during report generation: {path}")
        actual = (path.stat().st_size, sha256_file(path))
        if actual != expected:
            raise BenchmarkError(f"source evidence changed during report generation: {path}")
