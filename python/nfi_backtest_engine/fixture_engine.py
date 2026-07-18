"""Contract-fixture adapter for the Rust chronological simulator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import polars as pl

from .canonical import canonical_decimal, read_json, write_json
from .engine_runtime import run_engine
from .errors import BenchmarkError, StrategyAnalysisError
from .fixture import sha256_file, validate_fixture
from .mismatch_replay import create_mismatch_replay
from .parity import first_difference
from .specs import validate_trade_surface
from .state_trace import first_trace_difference, trace_summary
from .strategy_ir import analyze_strategy
from .trace_projection import project_engine_events, project_reference_trace

VerificationLevel = Literal["quick", "full"]


def run_fixture_engine(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    profile_path: str | Path | None = None,
    timeout_seconds: int | None = None,
    verification_level: VerificationLevel = "quick",
) -> dict[str, Any]:
    """Adapt one supported contract fixture, run Rust, and verify exact parity."""
    if verification_level not in {"quick", "full"}:
        raise BenchmarkError(
            f"verification level must be 'quick' or 'full', got {verification_level!r}"
        )
    manifest_file = Path(manifest_path).resolve()
    manifest = validate_fixture(
        manifest_file,
        validate_trace_semantics=False,
    )
    strategy_analysis = analyze_strategy(
        manifest_file.parent / _one_input(manifest, "strategy")["path"],
        class_name=manifest["freqtrade"]["strategy"],
    )
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"engine output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    input_path = output / "simulation-input.json"
    build_fixture_simulation_input(
        manifest_file,
        input_path,
        validated_manifest=manifest,
        strategy_analysis=strategy_analysis,
    )
    raw_result_path = output / "simulation-result.json"
    engine_events_path = (
        output / "engine-events.jsonl" if verification_level == "full" else None
    )
    execution = run_engine(
        input_path,
        raw_result_path,
        profile_path=profile_path,
        timeout_seconds=timeout_seconds,
        events_path=engine_events_path,
    )
    surface = engine_result_to_surface(
        manifest_file,
        raw_result_path,
        validated_manifest=manifest,
        strategy_analysis=strategy_analysis,
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
        state_difference = first_trace_difference(expected_trace_path, actual_trace_path)
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
    if not parity_equal:
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
                _artifact_record(engine_events_path)
                if engine_events_path is not None
                else None
            ),
            "engine_state_projection": (
                _artifact_record(actual_trace_path)
                if actual_trace_path is not None
                else None
            ),
        },
        "complete": parity_equal,
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
    manifest_file = Path(manifest_path).resolve()
    manifest = validated_manifest or validate_fixture(manifest_file)
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
        pair_series.append(
            {
                "pair": pair,
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
) -> dict[str, Any]:
    manifest_file = Path(manifest_path).resolve()
    manifest = validated_manifest or validate_fixture(manifest_file)
    result = read_json(result_path)
    analysis = strategy_analysis or analyze_strategy(
        manifest_file.parent / _one_input(manifest, "strategy")["path"],
        class_name=manifest["freqtrade"]["strategy"],
    )
    stoploss_ratio = analysis["strategies"][0]["constants"]["stoploss"]
    trades = [
        _surface_trade(trade, index, stoploss_ratio)
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
            "final_balance": _decimal(round(result["final_balance"], 8)),
            "profit_total_abs": _decimal(round(result["profit_total_abs"], 8)),
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
) -> dict[str, Any]:
    open_time = trade["open_timestamp_ms"]
    close_time = trade["close_timestamp_ms"]
    weekday = datetime.fromtimestamp(close_time / 1000, tz=timezone.utc).weekday()
    return {
        "sequence": sequence,
        "pair": trade["pair"],
        "direction": "long",
        "open_timestamp_ms": open_time,
        "close_timestamp_ms": close_time,
        "open_rate": _decimal(trade["open_rate"]),
        "close_rate": _decimal(trade["close_rate"]),
        "amount": _decimal(trade["amount"]),
        "stake_amount": _decimal(trade["stake_amount"]),
        "max_stake_amount": _decimal(trade["max_stake_amount"]),
        "leverage": "1",
        "entry_tag": trade["entry_tag"],
        "exit_reason": trade["exit_reason"],
        "fees": {
            "open_rate": _decimal(trade["fee_open"]),
            "open_cost": None,
            "open_currency": None,
            "close_rate": _decimal(trade["fee_close"]),
            "close_cost": None,
            "close_currency": None,
            "funding": "0",
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
    frame = frame.filter(
        (pl.col("_timestamp_ms") >= start_ms) & (pl.col("_timestamp_ms") < end_ms)
    ).slice(startup_candles).with_columns(
        pl.col("_raw_entry").shift(1).fill_null(False).alias("_enter_long")
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
    start_time = datetime.strptime(start, "%Y%m%d").replace(tzinfo=timezone.utc)
    end_time = datetime.strptime(end, "%Y%m%d").replace(tzinfo=timezone.utc)
    return int(start_time.timestamp() * 1000), int(end_time.timestamp() * 1000)


def _candle_input_for_pair(manifest: dict[str, Any], pair: str) -> dict[str, Any]:
    normalized = pair.replace("/", "_").replace(":", "_")
    candidates = [
        item
        for item in manifest["inputs"]
        if item["role"] == "candles" and Path(item["path"]).name.startswith(normalized)
    ]
    if len(candidates) != 1:
        raise BenchmarkError(f"expected one candle input for {pair}, found {len(candidates)}")
    return candidates[0]


def _one_input(manifest: dict[str, Any], role: str) -> dict[str, Any]:
    candidates = [item for item in manifest["inputs"] if item["role"] == role]
    if len(candidates) != 1:
        raise BenchmarkError(f"fixture requires exactly one {role!r} input")
    return candidates[0]


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
