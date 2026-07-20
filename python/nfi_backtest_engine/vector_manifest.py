"""Small validation helpers shared by compact vector-manifest adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow.ipc as pa_ipc

from .errors import StrategyAnalysisError
from .fixture import sha256_file

VECTOR_MANIFEST_VERSION = "1.2.0"
EMPTY_TAG_TRANSPORT_SENTINEL = "__nfi_bte_empty_tag_column__"


def feather_column_names(path: Path, pair: str) -> set[str]:
    """Read only Arrow metadata; no dataframe columns are materialized."""
    try:
        with pa_ipc.open_file(path) as reader:
            return set(reader.schema.names)
    except (OSError, ValueError) as exc:
        raise StrategyAnalysisError(f"cannot inspect vector artifact for {pair}: {path}") from exc


def require_columns(columns: set[str], required: set[str], pair: str) -> None:
    missing = required - columns
    if missing:
        raise StrategyAnalysisError(
            f"vector artifact for {pair} is missing columns: {', '.join(sorted(missing))}"
        )


def verified_vector_sha256(
    path: Path,
    artifact: dict[str, Any],
    pair: str,
) -> str:
    """Bind the manifest to the bytes reported by the completed vector stage."""
    if not path.is_file():
        raise StrategyAnalysisError(f"vector artifact does not exist for {pair}: {path}")
    expected = artifact.get("sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise StrategyAnalysisError(f"vector report lacks a canonical SHA-256 for {pair}")
    actual = sha256_file(path)
    if actual != expected:
        raise StrategyAnalysisError(
            f"vector artifact changed after analysis for {pair}: expected {expected}, got {actual}"
        )
    return actual


def artifact_execution_start_index(
    artifact: dict[str, Any],
    pair: str,
    row_count: int,
) -> int:
    """Validate the boundary between callback context and executable rows.

    Reports produced before vector-output v1.5 did not preserve callback
    context and therefore implicitly started at row zero. The default keeps
    those sealed fixtures readable while every new report carries an explicit
    index.
    """
    value = artifact.get("execution_start_index", 0)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value >= row_count
    ):
        raise StrategyAnalysisError(
            f"vector artifact for {pair} has invalid execution_start_index "
            f"{value!r} for {row_count} rows"
        )
    return value


def contained_vector_path(path: Path, manifest_directory: Path, pair: str) -> str:
    """Return a portable path that Rust can prove stays below the run root."""
    try:
        relative = path.relative_to(manifest_directory.resolve())
    except ValueError as exc:
        raise StrategyAnalysisError(
            f"vector artifact for {pair} must be inside the research output: {path}"
        ) from exc
    return relative.as_posix()
