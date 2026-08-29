"""Provenance-safe execution-signal overlays for callback branch probes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.feather as feather

from .errors import StrategyAnalysisError

_MARKET_COLUMNS: Final = ("date", "open", "high", "low", "close", "volume")
_FUTURES_COLUMNS: Final = (
    "nfi_exec_funding_rate",
    "nfi_exec_funding_mark_price",
)
_EXECUTION_COLUMNS: Final = (
    "enter_tag",
    "enter_long",
    "enter_short",
    "exit_tag",
    "exit_long",
    "exit_short",
    "nfi_exec_enter_long",
    "nfi_exec_enter_short",
    "nfi_exec_exit_long",
    "nfi_exec_exit_short",
    "nfi_exec_enter_tag",
    "nfi_exec_exit_tag",
)


def overlay_execution_signals(
    base_path: str | Path,
    scenario_path: str | Path,
    destination: str | Path,
) -> int:
    """Publish current-source features with an exact-market scenario signal surface."""
    target = Path(destination).resolve()
    if target.exists():
        raise StrategyAnalysisError(f"probe vector destination already exists: {target}")
    base = _read_vector(base_path, label="base")
    scenario = _read_vector(scenario_path, label="scenario")
    required = set(_MARKET_COLUMNS) | set(_EXECUTION_COLUMNS)
    for label, frame in (("base", base), ("scenario", scenario)):
        missing = sorted(required - set(frame.column_names))
        if missing:
            raise StrategyAnalysisError(
                f"probe vector {label} is missing columns: {', '.join(missing)}"
            )
    comparable: list[str] = list(_MARKET_COLUMNS)
    futures_present = [
        column
        for column in _FUTURES_COLUMNS
        if column in base.column_names or column in scenario.column_names
    ]
    if futures_present:
        if any(
            column not in base.column_names or column not in scenario.column_names
            for column in _FUTURES_COLUMNS
        ):
            raise StrategyAnalysisError("probe vector funding columns differ")
        comparable.extend(_FUTURES_COLUMNS)
    base_dates = base["date"].to_pylist()
    scenario_dates = scenario["date"].to_pylist()
    try:
        scenario_offset = base_dates.index(scenario_dates[0])
    except ValueError as exc:
        raise StrategyAnalysisError(
            "probe vector scenario does not start inside the base vector"
        ) from exc
    base_window = base.slice(scenario_offset, scenario.num_rows)
    if base_window.num_rows != scenario.num_rows or not base_window.select(comparable).equals(
        scenario.select(comparable)
    ):
        raise StrategyAnalysisError("probe vector market-state columns differ")
    overlaid = base
    for column in _EXECUTION_COLUMNS:
        index = overlaid.schema.get_field_index(column)
        prefix = base[column].slice(0, scenario_offset)
        suffix_offset = scenario_offset + scenario.num_rows
        suffix = base[column].slice(suffix_offset)
        values = pa.chunked_array(
            [*prefix.chunks, *scenario[column].chunks, *suffix.chunks],
            type=base[column].type,
        )
        overlaid = overlaid.set_column(index, column, values)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    try:
        feather.write_feather(overlaid, temporary, compression="zstd")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return scenario_offset


def _read_vector(path: str | Path, *, label: str) -> pa.Table:
    source = Path(path).resolve()
    try:
        return feather.read_table(source)
    except (OSError, pa.ArrowInvalid) as exc:
        raise StrategyAnalysisError(f"probe vector {label} cannot be read: {source}") from exc
