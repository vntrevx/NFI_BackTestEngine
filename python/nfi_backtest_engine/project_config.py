"""Portable saved-project schema and path boundary validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .config_loader import freeze_pairlist, load_effective_config
from .errors import SpecValidationError
from .timerange import parse_timerange_milliseconds

PROJECT_SETUP_VERSION = "1.0.0"
DEFAULT_PROJECT_PATH = Path(".nfi/project.json")


@dataclass(frozen=True)
class ProjectSettings:
    """Resolved paths and choices loaded from one portable project file."""

    project_path: Path
    workspace: Path
    strategy_path: Path
    class_name: str
    config_path: Path
    data_directory: Path
    timerange: str
    output_directory: Path
    pairs: tuple[str, ...] | None
    profile_path: Path
    cache_directory: Path
    registry_path: Path


def save_project(
    *,
    project_path: str | Path,
    workspace: Path,
    strategy_path: Path,
    class_name: str,
    config_path: Path,
    data_directory: Path,
    timerange: str,
    output_directory: Path,
    pairs: list[str] | None,
    now: datetime | None = None,
) -> ProjectSettings:
    """Write paths relative to the workspace and return the validated result."""
    destination = Path(project_path).resolve()
    _validate_timerange(timerange)
    _validate_output_boundary(
        output_directory,
        workspace=workspace,
        strategy=strategy_path,
        config=config_path,
        data=data_directory,
        project=destination,
    )
    created_at = now or datetime.now(UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    document = {
        "schema_version": PROJECT_SETUP_VERSION,
        "created_at": created_at.astimezone(UTC).isoformat(),
        "workspace": _stored_path(destination.parent, workspace),
        "strategy": {
            "path": _stored_path(workspace, strategy_path),
            "class_name": class_name,
        },
        "config_path": _stored_path(workspace, config_path),
        "data_directory": _stored_path(workspace, data_directory),
        "timerange": timerange,
        "pairs": pairs,
        "output_directory": _stored_path(workspace, output_directory),
        "runtime": {
            "profile_path": ".nfi/execution-profile.json",
            "cache_directory": ".nfi/cache",
            "registry_path": ".nfi/runs.sqlite",
        },
    }
    write_json(destination, document)
    return load_project(destination)


def load_project(source: str | Path = DEFAULT_PROJECT_PATH) -> ProjectSettings:
    """Load and strictly validate a saved project without importing strategy code."""
    path = Path(source).resolve()
    if not path.is_file():
        raise SpecValidationError(
            f"project does not exist: {path}; run `nfi-bte run path/to/strategy.py` "
            "for first-time setup"
        )
    document = read_json(path)
    if not isinstance(document, dict):
        raise SpecValidationError("project document must be an object")
    required = {
        "schema_version",
        "created_at",
        "workspace",
        "strategy",
        "config_path",
        "data_directory",
        "timerange",
        "pairs",
        "output_directory",
        "runtime",
    }
    if set(document) != required:
        raise SpecValidationError("project fields differ from the v1 contract")
    if document["schema_version"] != PROJECT_SETUP_VERSION:
        raise SpecValidationError(f"unsupported project version: {document['schema_version']!r}")
    _validate_created_at(document["created_at"])

    workspace = _document_path(path.parent, document["workspace"], field="workspace")
    strategy = document["strategy"]
    if not isinstance(strategy, dict) or set(strategy) != {"path", "class_name"}:
        raise SpecValidationError("project strategy fields differ from the v1 contract")
    class_name = strategy["class_name"]
    if not isinstance(class_name, str) or not class_name.isidentifier():
        raise SpecValidationError("project strategy class_name is invalid")
    strategy_path = _document_path(workspace, strategy["path"], field="strategy.path")
    config_path = _document_path(workspace, document["config_path"], field="config_path")
    data_directory = _document_path(
        workspace,
        document["data_directory"],
        field="data_directory",
    )
    output_directory = _document_path(
        workspace,
        document["output_directory"],
        field="output_directory",
    )
    if not strategy_path.is_file():
        raise SpecValidationError(f"project strategy does not exist: {strategy_path}")
    if not config_path.is_file():
        raise SpecValidationError(f"project config does not exist: {config_path}")
    if data_directory.exists() and not data_directory.is_dir():
        raise SpecValidationError(f"project data_directory is not a directory: {data_directory}")

    timerange = document["timerange"]
    if not isinstance(timerange, str):
        raise SpecValidationError("project timerange must be a string")
    _validate_timerange(timerange)
    pairs = _document_pairs(document["pairs"])

    runtime = document["runtime"]
    runtime_fields = {"profile_path", "cache_directory", "registry_path"}
    if not isinstance(runtime, dict) or set(runtime) != runtime_fields:
        raise SpecValidationError("project runtime fields differ from the v1 contract")
    profile_path = _document_path(
        workspace,
        runtime["profile_path"],
        field="runtime.profile_path",
    )
    cache_directory = _document_path(
        workspace,
        runtime["cache_directory"],
        field="runtime.cache_directory",
    )
    registry_path = _document_path(
        workspace,
        runtime["registry_path"],
        field="runtime.registry_path",
    )

    # A config edit is allowed, but it must still produce a deterministic pairlist.
    loaded = load_effective_config(config_path)
    freeze_pairlist(
        loaded["config"],
        resolved_pairs=list(pairs) if pairs is not None else None,
    )
    _validate_output_boundary(
        output_directory,
        workspace=workspace,
        strategy=strategy_path,
        config=config_path,
        data=data_directory,
        project=path,
    )
    return ProjectSettings(
        project_path=path,
        workspace=workspace,
        strategy_path=strategy_path,
        class_name=class_name,
        config_path=config_path,
        data_directory=data_directory,
        timerange=timerange,
        output_directory=output_directory,
        pairs=pairs,
        profile_path=profile_path,
        cache_directory=cache_directory,
        registry_path=registry_path,
    )


def project_run_arguments(settings: ProjectSettings) -> dict[str, Any]:
    """Translate saved settings to the existing research-runner boundary."""
    return {
        "strategy_path": settings.strategy_path,
        "class_name": settings.class_name,
        "config_path": settings.config_path,
        "data_directory": settings.data_directory,
        "timerange": settings.timerange,
        "output_directory": settings.output_directory,
        "pairs": list(settings.pairs) if settings.pairs is not None else None,
        "cache_directory": settings.cache_directory,
        "profile_path": settings.profile_path,
        "registry_path": settings.registry_path,
    }


def project_summary(settings: ProjectSettings) -> str:
    pair_source = (
        f"{len(settings.pairs)} explicit" if settings.pairs is not None else "Freqtrade config"
    )
    return (
        f"project ready: class={settings.class_name}, timerange={settings.timerange}, "
        f"pairs={pair_source}, output={settings.output_directory} -> "
        f"{settings.project_path}"
    )


def resolve_workspace_path(workspace: Path, value: str | Path) -> Path:
    """Resolve a CLI path from the selected project workspace."""
    candidate = Path(value)
    return (candidate if candidate.is_absolute() else workspace / candidate).resolve()


def _validate_created_at(value: Any) -> None:
    if not isinstance(value, str):
        raise SpecValidationError("project created_at must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SpecValidationError("project created_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise SpecValidationError("project created_at must include a timezone")


def _document_pairs(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SpecValidationError("project pairs must be null or a list of strings")
    return tuple(value)


def _validate_timerange(value: str) -> None:
    try:
        start_ms, stop_ms = parse_timerange_milliseconds(value)
    except ValueError as exc:
        raise SpecValidationError(f"invalid project timerange {value!r}: {exc}") from exc
    if start_ms == stop_ms:
        raise SpecValidationError("project timerange must span a non-zero interval")


def _validate_output_boundary(
    output: Path,
    *,
    workspace: Path,
    strategy: Path,
    config: Path,
    data: Path,
    project: Path,
) -> None:
    protected = {
        workspace.resolve(): "workspace",
        strategy.resolve(): "strategy",
        config.resolve(): "config",
        data.resolve(): "data directory",
        project.resolve(): "project file",
    }
    resolved = output.resolve()
    for path, label in protected.items():
        if resolved == path or path.is_relative_to(resolved):
            raise SpecValidationError(f"output directory would own the {label}: {resolved}")


def _document_path(base: Path, value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise SpecValidationError(f"project {field} must be a non-empty path string")
    candidate = Path(value)
    return (candidate if candidate.is_absolute() else base / candidate).resolve()


def _stored_path(base: Path, target: Path) -> str:
    try:
        rendered = os.path.relpath(target.resolve(), base.resolve())
    except ValueError:
        rendered = str(target.resolve())
    return rendered.replace("\\", "/")
