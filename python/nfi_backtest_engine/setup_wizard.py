"""First-run discovery and prompts for a saved NFI project."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .config_loader import freeze_pairlist, load_effective_config
from .errors import SpecValidationError
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
    trading_mode: str | None = None,
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
        generated_path=destination.parent / "first-run-config.json",
        config_path=config_path,
        trading_mode=trading_mode,
        interactive=interactive,
        prompt=prompt,
        emit=emit,
    )
    loaded_config = load_effective_config(selected_config)
    beginner_setup = selected_config == (destination.parent / "first-run-config.json").resolve()
    selected_pairs = _select_pairs(
        loaded_config["config"],
        workspace=root,
        beginner_setup=beginner_setup,
        pairs=pairs,
        interactive=interactive,
        prompt=prompt,
        emit=emit,
    )
    selected_data = _select_data_directory(
        root,
        selected_config,
        loaded_config["config"],
        data_directory=data_directory,
        interactive=interactive,
        prompt=prompt,
        emit=emit,
        managed_default=beginner_setup,
    )
    selected_timerange = _select_timerange(
        timerange,
        interactive=interactive,
        prompt=prompt,
        emit=emit,
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
    generated_path: Path,
    config_path: str | Path | None,
    trading_mode: str | None,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
) -> Path:
    if config_path is not None:
        selected = resolve_workspace_path(workspace, config_path)
        loaded = load_effective_config(selected)
        _require_requested_trading_mode(loaded["config"], trading_mode)
        return selected

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
    else:
        if candidates:
            emit(
                "NFI's modular config was found. It will not be changed; "
                "the backtest engine will create its own safe config."
            )
        emit("First-time setup. Press Enter to accept values shown in [brackets].")
        selected_mode = _select_trading_mode(
            trading_mode,
            interactive=interactive,
            prompt=prompt,
            emit=emit,
        )
        selected_exchange = _select_exchange(
            interactive=interactive,
            prompt=prompt,
            emit=emit,
        )
        selected = generated_path.resolve()
        _write_first_run_config(
            selected,
            trading_mode=selected_mode,
            exchange=selected_exchange,
        )
        emit(f"generated safe {selected_mode} config: {selected}")
        return selected

    loaded = load_effective_config(selected)
    _require_requested_trading_mode(loaded["config"], trading_mode)
    return selected


def _select_trading_mode(
    requested: str | None,
    *,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
) -> str:
    if requested is not None:
        if requested not in {"spot", "futures"}:
            raise SpecValidationError("trading mode must be spot or futures")
        emit(f"using requested trading mode: {requested}")
        return requested
    if not interactive:
        emit("using default trading mode: spot")
        return "spot"
    while True:
        selected = _prompt_value(
            "Trading mode (spot or futures)",
            default="spot",
            prompt=prompt,
        ).lower()
        if selected in {"spot", "futures"}:
            return selected
        emit("Enter spot or futures.")


def _select_exchange(
    *,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
) -> str:
    if not interactive:
        emit("using default exchange: binance")
        return "binance"
    while True:
        selected = _prompt_value("Exchange", default="binance", prompt=prompt).lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", selected):
            return selected
        emit("Enter a lowercase CCXT exchange id such as binance.")


def _require_requested_trading_mode(
    config: dict[str, Any],
    requested: str | None,
) -> None:
    if requested is None:
        return
    configured = config.get("trading_mode", "spot")
    if configured != requested:
        raise SpecValidationError(
            f"requested trading mode {requested} differs from config mode {configured}"
        )


def _write_first_run_config(
    destination: Path,
    *,
    trading_mode: str,
    exchange: str,
) -> None:
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise SpecValidationError(
            f"generated config destination is not a regular file: {destination}"
        )
    market_type = "linear" if trading_mode == "futures" else "spot"
    config: dict[str, Any] = {
        "$schema": "https://schema.freqtrade.io/schema.json",
        "dry_run": True,
        "dry_run_wallet": 10_000,
        "trading_mode": trading_mode,
        "grinding_enable": True,
        "max_open_trades": 1,
        "stake_currency": "USDT",
        "stake_amount": "unlimited",
        "tradable_balance_ratio": 0.99,
        "timeframe": "5m",
        "dataformat_ohlcv": "feather",
        "entry_pricing": {
            "price_side": "other",
            "use_order_book": True,
            "order_book_top": 1,
        },
        "exit_pricing": {
            "price_side": "other",
            "use_order_book": True,
            "order_book_top": 1,
        },
        "exchange": {
            "name": exchange,
            "key": "",
            "secret": "",
            "pair_whitelist": [],
            "pair_blacklist": [],
            "ccxt_config": {
                "options": {
                    "fetchMarkets": {
                        "types": [market_type],
                    }
                }
            },
        },
        "pairlists": [
            {
                "method": "StaticPairList",
                "allow_inactive": True,
            }
        ],
    }
    if trading_mode == "futures":
        config["margin_mode"] = "isolated"
    write_json(destination, config)
    load_effective_config(destination)


def _select_pairs(
    config: dict[str, Any],
    *,
    workspace: Path,
    beginner_setup: bool,
    pairs: list[str] | None,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
) -> list[str] | None:
    if pairs is not None:
        return freeze_pairlist(config, resolved_pairs=pairs)["pairs"]
    try:
        freeze_pairlist(config)
        return None
    except SpecValidationError:
        pass
    if beginner_setup:
        return _select_beginner_pairs(
            config,
            workspace=workspace,
            interactive=interactive,
            prompt=prompt,
            emit=emit,
        )
    if not interactive:
        raise SpecValidationError(
            "config has no static pair whitelist; repeat --pair for each pair"
        )
    return _select_custom_pairs(config, prompt=prompt)


def _select_beginner_pairs(
    config: dict[str, Any],
    *,
    workspace: Path,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
) -> list[str]:
    trading_mode = str(config.get("trading_mode", "spot"))
    quick_pair = "ADA/USDT:USDT" if trading_mode == "futures" else "ADA/USDT"
    quick = freeze_pairlist(config, resolved_pairs=[quick_pair])["pairs"]
    preset = _nfi_backtest_pairs(workspace, trading_mode=trading_mode)
    if not interactive:
        emit(f"using quick-test pair: {quick_pair}")
        return quick

    emit("Choose the markets to test:")
    emit(f"  1. Quick test — {quick_pair} (recommended)")
    if preset is not None:
        emit(
            f"  2. NFI backtest list — {len(preset)} pairs "
            "(larger download and longer run)"
        )
        emit("  3. Custom list — enter one or more pairs")
        choices = {"1", "2", "3"}
    else:
        emit("  2. Custom list — enter one or more pairs")
        choices = {"1", "2"}
    while True:
        choice = _prompt_value("Pair choice", default="1", prompt=prompt)
        if choice == "1":
            return quick
        if preset is not None and choice == "2":
            return preset
        custom_choice = "3" if preset is not None else "2"
        if choice == custom_choice:
            return _select_custom_pairs(config, prompt=prompt)
        emit(f"Enter one of: {', '.join(sorted(choices))}.")


def _select_custom_pairs(
    config: dict[str, Any],
    *,
    prompt: Prompt,
) -> list[str]:
    trading_mode = config.get("trading_mode", "spot")
    example = (
        "BTC/USDT:USDT,ETH/USDT:USDT"
        if trading_mode == "futures"
        else "BTC/USDT,ETH/USDT"
    )
    raw = _prompt_value(
        f"Pairs, separated by commas (for example {example})",
        prompt=prompt,
    )
    selected = [item.strip() for item in raw.split(",") if item.strip()]
    return freeze_pairlist(config, resolved_pairs=selected)["pairs"]


def _nfi_backtest_pairs(
    workspace: Path,
    *,
    trading_mode: str,
) -> list[str] | None:
    mode = "futures" if trading_mode == "futures" else "spot"
    candidate = (
        workspace
        / "configs"
        / f"pairlist-backtest-static-binance-{mode}-usdt.json"
    )
    if not candidate.is_file():
        return None
    document = read_json(candidate)
    if not isinstance(document, dict):
        raise SpecValidationError(f"NFI pair preset is not a JSON object: {candidate}")
    return freeze_pairlist(document)["pairs"]


def _select_data_directory(
    workspace: Path,
    config_path: Path,
    config: dict[str, Any],
    *,
    data_directory: str | Path | None,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
    managed_default: bool,
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
            if interactive and not managed_default:
                raw = _prompt_value(
                    "Candle data directory",
                    default=_display_path(workspace, default),
                    prompt=prompt,
                )
                selected = resolve_workspace_path(workspace, raw)
            else:
                selected = default
            emit(
                f"managed candle storage: {selected} "
                "(missing public data downloads automatically)"
            )
    if selected.exists() and not selected.is_dir():
        raise SpecValidationError(f"candle data path is not a directory: {selected}")
    return selected


def _select_timerange(
    value: str | None,
    *,
    interactive: bool,
    prompt: Prompt,
    emit: Emitter,
    now: datetime | None,
) -> str:
    if value is not None:
        return value
    default = _default_timerange(now or datetime.now(UTC))
    if not interactive:
        emit(f"using quick-test period: {default}")
        return default
    emit("The recommended first run covers the most recent seven complete days.")
    return _prompt_value(
        "Backtest period (YYYYMMDD-YYYYMMDD)",
        default=default,
        prompt=prompt,
    )


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
    return (workspace / ".nfi" / "runs" / f"{slug}-{timerange}").resolve()


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
    end = now.date()
    start = end - timedelta(days=7)
    return f"{start:%Y%m%d}-{end:%Y%m%d}"


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
