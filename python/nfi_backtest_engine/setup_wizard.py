"""First-run discovery and prompts for a saved NFI project."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config_loader import freeze_pairlist, load_effective_config
from .errors import SpecValidationError
from .product_contract import default_long_timerange
from .project_config import (
    DEFAULT_PROJECT_PATH,
    ProjectSettings,
    project_summary,
    resolve_workspace_path,
    save_project,
)
from .strategy_ir import analyze_strategy

Prompt = Callable[[str], str]
Emitter = Callable[[str], None]


def initialize_project(
    *,
    project_path: str | Path = DEFAULT_PROJECT_PATH,
    workspace: str | Path | None = None,
    source: str | Path | None = None,
    class_name: str | None = None,
    config_path: str | Path | None = None,
    data_directory: str | Path | None = None,
    timerange: str | None = None,
    output_directory: str | Path | None = None,
    pairs: list[str] | None = None,
    interactive: bool = True,
    force: bool = False,
    prompt: Prompt = input,
    emit: Emitter = print,
    now: datetime | None = None,
) -> ProjectSettings:
    """Discover standard Freqtrade paths, ask only for ambiguity, and save them."""
    root = Path.cwd().resolve() if workspace is None else Path(workspace).resolve()
    destination = resolve_workspace_path(root, project_path)
    if destination.exists() and not force:
        raise SpecValidationError(
            f"project already exists: {destination}; run `nfi-bte run` or use "
            "`nfi-bte init --force` to reconfigure it"
        )

    selected_source = _select_source(
        root,
        source=source,
        interactive=interactive,
        prompt=prompt,
        emit=emit,
    )
    selected_class = _select_class(
        selected_source,
        class_name=class_name,
        interactive=interactive,
        prompt=prompt,
        emit=emit,
    )
    selected_config = _select_config(
        root,
        selected_source,
        config_path=config_path,
        interactive=interactive,
        prompt=prompt,
        emit=emit,
    )
    loaded_config = load_effective_config(selected_config)
    selected_pairs = _select_pairs(
        loaded_config["config"],
        pairs=pairs,
        interactive=interactive,
        prompt=prompt,
    )
    selected_data = _select_data_directory(
        root,
        selected_config,
        loaded_config["config"],
        data_directory=data_directory,
        interactive=interactive,
        prompt=prompt,
        emit=emit,
    )
    selected_timerange = _select_timerange(
        timerange,
        interactive=interactive,
        prompt=prompt,
        now=now,
    )
    selected_output = _select_output_directory(
        root,
        selected_class,
        selected_timerange,
        output_directory=output_directory,
    )

    settings = save_project(
        project_path=destination,
        workspace=root,
        strategy_path=selected_source,
        class_name=selected_class,
        config_path=selected_config,
        data_directory=selected_data,
        timerange=selected_timerange,
        output_directory=selected_output,
        pairs=selected_pairs,
        now=now,
    )
    emit(project_summary(settings))
    return settings


def _select_source(
    workspace: Path,
    *,
    source: str | Path | None,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
) -> Path:
    if source is not None:
        selected = resolve_workspace_path(workspace, source)
    else:
        candidates = _strategy_candidates(workspace)
        if len(candidates) == 1:
            selected = candidates[0]
            emit(f"detected strategy: {selected}")
        elif candidates:
            selected = _choose_path(
                "strategy",
                candidates,
                interactive=interactive,
                prompt=prompt,
                emit=emit,
            )
        elif interactive:
            selected = resolve_workspace_path(
                workspace,
                _prompt_value("Strategy file", prompt=prompt),
            )
        else:
            raise SpecValidationError(
                "strategy was not provided and no standard strategy file was found"
            )
    if not selected.is_file():
        raise SpecValidationError(f"strategy file does not exist: {selected}")
    return selected


def _select_class(
    source: Path,
    *,
    class_name: str | None,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
) -> str:
    initial = analyze_strategy(source)
    names = [item["name"] for item in initial["strategies"]]
    if class_name is not None:
        selected = class_name
    elif len(names) == 1:
        selected = names[0]
        emit(f"detected strategy class: {selected}")
    elif names and interactive:
        selected = _choose_value(
            "strategy class",
            names,
            prompt=prompt,
            emit=emit,
        )
    elif names:
        rendered = ", ".join(names)
        raise SpecValidationError(f"multiple strategy classes found ({rendered}); pass --class")
    else:
        raise SpecValidationError(f"no IStrategy class was found in {source}")

    analysis = analyze_strategy(source, class_name=selected)
    errors = [
        diagnostic for diagnostic in analysis["diagnostics"] if diagnostic["severity"] == "error"
    ]
    if errors:
        first = errors[0]
        location = first["location"]
        raise SpecValidationError(
            f"{location['path']}:{location['line']}:{location['column']}: "
            f"{first['code']}: {first['message']}"
        )
    return selected


def _select_config(
    workspace: Path,
    source: Path,
    *,
    config_path: str | Path | None,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
) -> Path:
    if config_path is not None:
        selected = resolve_workspace_path(workspace, config_path)
    else:
        candidates = _config_candidates(workspace, source)
        valid = [candidate for candidate in candidates if _is_valid_config(candidate)]
        if len(valid) == 1:
            selected = valid[0]
            emit(f"detected Freqtrade config: {selected}")
        elif valid:
            selected = _choose_path(
                "Freqtrade config",
                valid,
                interactive=interactive,
                prompt=prompt,
                emit=emit,
            )
        elif interactive:
            selected = resolve_workspace_path(
                workspace,
                _prompt_value("Freqtrade config", prompt=prompt),
            )
        else:
            raise SpecValidationError(
                "Freqtrade config was not provided and no valid standard config was found"
            )
    load_effective_config(selected)
    return selected


def _select_pairs(
    config: dict[str, Any],
    *,
    pairs: list[str] | None,
    interactive: bool,
    prompt: Prompt,
) -> list[str] | None:
    if pairs is not None:
        return freeze_pairlist(config, resolved_pairs=pairs)["pairs"]
    try:
        freeze_pairlist(config)
        return None
    except SpecValidationError:
        if not interactive:
            raise SpecValidationError(
                "config has no static pair whitelist; repeat --pair for each pair"
            ) from None
    raw = _prompt_value(
        "Pairs (comma separated, for example BTC/USDT,ETH/USDT)",
        prompt=prompt,
    )
    selected = [item.strip() for item in raw.split(",") if item.strip()]
    return freeze_pairlist(config, resolved_pairs=selected)["pairs"]


def _select_data_directory(
    workspace: Path,
    config_path: Path,
    config: dict[str, Any],
    *,
    data_directory: str | Path | None,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
) -> Path:
    if data_directory is not None:
        selected = resolve_workspace_path(workspace, data_directory)
    else:
        exchange = config["exchange"]["name"]
        exchange_candidates = _unique_paths(
            [
                config_path.parent / "data" / exchange,
                workspace / "user_data" / "data" / exchange,
            ]
        )
        root_candidates = _unique_paths(
            [
                config_path.parent / "data",
                workspace / "user_data" / "data",
            ]
        )
        existing_exchange = [candidate for candidate in exchange_candidates if candidate.is_dir()]
        existing_roots = [candidate for candidate in root_candidates if candidate.is_dir()]
        # The exchange directory is more precise than its naturally existing parent.
        if existing_exchange:
            selected = existing_exchange[0]
            emit(f"detected candle data: {selected}")
        elif len(existing_roots) == 1:
            selected = existing_roots[0]
            emit(f"detected candle data: {selected}")
        elif len(existing_roots) > 1:
            selected = _choose_path(
                "candle data directory",
                existing_roots,
                interactive=interactive,
                prompt=prompt,
                emit=emit,
            )
        else:
            default = exchange_candidates[0]
            if interactive:
                raw = _prompt_value(
                    "Candle data directory",
                    default=_display_path(workspace, default),
                    prompt=prompt,
                )
                selected = resolve_workspace_path(workspace, raw)
            else:
                selected = default
            emit(f"candle data will use: {selected}")
    if selected.exists() and not selected.is_dir():
        raise SpecValidationError(f"candle data path is not a directory: {selected}")
    return selected


def _select_timerange(
    value: str | None,
    *,
    interactive: bool,
    prompt: Prompt,
    now: datetime | None,
) -> str:
    if value is not None:
        return value
    default = _default_timerange(now or datetime.now(UTC))
    return _prompt_value("Timerange", default=default, prompt=prompt) if interactive else default


def _select_output_directory(
    workspace: Path,
    class_name: str,
    timerange: str,
    *,
    output_directory: str | Path | None,
) -> Path:
    if output_directory is not None:
        return resolve_workspace_path(workspace, output_directory)
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", class_name)
    slug = re.sub(r"[^a-z0-9]+", "-", separated.lower()).strip("-")
    return (workspace / "artifacts" / f"{slug}-{timerange}").resolve()


def _strategy_candidates(workspace: Path) -> list[Path]:
    candidates: list[Path] = []
    for root in (workspace / "user_data" / "strategies", workspace / "strategies"):
        if root.is_dir():
            candidates.extend(
                path.resolve() for path in root.glob("*.py") if path.name != "__init__.py"
            )
    return sorted(set(candidates), key=lambda path: str(path).lower())


def _config_candidates(workspace: Path, source: Path) -> list[Path]:
    candidates = [
        workspace / "user_data" / "config.json",
        workspace / "config.json",
    ]
    if source.parent.name == "strategies":
        candidates.insert(0, source.parent.parent / "config.json")
    return [path for path in _unique_paths(candidates) if path.is_file()]


def _is_valid_config(path: Path) -> bool:
    try:
        load_effective_config(path)
    except (OSError, SpecValidationError):
        return False
    return True


def _choose_path(
    label: str,
    candidates: list[Path],
    *,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
) -> Path:
    if not interactive:
        rendered = ", ".join(str(path) for path in candidates)
        raise SpecValidationError(f"multiple {label} candidates found: {rendered}")
    selected = _choose_value(
        label,
        [str(path) for path in candidates],
        prompt=prompt,
        emit=emit,
    )
    return Path(selected).resolve()


def _choose_value(
    label: str,
    values: list[str],
    *,
    prompt: Prompt,
    emit: Emitter,
) -> str:
    emit(f"Multiple {label} choices were found:")
    for index, value in enumerate(values, start=1):
        emit(f"  {index}. {value}")
    while True:
        raw = _prompt_value(f"Choose {label} [1-{len(values)}]", prompt=prompt)
        if raw.isdigit() and 1 <= int(raw) <= len(values):
            return values[int(raw) - 1]
        emit(f"Enter a number from 1 to {len(values)}.")


def _prompt_value(
    label: str,
    *,
    prompt: Prompt,
    default: str | None = None,
) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        value = prompt(f"{label}{suffix}: ").strip()
    except EOFError as exc:
        raise SpecValidationError(
            f"{label} is required; pass it as an option or use --yes for defaults"
        ) from exc
    if value:
        return value
    if default is not None:
        return default
    raise SpecValidationError(f"{label} cannot be empty")


def _default_timerange(now: datetime) -> str:
    return default_long_timerange(now)


def _display_path(workspace: Path, value: Path) -> str:
    try:
        return str(value.relative_to(workspace)).replace("\\", "/")
    except ValueError:
        return str(value)


def _unique_paths(values: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for value in values:
        resolved = value.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result
