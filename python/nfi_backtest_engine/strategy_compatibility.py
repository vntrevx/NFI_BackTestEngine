"""Fast compatibility preflight for a newly supplied strategy revision."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import write_json
from .config_loader import load_effective_config
from .errors import StrategyAnalysisError
from .hot_ir import build_hot_callback_ir
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

    if not blockers and len(analysis["strategies"]) == 1:
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
            blockers.append(
                {
                    "code": "EXACT_LOWERING_REVIEW_REQUIRED",
                    "message": str(exc),
                }
            )
        else:
            blockers.extend(callback_ir["blockers"])
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
        and callback_summary is not None
        and callback_summary["hot_loop_ready"],
        "blockers": blockers,
        "callback_ir": callback_summary,
    }
    if output_path is not None:
        write_json(output_path, report)
    return report
