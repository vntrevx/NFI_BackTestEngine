"""Fast compatibility preflight for a newly supplied strategy revision."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .callback_source_ir import compile_callback_source_ir
from .canonical import write_json
from .config_loader import load_effective_config
from .errors import StrategyAnalysisError
from .hot_ir import build_hot_callback_ir
from .state_machine_ir import compile_state_machine_program
from .strategy_ir import analyze_strategy


def check_strategy_compatibility(
    source: str | Path,
    *,
    class_name: str | None = None,
    config_path: str | Path | None = None,
    trading_mode: str | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Report whether the exact source can enter the native execution pipeline.

    This command deliberately performs no candle preparation or backtest. It is cheap
    enough for daily upstream checks and distinguishes a syntactically valid strategy
    from one whose active callbacks all have exact native lowerings.
    """
    analysis = analyze_strategy(source, class_name=class_name)
    blockers = [
        {
            "code": diagnostic["code"],
            "message": diagnostic["message"],
            "location": diagnostic["location"],
        }
        for diagnostic in analysis["diagnostics"]
        if diagnostic["severity"] == "error"
    ]
    selected_config: dict[str, Any] = {}
    config_identity: dict[str, Any] | None = None
    if config_path is not None:
        loaded = load_effective_config(config_path)
        selected_config = loaded["config"]
        config_identity = {
            "path": str(Path(config_path).resolve()),
            "sha256": loaded["sha256"],
        }
    effective_trading_mode = trading_mode or str(
        selected_config.get("trading_mode", "spot")
    )
    callback_summary: dict[str, Any] | None = None
    callback_source_summary: dict[str, Any] | None = None
    state_machine_summary: dict[str, Any] | None = None

    if not blockers and len(analysis["strategies"]) == 1:
        callback_source_ir = compile_callback_source_ir(
            source,
            class_name=class_name,
            trading_mode=(
                effective_trading_mode
                if effective_trading_mode in {"spot", "futures"}
                else "all"
            ),
            analysis=analysis,
        )
        callback_source_summary = {
            "schema_version": callback_source_ir["schema_version"],
            "fingerprint": callback_source_ir["fingerprint"],
            "entrypoints": [
                entrypoint["name"]
                for entrypoint in callback_source_ir["entrypoints"]
                if entrypoint["active_for_mode"]
            ],
            "route_keys": [item["key"] for item in callback_source_ir["route_keys"]],
            "emitted_tag_count": len(callback_source_ir["emitted_tags"]),
            "required_reads": callback_source_ir["required_reads"],
            "required_columns": callback_source_ir["required_columns"],
        }
        try:
            callback_ir = build_hot_callback_ir(
                analysis,
                trading_mode=effective_trading_mode,
                run_mode="backtest",
                config=selected_config,
            )
        except StrategyAnalysisError as exc:
            # Source-bound handwritten state machines intentionally raise here when a
            # future NFI patch changes observable callback behavior. Convert that
            # exception into a durable report instead of losing the upstream source
            # identity in a generic CLI error.
            state_machine_summary = _state_machine_summary(
                source,
                class_name=class_name,
                analysis=analysis,
                trading_mode=effective_trading_mode,
            )
            if state_machine_summary is None:
                blockers.append(
                    {
                        "code": "EXACT_LOWERING_REVIEW_REQUIRED",
                        "message": str(exc),
                    }
                )
        else:
            callback_summary = {
                "schema_version": callback_ir["schema_version"],
                "fingerprint": callback_ir["fingerprint"],
                "hot_loop_ready": callback_ir["hot_loop_ready"],
                "callbacks": [
                    {
                        "name": callback["name"],
                        "active_for_run": callback["active_for_run"],
                        "backend": callback["backend"],
                        "executable_in_rust": callback["executable_in_rust"],
                    }
                    for callback in callback_ir["callbacks"]
                ],
            }
            if not callback_ir["hot_loop_ready"]:
                state_machine_summary = _state_machine_summary(
                    source,
                    class_name=class_name,
                    analysis=analysis,
                    trading_mode=effective_trading_mode,
                )
            if state_machine_summary is None:
                blockers.extend(callback_ir["blockers"])

    report = {
        "schema_version": "1.0.0",
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": analysis["source"],
        "selected_class": (
            analysis["strategies"][0]["name"] if len(analysis["strategies"]) == 1 else None
        ),
        "trading_mode": effective_trading_mode,
        "config": config_identity,
        "static_safe": analysis["static_safe"],
        "native_compatible": not blockers
        and (
            (
                callback_summary is not None
                and callback_summary["hot_loop_ready"]
            )
            or state_machine_summary is not None
        ),
        "blockers": blockers,
        "callback_ir": callback_summary,
        "callback_source_ir": callback_source_summary,
        "state_machine_ir": state_machine_summary,
    }
    if output_path is not None:
        write_json(output_path, report)
    return report


def _state_machine_summary(
    source: str | Path,
    *,
    class_name: str | None,
    analysis: dict[str, Any],
    trading_mode: str,
) -> dict[str, Any] | None:
    if trading_mode != "spot":
        return None
    try:
        program = compile_state_machine_program(source, class_name=class_name)
    except StrategyAnalysisError:
        return None
    strategy = analysis["strategies"][0]
    active_callbacks = set(
        strategy.get(
            "strategy_callbacks",
            strategy.get("hot_callbacks", []),
        )
    )
    if trading_mode == "spot":
        active_callbacks.discard("leverage")
    compiled_callbacks = set(program["entrypoints"])
    if not active_callbacks or compiled_callbacks != active_callbacks:
        return None
    payload = json.dumps(
        program,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "schema_version": program["schema_version"],
        "sha256": hashlib.sha256(payload).hexdigest(),
        "entrypoints": sorted(compiled_callbacks),
        "required_columns": program["required_columns"],
        "required_state_keys": program["required_state_keys"],
        "opcodes": program["opcodes"],
    }
