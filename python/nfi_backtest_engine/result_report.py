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
from .reporting.contracts import HTML_FILENAME, SUMMARY_FILENAME, TRADES_FILENAME
from .reporting.csv_export import _write_trades_csv
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
from .result_summary import build_result_summary

__all__ = [
    "HTML_FILENAME",
    "SUMMARY_FILENAME",
    "TRADES_FILENAME",
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
    run_report = read_json(run_path)
    if not isinstance(run_report, dict):
        raise BenchmarkError(f"research run report must be an object: {run_path}")
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
    write_json(root / SUMMARY_FILENAME, summary)
    _write_trades_csv(root / TRADES_FILENAME, surface)
    (root / HTML_FILENAME).write_text(
        _render_html(summary, surface),
        encoding="utf-8",
        newline="\n",
    )
    return summary
