"""Contract-fixture adapter for the Rust chronological simulator."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import polars as pl

from .branch_coverage import validate_fixture_coverage
from .callback_trace_projection import project_engine_events
from .canonical import canonical_decimal, read_json, write_json
from .data_seal import timeframe_milliseconds
from .engine_runtime import run_engine
from .errors import BenchmarkError, StrategyAnalysisError
from .fixture import (
    fixture_input_sha256,
    materialized_fixture,
    sha256_file,
    validate_fixture,
)
from .mismatch_replay import create_mismatch_replay
from .parity import first_difference
from .specs import validate_trade_surface
from .state_trace import (
    first_trace_difference,
    iter_validated_trace_events,
    trace_summary,
)
from .strategy_ir import analyze_strategy
from .trace_projection import (
    project_engine_events as project_portfolio_engine_events,
)
from .trace_projection import (
    project_reference_trace,
)


def _uses_legacy_reference_state(root: Path, manifest: dict[str, Any]) -> bool:
    source_record = manifest["artifacts"].get("state_trace")
    if source_record is None:
        return False
    first_event = next(iter_validated_trace_events(root / source_record["path"]), None)
    if first_event is None:
        return False
    state = first_event.get("state")
    return isinstance(state, dict) and "schema_version" not in state

VerificationLevel = Literal["quick", "full"]


def run_fixture_engine(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    profile_path: str | Path | None = None,
    timeout_seconds: int | None = None,
    verification_level: VerificationLevel = "quick",
) -> dict[str, Any]:
    """Adapt one retained contract fixture, run Rust, and verify exact parity."""
    manifest_file = Path(manifest_path).absolute()
    manifest = validate_fixture(
        manifest_file,
        validate_trace_semantics=False,
    )
    with materialized_fixture(manifest_file, manifest) as retained:
        return _run_fixture_engine_materialized(
            retained[0],
            retained[1],
            output_directory,
            profile_path=profile_path,
            timeout_seconds=timeout_seconds,
            verification_level=verification_level,
        )


def _run_fixture_engine_materialized(
    manifest_file: Path,
    manifest: dict[str, Any],
    output_directory: str | Path,
    *,
    profile_path: str | Path | None,
    timeout_seconds: int | None,
    verification_level: VerificationLevel,
) -> dict[str, Any]:
    if verification_level not in {"quick", "full"}:
        raise BenchmarkError(
            f"verification level must be 'quick' or 'full', got {verification_level!r}"
        )
    native_vector_input = _native_vector_input(manifest_file, manifest)
    if native_vector_input is None and (
        manifest["schema_version"] == "3.0.0"
        or manifest["freqtrade"]["strategy"]
        not in {"ContractStopsOnly", "ContractNormalRouting"}
    ):
        return _run_research_fixture_engine(
            manifest_file,
            manifest,
            output_directory=output_directory,
            profile_path=profile_path,
            timeout_seconds=timeout_seconds,
            verification_level=verification_level,
        )
    strategy_analysis = analyze_strategy(
        manifest_file.parent / _one_input(manifest, "strategy")["path"],
        class_name=manifest["freqtrade"]["strategy"],
    )
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"engine output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    if native_vector_input is None:
        input_path = output / "simulation-input.json"
        build_fixture_simulation_input(
            manifest_file,
            input_path,
            validated_manifest=manifest,
            strategy_analysis=strategy_analysis,
        )
    else:
        input_path = (manifest_file.parent / native_vector_input["path"]).resolve()
    raw_result_path = output / "simulation-result.json"
    engine_events_path = output / "engine-events.jsonl" if verification_level == "full" else None
    execution = run_engine(
        input_path,
        raw_result_path,
        profile_path=profile_path,
        timeout_seconds=timeout_seconds,
        events_path=engine_events_path,
        vector_manifest=native_vector_input is not None,
    )
    surface = engine_result_to_surface(
        manifest_file,
        raw_result_path,
        validated_manifest=manifest,
        strategy_analysis=strategy_analysis,
        vector_result=native_vector_input is not None,
    )
    surface_path = output / "trade-surface.json"
    write_json(surface_path, surface)
    expected_path = (
        manifest_file.parent / manifest["artifacts"]["trade_surface"]["path"]
    ).resolve()
    expected = read_json(expected_path)
    trade_difference = first_difference(expected, surface)
    trade_parity = {
        "equal": trade_difference is None,
        "difference": _difference_document(trade_difference),
    }
    state_parity: dict[str, Any] = {
        "checked": False,
        "equal": None,
        "difference": None,
        "expected": None,
        "actual": None,
    }
    expected_trace_path: Path | None = None
    actual_trace_path: Path | None = None
    state_difference = None
    if verification_level == "full":
        assert engine_events_path is not None
        projection_artifact = manifest["artifacts"].get("state_projection")
        expected_trace_path = (
            manifest_file.parent / projection_artifact["path"]
            if projection_artifact is not None
            else output / "reference-state-projected.trace"
        )
        actual_trace_path = output / "engine-state-projected.trace"
        assert expected_trace_path is not None
        assert actual_trace_path is not None
        if projection_artifact is None:
            project_reference_trace(
                manifest_file,
                expected_trace_path,
                manifest=manifest,
            )
        project_engine_events(
            manifest_file,
            engine_events_path,
            actual_trace_path,
            manifest=manifest,
        )
        state_difference = first_trace_difference(
            expected_trace_path,
            actual_trace_path,
            allow_actual_terminal_state_repeat=_uses_legacy_reference_state(
                manifest_file.parent,
                manifest,
            ),
        )
        state_parity = {
            "checked": True,
            "equal": state_difference is None,
            "difference": _trace_difference_document(state_difference),
            "expected": {
                "path": str(expected_trace_path),
                **trace_summary(expected_trace_path),
            },
            "actual": {
                "path": str(actual_trace_path),
                **trace_summary(actual_trace_path),
            },
        }
    parity_equal = trade_difference is None and state_parity["equal"] is not False
    mismatch_replay = None
    if not parity_equal and native_vector_input is None:
        mismatch_replay = create_mismatch_replay(
            output / "mismatch-replay",
            fixture_id=manifest["fixture_id"],
            manifest_path=manifest_file,
            simulation_input_path=input_path,
            expected_surface_path=expected_path,
            actual_surface_path=surface_path,
            trade_difference=trade_difference,
            state_difference=state_difference,
            expected_trace_path=expected_trace_path,
            actual_trace_path=actual_trace_path,
        )
    report = {
        "schema_version": "1.0.0",
        "fixture_id": manifest["fixture_id"],
        "verification_level": verification_level,
        "execution": execution,
        "strategy": {
            "class_name": manifest["freqtrade"]["strategy"],
            "source_sha256": _one_input(manifest, "strategy")["sha256"],
            "static_safe": strategy_analysis["static_safe"],
            "diagnostic_count": len(strategy_analysis["diagnostics"]),
        },
        "parity": {
            "equal": parity_equal,
            "trade_surface": trade_parity,
            "state_trace": state_parity,
        },
        "mismatch_replay": mismatch_replay,
        "artifacts": {
            "simulation_input": _artifact_record(input_path),
            "simulation_result": _artifact_record(raw_result_path),
            "trade_surface": _artifact_record(surface_path),
            "engine_events": (
                _artifact_record(engine_events_path) if engine_events_path is not None else None
            ),
            "engine_state_projection": (
                _artifact_record(actual_trace_path) if actual_trace_path is not None else None
            ),
        },
        "complete": parity_equal,
    }
    write_json(output / "run.json", report)
    return report


def _run_research_fixture_engine(
    manifest_file: Path,
    manifest: dict[str, Any],
    *,
    output_directory: str | Path,
    profile_path: str | Path | None,
    timeout_seconds: int | None,
    verification_level: VerificationLevel,
) -> dict[str, Any]:
    """Execute a compiled fixture through the real research pipeline."""
    from .research_runner import run_research_backtest

    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"engine output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    root = manifest_file.parent
    strategy_input = _one_input(manifest, "strategy")
    config_input = _one_input(manifest, "config")
    market_input = _one_input(manifest, "market_metadata")
    config = read_json(root / config_input["path"])
    pairs = config["exchange"]["pair_whitelist"]
    interval_identity = official_consumed_interval(manifest_file, manifest)
    research_output = output / "research"
    selected_profile = (
        Path(profile_path).resolve()
        if profile_path is not None
        else output / "execution-profile.json"
    )
    manifest_payload = getattr(manifest, "manifest_payload", None)
    if not isinstance(manifest_payload, bytes):
        raise BenchmarkError("fixture validation did not retain manifest bytes")
    official_trace_inputs = [
        item
        for item in manifest["inputs"]
        if item["role"] == "auxiliary"
        and item["sha256"] == interval_identity["official_trace_sha256"]
    ]
    if len(official_trace_inputs) > 1:
        raise BenchmarkError("fixture has ambiguous official portfolio trace inputs")
    portfolio_envelope_identity = (
        {
            "fixture_id": manifest["fixture_id"],
            "fixture_manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
            "scheduler_contract_sha256": interval_identity["scheduler_contract_sha256"],
            "scheduler_contract_fingerprint": interval_identity[
                "scheduler_contract_fingerprint"
            ],
            "portfolio_contract_sha256": interval_identity["portfolio_contract_sha256"],
            "portfolio_contract_fingerprint": interval_identity[
                "portfolio_contract_fingerprint"
            ],
            "source_sha256": strategy_input["sha256"],
            "config_sha256": config_input["sha256"],
            "data_sha256": interval_identity["data_sha256"],
            "official_trace_sha256": interval_identity["official_trace_sha256"],
            "native_timerange": interval_identity["native_timerange"],
            "configured_pairs": pairs,
            "slot_limit": int(config["max_open_trades"]),
        }
        if official_trace_inputs
        else None
    )
    research = run_research_backtest(
        strategy_path=root / strategy_input["path"],
        class_name=manifest["freqtrade"]["strategy"],
        config_path=root / config_input["path"],
        data_directory=_fixture_data_directory(root, manifest),
        timerange=interval_identity["native_timerange"],
        output_directory=research_output,
        pairs=pairs,
        workers=1,
        cache_directory=output / "vector-cache",
        profile_path=selected_profile,
        resume=False,
        prepare_only=False,
        download_missing=False,
        market_metadata_path=root / market_input["path"],
        download_market_metadata=False,
        recalibrate=True,
        history_coverage_policy="strict",
        trace_engine_events=verification_level == "full",
        portfolio_envelope_identity=portfolio_envelope_identity,
    )
    surface_path = research_output / "trade-surface.json"
    expected_path = root / manifest["artifacts"]["trade_surface"]["path"]
    if surface_path.is_file():
        surface = read_json(surface_path)
        surface["context"]["timerange"] = manifest["freqtrade"]["timerange"]
        write_json(surface_path, surface)
    trade_difference = (
        first_difference(read_json(expected_path), read_json(surface_path))
        if surface_path.is_file()
        else None
    )
    trade_equal = research["complete"] and surface_path.is_file() and trade_difference is None
    state_parity: dict[str, Any] = {
        "checked": False,
        "equal": None,
        "difference": None,
        "expected": None,
        "actual": None,
    }
    actual_trace_path: Path | None = None
    state_difference = None
    if verification_level == "full" and research["complete"]:
        engine_events = research_output / "engine-events.jsonl"
        expected_trace_path = (
            root / manifest["artifacts"]["state_projection"]["path"]
            if "state_projection" in manifest["artifacts"]
            else output / "reference-state-projected.trace"
        )
        if "state_projection" not in manifest["artifacts"]:
            project_reference_trace(
                manifest_file,
                expected_trace_path,
                manifest=manifest,
            )
        actual_trace_path = output / "engine-state-projected.trace"
        expected_phases = {
            event["phase"] for event in iter_validated_trace_events(expected_trace_path)
        }
        projector = (
            project_portfolio_engine_events
            if expected_phases == {"portfolio.after_candle"}
            else project_engine_events
        )
        projector(
            manifest_file,
            engine_events,
            actual_trace_path,
            manifest=manifest,
        )
        state_difference = first_trace_difference(
            expected_trace_path,
            actual_trace_path,
            allow_actual_terminal_state_repeat=_uses_legacy_reference_state(root, manifest),
        )
        state_parity = {
            "checked": True,
            "equal": state_difference is None,
            "difference": _trace_difference_document(state_difference),
            "expected": {
                "path": str(expected_trace_path),
                **trace_summary(expected_trace_path),
            },
            "actual": {
                "path": str(actual_trace_path),
                **trace_summary(actual_trace_path),
            },
        }
    portfolio_parity: dict[str, Any] = {
        "checked": False,
        "equal": None,
        "event_count": None,
        "difference": None,
        "verification": None,
    }
    portfolio_verification_path: Path | None = None
    portfolio_events_path = research_output / "portfolio-events.json"
    if (
        research["complete"]
        and portfolio_events_path.is_file()
        and official_trace_inputs
    ):
        from .portfolio_trace import verify_portfolio_trace

        if len(official_trace_inputs) != 1:
            raise BenchmarkError("fixture has ambiguous official portfolio trace inputs")
        official_trace_input = official_trace_inputs[0]
        portfolio_verification_path = output / "portfolio-verification.json"
        portfolio_verification = verify_portfolio_trace(
            manifest_file,
            root / official_trace_input["path"],
            portfolio_events_path,
            output_path=portfolio_verification_path,
        )
        native_header = read_json(portfolio_events_path)["portfolio_header"]
        execution = research["result"]["execution"]
        if native_header["native_binary_sha256"] != execution["build"]["binary_sha256"]:
            raise BenchmarkError("portfolio envelope Native binary identity differs")
        portfolio_parity = {
            "checked": True,
            "equal": portfolio_verification["exact"],
            "event_count": portfolio_verification["event_count"],
            "difference": portfolio_verification["mismatch"],
            "verification": portfolio_verification,
        }
    parity_equal = (
        trade_equal
        and state_parity["equal"] is not False
        and portfolio_parity["equal"] is not False
    )
    coverage = (
        validate_fixture_coverage(manifest_file, manifest)
        if manifest["schema_version"] == "3.0.0"
        else None
    )
    report = {
        "schema_version": "1.1.0",
        "fixture_id": manifest["fixture_id"],
        "verification_level": verification_level,
        "execution": (
            research["result"]["execution"]
            if isinstance(research.get("result"), dict)
            else {
                "peak_rss_bytes": None,
                "exit_code": 1,
            }
        ),
        "strategy": {
            "class_name": manifest["freqtrade"]["strategy"],
            "source_sha256": strategy_input["sha256"],
            "static_safe": research["capability"]["strategy_static_safe"],
            "diagnostic_count": len(research["capability"]["blockers"]),
        },
        "parity": {
            "equal": parity_equal,
            "trade_surface": {
                "equal": trade_equal,
                "difference": _difference_document(trade_difference),
            },
            "state_trace": state_parity,
            "portfolio_trace": portfolio_parity,
        },
        "branch_coverage": coverage,
        "research_report": _artifact_record(research_output / "run.json"),
        "artifacts": {
            "trade_surface": (
                _artifact_record(surface_path) if surface_path.is_file() else None
            ),
            "engine_state_projection": (
                _artifact_record(actual_trace_path)
                if actual_trace_path is not None
                else None
            ),
            "portfolio_events": (
                _artifact_record(portfolio_events_path)
                if portfolio_events_path.is_file()
                else None
            ),
            "portfolio_verification": (
                _artifact_record(portfolio_verification_path)
                if portfolio_verification_path is not None
                else None
            ),
        },
        "complete": parity_equal and (coverage is None or coverage["met"]),
    }
    write_json(output / "run.json", report)
    return report


def _difference_document(difference: Any) -> dict[str, Any] | None:
    if difference is None:
        return None
    return {
        "path": difference.path,
        "expected": _json_value(difference.expected),
        "actual": _json_value(difference.actual),
        "reason": difference.reason,
    }


def _trace_difference_document(difference: Any) -> dict[str, Any] | None:
    if difference is None:
        return None
    return {
        "sequence": difference.sequence,
        "path": difference.path,
        "expected": _json_value(difference.expected),
        "actual": _json_value(difference.actual),
        "reason": difference.reason,
        "event_key": difference.event_key,
    }


def _json_value(value: Any) -> Any:
    if type(value).__name__ == "_Missing":
        return {"missing": True}
    return value


def build_fixture_simulation_input(
    manifest_path: str | Path,
    destination: str | Path,
    *,
    validated_manifest: dict[str, Any] | None = None,
    strategy_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_file = Path(manifest_path).absolute()
    if validated_manifest is None:
        manifest = validate_fixture(manifest_file)
        with materialized_fixture(manifest_file, manifest) as retained:
            return build_fixture_simulation_input(
                retained[0],
                destination,
                validated_manifest=retained[1],
                strategy_analysis=strategy_analysis,
            )
    manifest = validated_manifest
    root = manifest_file.parent
    strategy_input = _one_input(manifest, "strategy")
    config_input = _one_input(manifest, "config")
    market_input = _one_input(manifest, "market_metadata")
    strategy_path = root / strategy_input["path"]
    config = read_json(root / config_input["path"])
    market_snapshot = read_json(root / market_input["path"])
    strategy_name = manifest["freqtrade"]["strategy"]
    analysis = strategy_analysis or analyze_strategy(
        strategy_path,
        class_name=strategy_name,
    )
    errors = [item for item in analysis["diagnostics"] if item["severity"] == "error"]
    if errors:
        first = errors[0]
        location = first["location"]
        raise StrategyAnalysisError(
            f"{location['path']}:{location['line']}:{location['column']}: "
            f"{first['code']}: {first['message']}"
        )
    strategy = analysis["strategies"][0]
    constants = strategy["constants"]
    if manifest["freqtrade"]["trading_mode"] != "spot":
        line = strategy["location"]["line"]
        raise StrategyAnalysisError(
            f"{strategy_path}:{line}:0: FUTURES_SEMANTICS_UNSUPPORTED: "
            "the current Rust fixture adapter supports spot-long semantics only"
        )
    if strategy_name not in {"ContractStopsOnly", "ContractNormalRouting"}:
        line = strategy["location"]["line"]
        raise StrategyAnalysisError(
            f"{strategy_path}:{line}:0: HOT_CALLBACK_IR_UNSUPPORTED: "
            f"fixture adapter does not compile {strategy_name!r}"
        )

    pairs = config["exchange"]["pair_whitelist"]
    pair_series = []
    for pair in pairs:
        candle_input = _candle_input_for_pair(manifest, pair)
        frame = pl.read_ipc(root / candle_input["path"], memory_map=True, rechunk=False)
        pair_market = market_snapshot["markets"][pair]
        pair_series.append(
            {
                "pair": pair,
                "amount_step": float(pair_market["precision"]["amount"]),
                "price_step": float(pair_market["precision"]["price"]),
                "candles": _contract_candles(
                    frame,
                    strategy_name=strategy_name,
                    startup_candles=int(constants.get("startup_candle_count", 0)),
                    timerange=manifest["freqtrade"]["timerange"],
                ),
            }
        )
    market = market_snapshot["markets"][pairs[0]]
    fee_rate = _command_option_float(manifest["freqtrade"]["command"], "--fee")
    document = {
        "schema_version": "1.0.0",
        "config": {
            "starting_balance": float(config["dry_run_wallet"]),
            "max_open_trades": min(int(config["max_open_trades"]), len(pairs)),
            "stake_amount": float(config["stake_amount"]),
            "fee_rate": fee_rate,
            "stoploss_ratio": float(constants["stoploss"]),
            "amount_step": float(market["precision"]["amount"]),
            "price_step": float(market["precision"]["price"]),
            "custom_exit_after_ms": (
                6 * 60 * 60 * 1000 if strategy_name == "ContractNormalRouting" else None
            ),
            "adjustment_rule": (
                {
                    "profit_below": -0.004,
                    "stake_ratio": 0.5,
                    "max_adjustments": 1,
                    "tag": "contract_rebuy",
                }
                if strategy_name == "ContractNormalRouting"
                else None
            ),
        },
        "pairs": pair_series,
    }
    write_json(destination, document)
    return document


def engine_result_to_surface(
    manifest_path: str | Path,
    result_path: str | Path,
    *,
    validated_manifest: dict[str, Any] | None = None,
    strategy_analysis: dict[str, Any] | None = None,
    vector_result: bool = False,
) -> dict[str, Any]:
    manifest_file = Path(manifest_path).absolute()
    if validated_manifest is None:
        manifest = validate_fixture(manifest_file)
        with materialized_fixture(manifest_file, manifest) as retained:
            return engine_result_to_surface(
                retained[0],
                result_path,
                validated_manifest=retained[1],
                strategy_analysis=strategy_analysis,
                vector_result=vector_result,
            )
    manifest = validated_manifest
    result = read_json(result_path)
    analysis = strategy_analysis or analyze_strategy(
        manifest_file.parent / _one_input(manifest, "strategy")["path"],
        class_name=manifest["freqtrade"]["strategy"],
    )
    stoploss_ratio = analysis["strategies"][0]["constants"]["stoploss"]
    trades = [
        _surface_trade(
            trade,
            index,
            stoploss_ratio,
            vector_result=vector_result,
        )
        for index, trade in enumerate(result["trades"])
    ]
    surface = {
        "schema_version": "2.0.0",
        "strategy": manifest["freqtrade"]["strategy"],
        "context": {
            "trading_mode": manifest["freqtrade"]["trading_mode"],
            "margin_mode": manifest["freqtrade"]["margin_mode"] or "",
            "timeframe": manifest["freqtrade"]["timeframe"],
            "timeframe_detail": manifest["freqtrade"]["timeframe_detail"] or "",
            "timerange": manifest["freqtrade"]["timerange"],
        },
        "summary": {
            "total_trades": len(trades),
            "starting_balance": _decimal(result["starting_balance"]),
            "final_balance": _decimal(
                result["final_balance"]
                if vector_result
                else round(result["final_balance"], 8)
            ),
            "profit_total_abs": _decimal(
                result["profit_total_abs"]
                if vector_result
                else round(result["profit_total_abs"], 8)
            ),
            "total_volume": _decimal(result["total_volume"]),
            "rejected_signals": result["rejected_signals"],
            "timedout_entry_orders": 0,
            "timedout_exit_orders": 0,
            "canceled_trade_entries": 0,
            "canceled_entry_orders": 0,
            "replaced_entry_orders": 0,
            "max_open_trades": result["maximum_concurrent_trades"],
        },
        "locks": [],
        "trades": trades,
    }
    validate_trade_surface(surface)
    return surface


def _surface_trade(
    trade: dict[str, Any],
    sequence: int,
    stoploss_ratio: float,
    *,
    vector_result: bool,
) -> dict[str, Any]:
    open_time = trade["open_timestamp_ms"]
    close_time = trade["close_timestamp_ms"]
    weekday = datetime.fromtimestamp(close_time / 1000, tz=UTC).weekday()
    return {
        "sequence": sequence,
        "pair": trade["pair"],
        "direction": "short" if vector_result and trade.get("is_short") else "long",
        "open_timestamp_ms": open_time,
        "close_timestamp_ms": close_time,
        "open_rate": _decimal(trade["open_rate"]),
        "close_rate": _decimal(trade["close_rate"]),
        "amount": _decimal(trade["amount"]),
        "stake_amount": _decimal(
            round(trade["stake_amount"], 8) if vector_result else trade["stake_amount"]
        ),
        "max_stake_amount": _decimal(
            round(trade["max_stake_amount"], 8)
            if vector_result
            else trade["max_stake_amount"]
        ),
        "leverage": _decimal(trade.get("leverage", 1)) if vector_result else "1",
        "entry_tag": trade["entry_tag"],
        "exit_reason": trade["exit_reason"],
        "fees": {
            "open_rate": _decimal(trade["fee_open"]),
            "open_cost": None,
            "open_currency": None,
            "close_rate": _decimal(trade["fee_close"]),
            "close_cost": None,
            "close_currency": None,
            "funding": _decimal(trade.get("funding_fees", 0)) if vector_result else "0",
        },
        "profit": {
            "absolute": _decimal(round(trade["profit_abs"], 8)),
            "ratio": _decimal(trade["profit_ratio"]),
        },
        "liquidation_price": None,
        "initial_stop_loss": _decimal(trade["initial_stop_loss"]),
        "stop_loss": _decimal(trade["stop_loss"]),
        "orders": [
            {
                "sequence": order_index,
                "side": order["side"],
                "is_entry": order["is_entry"],
                "filled_timestamp_ms": order["filled_timestamp_ms"],
                "amount": _decimal(order["amount"]),
                "price": _decimal(order["price"]),
                "cost": _decimal(order["cost"]),
                "tag": order["tag"],
            }
            for order_index, order in enumerate(trade["orders"])
        ],
        "duration_minutes": (close_time - open_time) // 60_000,
        "is_open": False,
        "minimum_rate": _decimal(trade["minimum_rate"]),
        "maximum_rate": _decimal(trade["maximum_rate"]),
        "initial_stop_loss_ratio": _decimal(stoploss_ratio),
        "stop_loss_ratio": _decimal(stoploss_ratio),
        "weekday": weekday,
    }


def _contract_candles(
    frame: pl.DataFrame,
    *,
    strategy_name: str,
    startup_candles: int,
    timerange: str,
) -> list[dict[str, Any]]:
    frame = frame.sort("date").with_columns(pl.col("date").cast(pl.Int64).alias("_timestamp_ms"))
    if strategy_name == "ContractStopsOnly":
        previous_green = pl.col("close").shift(1) > pl.col("open").shift(1)
        raw_entry = (
            (pl.col("volume") > 0)
            & previous_green.fill_null(False)
            & (pl.col("close") < pl.col("open"))
        )
        tag = "contract_stop"
    else:
        raw_values = [(index % 72) == 0 for index in range(frame.height)]
        raw_entry = pl.Series("_raw_entry", raw_values)
        tag = "contract_route"
    frame = frame.with_columns(raw_entry.alias("_raw_entry"))
    start_ms, end_ms = _timerange_bounds(timerange)
    frame = (
        frame.filter((pl.col("_timestamp_ms") >= start_ms) & (pl.col("_timestamp_ms") < end_ms))
        .slice(startup_candles)
        .with_columns(pl.col("_raw_entry").shift(1).fill_null(False).alias("_enter_long"))
    )
    rows = frame.select(
        "_timestamp_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "_enter_long",
    ).iter_rows(named=True)
    return [
        {
            "timestamp_ms": row["_timestamp_ms"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "enter_long": {"tag": tag} if row["_enter_long"] else None,
            "exit_long": None,
            "adjustment": None,
        }
        for row in rows
    ]


def _timerange_bounds(timerange: str) -> tuple[int, int]:
    start, end = timerange.split("-", 1)
    start_time = datetime.strptime(start, "%Y%m%d").replace(tzinfo=UTC)
    end_time = datetime.strptime(end, "%Y%m%d").replace(tzinfo=UTC)
    return int(start_time.timestamp() * 1000), int(end_time.timestamp() * 1000)


def _candle_input_for_pair(manifest: dict[str, Any], pair: str) -> dict[str, Any]:
    normalized = pair.replace("/", "_").replace(":", "_")
    timeframe = manifest["freqtrade"]["timeframe"]
    expected_names = {
        f"{normalized}-{timeframe}.feather",
        f"{normalized}-{timeframe}-futures.feather",
    }
    candidates = [
        item
        for item in manifest["inputs"]
        if item["role"] == "candles" and Path(item["path"]).name in expected_names
    ]
    if len(candidates) != 1:
        raise BenchmarkError(
            f"expected one primary candle input for {pair}, found {len(candidates)}"
        )
    return candidates[0]


def official_consumed_interval(
    manifest_file: str | Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Authenticate the exact Oracle-consumed slots before narrowing a fixture run."""
    root = Path(manifest_file).resolve().parent
    configured = read_json(root / _one_input(manifest, "config")["path"])["exchange"][
        "pair_whitelist"
    ]
    auxiliary = [item for item in manifest["inputs"] if item["role"] == "auxiliary"]
    documents = [(item, read_json(root / item["path"])) for item in auxiliary]
    traces = [
        (item, document)
        for item, document in documents
        if isinstance(document, dict)
        and document.get("schema_version") == "freqtrade-portfolio-pressure-trace-v1"
    ]
    authentications = [
        document
        for _item, document in documents
        if isinstance(document, dict)
        and document.get("schema_version") == "official-source-authentication-v1"
    ]
    portfolio_contract_path = (
        Path(__file__).parent / "contracts/freqtrade-portfolio-pressure-contract-v1.json"
    )
    scheduler_contract_path = Path(__file__).parent / "contracts/freqtrade-scheduler-contract.json"
    portfolio_contract = read_json(portfolio_contract_path)
    scheduler_contract = read_json(scheduler_contract_path)

    if len(traces) == 1 and len(authentications) == 1:
        trace_input, trace = traces[0]
        authentication = authentications[0]
        if (
            trace.get("configured_pair_order") != configured
            or authentication.get("configured_pair_order") != configured
        ):
            raise BenchmarkError("official-consumed configured pair order differs")
        events = trace.get("events")
        if not isinstance(events, list) or not events:
            raise BenchmarkError("official-consumed trace has no events")
        event_timestamps: set[int] = set()
        for event in events:
            if not isinstance(event, dict) or event.get("pair") not in configured:
                continue
            timestamp = event.get("timestamp_ms")
            if not isinstance(timestamp, int) or isinstance(timestamp, bool):
                raise BenchmarkError("official-consumed trace has an invalid timestamp")
            event_timestamps.add(timestamp)
        official_trace_sha256 = trace_input["sha256"]
        scheduler_contract_fingerprint = authentication[
            "scheduler_contract_fingerprint"
        ]
        portfolio_contract_sha256 = authentication["portfolio_contract"]["sha256"]
        require_exact_candle_slots = True
    elif not traces and not authentications:
        event_timestamps, official_trace_sha256 = _legacy_official_trace_identity(
            root,
            manifest,
            configured,
        )
        scheduler_contract_fingerprint = scheduler_contract["fingerprint"]
        portfolio_contract_sha256 = sha256_file(portfolio_contract_path)
        require_exact_candle_slots = False
    else:
        raise BenchmarkError(
            "fixture requires matched official-consumed trace and authentication"
        )

    if not event_timestamps:
        raise BenchmarkError("official-consumed trace has no configured-pair events")
    step = timeframe_milliseconds(manifest["freqtrade"]["timeframe"])
    event_start = min(event_timestamps)
    event_end = max(event_timestamps)
    interval_start = event_start - step
    interval_end = event_end + step
    expected = set(range(interval_start, interval_end, step))
    candle_identities: list[dict[str, Any]] = []
    for pair in configured:
        candle = _candle_input_for_pair(manifest, pair)
        candle_path = root / candle["path"]
        frame = pl.read_ipc(candle_path, memory_map=True, rechunk=False)
        timestamps = set(frame["date"].dt.epoch("ms").to_list())
        missing = sorted(expected - timestamps)
        if missing:
            raise BenchmarkError(
                f"official-consumed interval for {pair} has missing slot {missing[0]}"
            )
        if require_exact_candle_slots and timestamps != expected:
            raise BenchmarkError(
                f"official-consumed interval for {pair} has unbound slots"
            )
        candle_identities.append(
            {
                "pair": pair,
                "path": candle["path"],
                "sha256": candle["sha256"],
                "bytes": candle["bytes"],
            }
        )
    data_identity = hashlib.sha256(
        json.dumps(candle_identities, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": "official-consumed-interval-v1",
        "native_timerange": f"{interval_start}-{interval_end}",
        "official_event_start_timestamp_ms": event_start,
        "official_event_end_timestamp_ms": event_end,
        "timeframe_ms": step,
        "configured_pairs": configured,
        "official_trace_sha256": official_trace_sha256,
        "data_sha256": data_identity,
        "scheduler_contract_sha256": sha256_file(scheduler_contract_path),
        "scheduler_contract_fingerprint": scheduler_contract_fingerprint,
        "portfolio_contract_sha256": portfolio_contract_sha256,
        "portfolio_contract_fingerprint": portfolio_contract["fingerprint"],
    }


def _legacy_official_trace_identity(
    root: Path,
    manifest: dict[str, Any],
    configured_pairs: list[str],
) -> tuple[set[int], str]:
    trace = manifest["artifacts"].get("state_trace")
    if not isinstance(trace, dict):
        raise BenchmarkError("legacy fixture requires one sealed full state trace")
    trace_path = root / trace["path"]
    summary = trace_summary(trace_path)
    strategy = _one_input(manifest, "strategy")
    config = _one_input(manifest, "config")
    expected_header = {
        "source": "freqtrade-reference",
        "input_sha256": fixture_input_sha256(manifest["inputs"]),
        "strategy_sha256": strategy["sha256"],
        "profile_sha256": config["sha256"],
        "trading_mode": manifest["freqtrade"]["trading_mode"],
        "include_state": True,
    }
    for field, expected in expected_header.items():
        if summary[field] != expected:
            raise BenchmarkError(f"legacy state trace {field} identity differs")

    ordered_pairs: dict[int, list[str]] = {}
    configured = set(configured_pairs)
    for event in iter_validated_trace_events(trace_path):
        if event["phase"] != "candle.after":
            continue
        pair = event["pair"]
        if pair not in configured:
            raise BenchmarkError("legacy state trace contains an unconfigured candle pair")
        ordered_pairs.setdefault(event["timestamp_ms"], []).append(pair)
    if not ordered_pairs:
        raise BenchmarkError("legacy state trace has no candle events")
    for timestamp, pairs in ordered_pairs.items():
        if pairs != configured_pairs:
            raise BenchmarkError(
                f"legacy state trace configured pair order differs at {timestamp}"
            )
    return set(ordered_pairs), trace["sha256"]


def _fixture_data_directory(root: Path, manifest: dict[str, Any]) -> Path:
    """Derive the Freqtrade datadir from the fixture's sealed candle inputs.

    Captured fixtures retain their original layout. Spot captures can store
    candles directly under ``inputs/candles`` or ``inputs/data`` while futures
    captures use Freqtrade's ``inputs/data/futures`` subdirectory. Deriving the
    root from input roles keeps the runner independent of a particular fixture
    name and avoids recursive file searches on large datasets.
    """

    data_roots: set[Path] = set()
    for item in manifest.get("inputs", []):
        if item.get("role") not in {"candles", "funding_candles", "mark_candles"}:
            continue
        parent = Path(item["path"]).parent
        data_roots.add(parent.parent if parent.name == "futures" else parent)
    if len(data_roots) != 1:
        raise BenchmarkError(
            "fixture candle inputs must resolve to one shared data directory"
        )

    fixture_root = root.resolve()
    data_directory = (fixture_root / data_roots.pop()).resolve()
    try:
        data_directory.relative_to(fixture_root)
    except ValueError as exc:
        raise BenchmarkError("fixture candle data directory escapes the fixture") from exc
    if not data_directory.is_dir():
        raise BenchmarkError(
            f"fixture candle data directory is missing: {data_directory}"
        )
    return data_directory


def _one_input(manifest: dict[str, Any], role: str) -> dict[str, Any]:
    candidates = [item for item in manifest["inputs"] if item["role"] == role]
    if len(candidates) != 1:
        raise BenchmarkError(f"fixture requires exactly one {role!r} input")
    return candidates[0]


def _native_vector_input(
    manifest_file: Path,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    value = manifest["artifacts"].get("native_vector_manifest")
    if not isinstance(value, dict):
        return None
    document = read_json(manifest_file.parent / value["path"])
    if not isinstance(document, dict):
        raise BenchmarkError("Native vector manifest must be a JSON object")
    validate_native_manager_binding(manifest, document)
    return value


def validate_native_manager_binding(
    manifest: dict[str, Any],
    native_document: dict[str, Any],
) -> None:
    config = native_document.get("config")
    manager = config.get("nfi_x7_trade_manager") if isinstance(config, dict) else None
    if not isinstance(manager, dict):
        raise BenchmarkError("Native vector manifest requires a compiled NFI trade manager")
    expected_source = manifest.get("strategy_provenance", {}).get("base_source_sha256")
    if (
        not isinstance(expected_source, str)
        or manager.get("source_sha256") != expected_source
    ):
        raise BenchmarkError("Native trade manager source differs from fixture provenance")


def _command_option_float(command: list[str], option: str) -> float:
    try:
        index = command.index(option)
        return float(command[index + 1])
    except (ValueError, IndexError) as exc:
        raise BenchmarkError(f"fixture command is missing {option}") from exc


def _decimal(value: Any) -> str:
    result = canonical_decimal(value, path="$engine")
    assert result is not None
    return result


def _artifact_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
