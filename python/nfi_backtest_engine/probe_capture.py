"""One-command capture of branch-reaching Full X7 official fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import read_json
from .data_seal import compact_candle_directory
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file
from .fixture_capture import CaptureInput, finalize_fixture_v3, stage_fixture_v3
from .probe_strategy import prepare_probe_strategy, write_probe_config
from .reference_runtime import (
    REFERENCE_IMAGE,
    REFERENCE_INDEX_DIGEST,
    REFERENCE_PLATFORM,
    REFERENCE_PLATFORM_DIGEST,
    REFERENCE_TRACER_VERSION,
    REFERENCE_VERSION,
)
from .research_reference import run_research_reference
from .research_runner import run_research_backtest
from .strategy_ir import analyze_strategy

PROBE_SPEC_VERSION = "1.0.0"


def capture_x7_probe(
    spec_path: str | Path,
    output_directory: str | Path,
    work_directory: str | Path,
    *,
    timeout_seconds: int,
    workers: int | None = None,
) -> dict[str, Any]:
    """Transform, execute, trace, finalize, and validate one official probe."""
    spec_file = Path(spec_path).resolve()
    spec = _load_probe_spec(spec_file)
    root = spec_file.parent
    output = Path(output_directory).resolve()
    work = Path(work_directory).resolve()
    _require_empty_destination(output, "probe fixture")
    _require_empty_destination(work, "probe work")
    work.mkdir(parents=True, exist_ok=True)
    prepared = work / "prepared"
    prepared.mkdir()

    strategy_spec = spec["strategy"]
    config_spec = spec["config"]
    data_spec = spec["data"]
    market_spec = spec["markets"]
    execution_spec = spec["execution"]
    strategy_source = _resolve_file(root, strategy_spec["source"], "strategy")
    config_source = _resolve_file(root, config_spec["source"], "config")
    data_directory = _resolve_directory(root, data_spec["directory"], "data")
    engine_markets = _resolve_file(root, market_spec["engine"], "engine markets")
    reference_markets = _resolve_file(
        root,
        market_spec["reference"],
        "reference markets",
    )
    profile = _resolve_file(root, execution_spec["profile"], "execution profile")

    effective_strategy = prepared / "strategy.py"
    provenance = prepare_probe_strategy(
        strategy_source,
        effective_strategy,
        class_name=strategy_spec["class_name"],
        upstream_repository=spec["upstream"]["repository"],
        upstream_commit=spec["upstream"]["commit"],
        boolean_toggles=strategy_spec.get("boolean_toggles"),
        protections=strategy_spec.get("protections"),
    )
    effective_config = prepared / "config.json"
    config_transformation = write_probe_config(
        config_source,
        effective_config,
        overrides=config_spec["overrides"],
        remove_paths=config_spec["remove_paths"],
    )
    if config_spec["overrides"] or config_spec["remove_paths"]:
        provenance["transformations"].append(config_transformation)

    analysis = analyze_strategy(
        effective_strategy,
        class_name=strategy_spec["class_name"],
    )
    if not analysis["static_safe"] or len(analysis["strategies"]) != 1:
        raise SpecValidationError(
            "effective probe strategy must contain one static-safe selected class"
        )
    selected_strategy = analysis["strategies"][0]
    raw_startup = selected_strategy["constants"].get("startup_candle_count", 0)
    if (
        not isinstance(raw_startup, int)
        or isinstance(raw_startup, bool)
        or raw_startup < 0
    ):
        raise SpecValidationError(
            "effective probe strategy startup_candle_count must be non-negative"
        )
    required_timeframes = selected_strategy.get("required_timeframes")
    if (
        not isinstance(required_timeframes, list)
        or not required_timeframes
        or not all(
            isinstance(timeframe, str) and timeframe
            for timeframe in required_timeframes
        )
    ):
        raise SpecValidationError(
            "effective probe strategy must expose required timeframes"
        )
    effective_config_document = read_json(effective_config)
    trading_mode = str(effective_config_document.get("trading_mode", "spot"))
    compact_data_directory = prepared / "data"
    compact_candle_directory(
        data_directory,
        compact_data_directory,
        pairs=data_spec["pairs"],
        timeframes=required_timeframes,
        trading_mode=trading_mode,
        timerange=data_spec["timerange"],
        startup_candles=raw_startup,
    )
    engine_output = work / "engine"
    engine = run_research_backtest(
        strategy_path=effective_strategy,
        class_name=strategy_spec["class_name"],
        config_path=effective_config,
        data_directory=compact_data_directory,
        timerange=data_spec["timerange"],
        output_directory=engine_output,
        pairs=data_spec["pairs"],
        workers=workers,
        cache_directory=work / "vector-cache",
        profile_path=profile,
        resume=False,
        prepare_only=False,
        download_missing=False,
        market_metadata_path=engine_markets,
        registry_path=work / "runs.sqlite",
        download_market_metadata=False,
        recalibrate=True,
        history_coverage_policy="strict",
        trace_engine_events=True,
    )
    if not engine["complete"]:
        raise BenchmarkError(
            "native probe run did not complete; inspect work/engine/run.json"
        )
    _require_native_surface_coverage(
        spec["fixture"]["required_coverage"],
        read_json(engine_output / "trade-surface.json"),
    )

    capture_inputs = _capture_inputs_from_seal(
        engine_output / "data-seal.json",
        compact_data_directory,
    )
    capture_inputs.extend(
        [
            ("market_metadata", engine_markets),
            ("reference_market_metadata", reference_markets),
        ]
    )
    stage = stage_fixture_v3(
        output,
        fixture_id=spec["fixture"]["id"],
        description=spec["fixture"]["description"],
        probe_kind=spec["fixture"]["probe_kind"],
        strategy_provenance=provenance,
        required_coverage=spec["fixture"]["required_coverage"],
        strategy=effective_strategy,
        config=effective_config,
        inputs=capture_inputs,
    )
    config = effective_config_document
    trading_mode = str(config.get("trading_mode", "spot"))
    trace_identity = {
        "run_id": stage["fixture_id"],
        "input_sha256": stage["input_sha256"],
        "strategy_sha256": stage["strategy_sha256"],
        "profile_sha256": stage["profile_sha256"],
        "trading_mode": trading_mode,
    }
    reference_output = work / "reference"
    reference = run_research_reference(
        engine_output,
        reference_output,
        market_snapshot_path=reference_markets,
        capture_markets=False,
        audit_timestamps_ms=execution_spec["audit_timestamps_ms"],
        timeout_seconds=timeout_seconds,
        trace_identity=trace_identity,
    )
    if not reference["complete"]:
        raise BenchmarkError(
            "official probe run did not complete exact parity; "
            "inspect work/reference/run.json"
        )
    _verify_reference_materialization(
        output,
        reference_output,
        stage,
    )
    result = reference["result"]
    official_surface = reference["official_trade_surface"]
    state_trace = reference["state_trace"]
    if not all(
        isinstance(record, dict) and isinstance(record.get("path"), str)
        for record in (result, official_surface, state_trace)
    ):
        raise BenchmarkError("official probe artifacts are incomplete")

    fixture = finalize_fixture_v3(
        output,
        freqtrade_result=result["path"],
        trade_surface=official_surface["path"],
        state_trace=state_trace["path"],
        freqtrade=_freqtrade_record(
            spec,
            config,
            selected_strategy,
        ),
    )
    return {
        "schema_version": "1.0.0",
        "complete": True,
        "fixture_id": fixture["fixture_id"],
        "manifest_path": str(output / "manifest.json"),
        "manifest_sha256": sha256_file(output / "manifest.json"),
        "engine_report": str(engine_output / "run.json"),
        "reference_report": str(reference_output / "run.json"),
    }


def _load_probe_spec(path: Path) -> dict[str, Any]:
    document = read_json(path)
    required = {
        "schema_version",
        "fixture",
        "upstream",
        "strategy",
        "config",
        "data",
        "markets",
        "execution",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise SpecValidationError("probe spec fields differ from the v1 contract")
    if document["schema_version"] != PROBE_SPEC_VERSION:
        raise SpecValidationError("unsupported probe spec version")
    _require_fields(
        document["fixture"],
        {"id", "description", "probe_kind", "required_coverage"},
        "fixture",
    )
    _require_fields(document["upstream"], {"repository", "commit"}, "upstream")
    strategy = document["strategy"]
    if not isinstance(strategy, dict):
        raise SpecValidationError("probe spec strategy must be an object")
    required_strategy = {"source", "class_name"}
    optional_strategy = {"boolean_toggles", "protections"}
    if not required_strategy <= set(strategy) or set(strategy) - (
        required_strategy | optional_strategy
    ):
        raise SpecValidationError("probe spec strategy fields are invalid")
    _validate_boolean_toggle_specs(strategy.get("boolean_toggles"))
    protections = strategy.get("protections")
    if protections is not None and (
        not isinstance(protections, list)
        or len(protections) != 1
        or not isinstance(protections[0], dict)
    ):
        raise SpecValidationError(
            "probe strategy protections must contain exactly one object"
        )
    _require_fields(
        document["config"],
        {"source", "overrides", "remove_paths"},
        "config",
    )
    _require_fields(
        document["data"],
        {"directory", "timerange", "pairs"},
        "data",
    )
    _require_fields(document["markets"], {"engine", "reference"}, "markets")
    _require_fields(
        document["execution"],
        {"profile", "audit_timestamps_ms"},
        "execution",
    )
    pairs = document["data"]["pairs"]
    if (
        not isinstance(pairs, list)
        or not pairs
        or len(pairs) != len(set(pairs))
        or not all(isinstance(pair, str) and "/" in pair for pair in pairs)
    ):
        raise SpecValidationError("probe spec pairs must be unique CCXT pair strings")
    overrides = document["config"]["overrides"]
    if not isinstance(overrides, dict):
        raise SpecValidationError("probe config overrides must be an object")
    remove_paths = document["config"]["remove_paths"]
    if (
        not isinstance(remove_paths, list)
        or not all(isinstance(path, str) and path for path in remove_paths)
        or len(remove_paths) != len(set(remove_paths))
    ):
        raise SpecValidationError("probe config remove_paths must be unique strings")
    timestamps = document["execution"]["audit_timestamps_ms"]
    if (
        not isinstance(timestamps, list)
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for value in timestamps
        )
    ):
        raise SpecValidationError(
            "probe audit timestamps must be non-negative integers"
        )
    return document


def _validate_boolean_toggle_specs(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise SpecValidationError("probe strategy boolean_toggles must be an array")
    for index, toggle in enumerate(value):
        required = {"mapping", "key", "expected", "replacement"}
        if not isinstance(toggle, dict) or set(toggle) != required:
            raise SpecValidationError(
                f"probe boolean toggle {index} fields are invalid"
            )
        if (
            not isinstance(toggle["mapping"], str)
            or not toggle["mapping"]
            or not isinstance(toggle["key"], str)
            or not toggle["key"]
            or not isinstance(toggle["expected"], bool)
            or not isinstance(toggle["replacement"], bool)
            or toggle["expected"] is toggle["replacement"]
        ):
            raise SpecValidationError(
                f"probe boolean toggle {index} values are invalid"
            )


def _capture_inputs_from_seal(
    seal_path: Path,
    data_directory: Path,
) -> list[CaptureInput]:
    seal = read_json(seal_path)
    files = seal.get("files") if isinstance(seal, dict) else None
    if not isinstance(files, list) or not files:
        raise BenchmarkError("probe data seal contains no files")
    inputs: list[CaptureInput] = []
    for record in files:
        relative = record.get("path") if isinstance(record, dict) else None
        if not isinstance(relative, str):
            raise BenchmarkError("probe data seal file path is invalid")
        source = (data_directory / relative).resolve()
        if not source.is_relative_to(data_directory) or not source.is_file():
            raise BenchmarkError(f"probe data seal file is missing: {relative}")
        inputs.append(
            (
                _candle_role(source),
                source,
                f"inputs/data/{Path(relative).as_posix()}",
            )
        )
    return inputs


def _candle_role(path: Path) -> str:
    name = path.stem.lower()
    if "funding_rate" in name:
        return "funding_candles"
    if "-mark" in name:
        return "mark_candles"
    return "candles"


def _require_native_surface_coverage(
    required: dict[str, Any],
    surface: dict[str, Any],
) -> None:
    """Fail before the official run when trade-visible branches were not reached.

    Callbacks and pair locks are official-runtime observations, so they remain
    finalization checks. Entry tags, exits, sides, and leverage are already
    present in the native trade surface and can safely reject an ineffective
    probe before paying for a second backtest.
    """
    trades = surface.get("trades") if isinstance(surface, dict) else None
    if not isinstance(trades, list):
        raise BenchmarkError("native probe trade surface has no trades array")
    complete_tags = {
        tag.strip()
        for trade in trades
        if isinstance(trade, dict)
        and isinstance((tag := trade.get("entry_tag")), str)
        and tag.strip()
    }
    observed = {
        "entry_tags": {
            token
            for tag in complete_tags
            for token in tag.split()
            if token
        },
        "compound_tags": {tag for tag in complete_tags if len(tag.split()) > 1},
        "exit_reasons": {
            reason
            for trade in trades
            if isinstance(trade, dict)
            and isinstance((reason := trade.get("exit_reason")), str)
            and reason
        },
        "sides": {
            side
            for trade in trades
            if isinstance(trade, dict)
            and isinstance((side := trade.get("direction")), str)
            and side
        },
        "leverages": {
            leverage
            for trade in trades
            if isinstance(trade, dict)
            and isinstance((leverage := trade.get("leverage")), str)
            and leverage
        },
    }
    missing: list[str] = []
    for field in ("entry_tags", "compound_tags", "exit_reasons", "sides"):
        values = required.get(field)
        if not isinstance(values, list):
            raise SpecValidationError(
                f"probe required_coverage.{field} must be an array"
            )
        absent = sorted(set(values) - observed[field])
        missing.extend(f"{field}:{value}" for value in absent)
    minimum = required.get("minimum_distinct_leverages")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
        raise SpecValidationError(
            "probe required_coverage.minimum_distinct_leverages "
            "must be a non-negative integer"
        )
    if len(observed["leverages"]) < minimum:
        missing.append(
            "distinct_leverages:"
            f"{len(observed['leverages'])}<{minimum}"
        )
    if missing:
        raise BenchmarkError(
            "native probe did not reach trade-visible required coverage: "
            + ", ".join(missing)
        )


def _verify_reference_materialization(
    fixture_root: Path,
    reference_output: Path,
    stage: dict[str, Any],
) -> None:
    expected_strategy = next(
        item for item in stage["inputs"] if item["role"] == "strategy"
    )
    expected_config = next(
        item for item in stage["inputs"] if item["role"] == "config"
    )
    checks = (
        (reference_output / "inputs" / "strategy.py", expected_strategy),
        (reference_output / "inputs" / "config.json", expected_config),
    )
    for path, expected in checks:
        if not path.is_file() or sha256_file(path) != expected["sha256"]:
            raise BenchmarkError(
                "official materialized input differs from staged fixture: "
                f"{path.relative_to(reference_output)}"
            )
    if not (fixture_root / expected_strategy["path"]).is_file():
        raise BenchmarkError("staged probe strategy disappeared before finalization")


def _freqtrade_record(
    spec: dict[str, Any],
    config: dict[str, Any],
    strategy: dict[str, Any],
) -> dict[str, Any]:
    strategy_name = spec["strategy"]["class_name"]
    timerange = spec["data"]["timerange"]
    pairs = spec["data"]["pairs"]
    exchange = config.get("exchange")
    if not isinstance(exchange, dict) or not isinstance(exchange.get("name"), str):
        raise SpecValidationError("probe config exchange.name must be a string")
    timeframe = strategy["constants"].get("timeframe")
    if not isinstance(timeframe, str) or not timeframe:
        raise SpecValidationError("probe strategy has no static timeframe")
    trading_mode = str(config.get("trading_mode", "spot"))
    margin_mode = config.get("margin_mode")
    if margin_mode is not None and not isinstance(margin_mode, str):
        raise SpecValidationError("probe config margin_mode must be text or null")
    return {
        "version": REFERENCE_VERSION,
        "image": REFERENCE_IMAGE,
        "image_index_digest": REFERENCE_INDEX_DIGEST,
        "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
        "platform": REFERENCE_PLATFORM,
        "tracer_version": REFERENCE_TRACER_VERSION,
        "exchange": exchange["name"],
        "strategy": strategy_name,
        "timerange": timerange,
        "timeframe": timeframe,
        "timeframe_detail": config.get("timeframe_detail"),
        "trading_mode": trading_mode,
        "margin_mode": margin_mode,
        "command": [
            "freqtrade",
            "backtesting",
            "--config",
            "inputs/config.json",
            "--strategy-path",
            "inputs",
            "--strategy",
            strategy_name,
            "--datadir",
            "inputs/data",
            "--timerange",
            timerange,
            "--pairs",
            *pairs,
            "--cache",
            "none",
            "--export",
            "trades",
        ],
    }


def _require_fields(document: Any, fields: set[str], label: str) -> None:
    if not isinstance(document, dict) or set(document) != fields:
        raise SpecValidationError(f"probe spec {label} fields are invalid")


def _resolve_file(root: Path, value: Any, label: str) -> Path:
    path = _resolve_path(root, value, label)
    if not path.is_file():
        raise SpecValidationError(f"probe {label} does not exist: {path}")
    return path


def _resolve_directory(root: Path, value: Any, label: str) -> Path:
    path = _resolve_path(root, value, label)
    if not path.is_dir():
        raise SpecValidationError(f"probe {label} does not exist: {path}")
    return path


def _resolve_path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise SpecValidationError(f"probe {label} path must be non-empty")
    path = Path(value)
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def _require_empty_destination(path: Path, label: str) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise BenchmarkError(f"{label} directory must be empty: {path}")
