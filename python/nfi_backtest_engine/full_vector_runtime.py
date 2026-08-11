"""Build the compact, SHA-bound input for complete Rust-native execution."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from .canonical import read_json, write_json
from .errors import StrategyAnalysisError
from .fixture import sha256_file
from .indicator_program import compile_indicator_program
from .market_precision import historic_price_steps
from .signal_program import compile_signal_program
from .specs import FULL_NATIVE_VECTOR_MANIFEST_SCHEMA, validate_schema
from .tag_program import compile_tag_program
from .timerange import parse_timerange_milliseconds
from .vector_runtime import (
    PairFundingData,
    resolve_pair_frames,
    resolve_pair_funding_data,
)
from .x7.contracts import (
    _market_maximum_leverage,
    _non_negative_float,
    _optional_non_negative_float,
    _positive_float,
    _x7_funding_fee_interval_ms,
    _x7_liquidation_contract,
    x7_adapter_blockers,
)
from .x7.serialization import (
    _nfi_trade_manager_config,
    _required_trade_features,
    _x7_portfolio_config,
)

FULL_NATIVE_VECTOR_MANIFEST_VERSION = "full-native-vector-manifest-v1"
FULL_NATIVE_VECTOR_RUNTIME_VERSION = "1.0.0"
_PROGRAM_NAMES = ("indicator", "signal", "tag")


def build_full_native_vector_manifest(
    *,
    strategy_path: str | Path,
    class_name: str,
    analysis: dict[str, Any],
    hot_ir: dict[str, Any],
    config: dict[str, Any],
    pairs: list[str],
    data_directory: str | Path,
    timerange: str,
    market_metadata_path: str | Path,
    destination: str | Path,
    compiled_programs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compile strategy programs and seal raw inputs without copying candle bytes."""
    target = Path(destination).resolve()
    if target.exists():
        raise StrategyAnalysisError(f"full native manifest already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    artifact_root = target.parent / f"{target.stem}.artifacts"
    if artifact_root.exists():
        raise StrategyAnalysisError(
            f"full native artifact directory already exists: {artifact_root}"
        )
    if not pairs or len(set(pairs)) != len(pairs) or any(not pair for pair in pairs):
        raise StrategyAnalysisError("full native pairs must be non-empty and unique")

    blockers = x7_adapter_blockers(
        analysis,
        hot_ir,
        config,
        market_metadata_path=market_metadata_path,
    )
    if blockers:
        raise StrategyAnalysisError(blockers[0]["message"])

    source = Path(strategy_path).resolve()
    trading_mode = str(config.get("trading_mode", "spot"))
    programs = (
        compiled_programs
        if compiled_programs is not None
        else compile_full_native_programs(
            source,
            class_name=class_name,
            config=config,
        )
    )
    if set(programs) != set(_PROGRAM_NAMES):
        raise StrategyAnalysisError("compiled full native program set is incomplete")
    indicator = programs["indicator"]
    _validate_program_identity(programs, class_name=class_name, trading_mode=trading_mode)

    strategy = analysis["strategies"][0]
    constants = strategy["constants"]
    base_timeframe = constants.get("timeframe")
    if not isinstance(base_timeframe, str) or not base_timeframe:
        raise StrategyAnalysisError("strategy base timeframe is not a literal string")
    raw_startup = constants.get("startup_candle_count", 0)
    if isinstance(raw_startup, bool) or not isinstance(raw_startup, int) or raw_startup < 0:
        raise StrategyAnalysisError("strategy startup_candle_count must be non-negative")
    start_ms, stop_ms = parse_timerange_milliseconds(timerange)

    market_snapshot = read_json(market_metadata_path)
    if not isinstance(market_snapshot, dict) or not isinstance(
        market_snapshot.get("markets"), dict
    ):
        raise StrategyAnalysisError("market snapshot does not contain a markets object")
    markets = market_snapshot["markets"]
    required_timeframes = strategy.get("required_timeframes")
    if not isinstance(required_timeframes, list) or not all(
        isinstance(timeframe, str) and timeframe for timeframe in required_timeframes
    ):
        raise StrategyAnalysisError("strategy required timeframes are invalid")

    data_root = Path(data_directory).resolve()
    data_index: dict[str, list[Path]] = {}
    frame_sources: dict[tuple[str, str], Path] = {}
    funding_sources: dict[str, PairFundingData] = {}
    for pair in pairs:
        resolved = resolve_pair_frames(
            data_root,
            pair=pair,
            pairs=pairs,
            timeframes=required_timeframes,
            config=config,
            data_index=data_index,
        )
        for identity, path in resolved.items():
            frame_pair, separator, timeframe = identity.rpartition("|")
            if not separator or not frame_pair or not timeframe:
                raise StrategyAnalysisError(f"invalid resolved frame identity: {identity}")
            key = (frame_pair, timeframe)
            prior = frame_sources.setdefault(key, path.resolve())
            if prior != path.resolve():
                raise StrategyAnalysisError(f"resolved frame identity has two files: {identity}")
        if trading_mode == "futures":
            funding_sources[pair] = resolve_pair_funding_data(
                data_root,
                pair=pair,
                data_index=data_index,
            )

    futures_execution = _shared_futures_execution(funding_sources)
    funding_fee_interval_ms = _x7_funding_fee_interval_ms(
        config,
        {"futures_execution": futures_execution},
    )
    pair_documents, portfolio_config = _portfolio_contract(
        analysis=analysis,
        hot_ir=hot_ir,
        config=config,
        pairs=pairs,
        markets=markets,
        frame_sources=frame_sources,
        base_timeframe=base_timeframe,
        funding_fee_interval_ms=funding_fee_interval_ms,
        market_snapshot=market_snapshot,
    )
    retained_features = _required_trade_features(hot_ir)
    retained_fingerprint = _retained_feature_fingerprint(retained_features)

    staging = Path(
        tempfile.mkdtemp(prefix=f".{artifact_root.name}.", dir=target.parent)
    ).resolve()
    installed = False
    try:
        program_documents = _write_program_artifacts(
            programs,
            staging=staging,
            artifact_root_name=artifact_root.name,
        )
        frame_documents = _seal_frame_sources(
            frame_sources,
            staging=staging,
            artifact_root_name=artifact_root.name,
        )
        futures_documents = _seal_futures_sources(
            funding_sources,
            staging=staging,
            artifact_root_name=artifact_root.name,
        )
        document = {
            "schema_version": FULL_NATIVE_VECTOR_MANIFEST_VERSION,
            "source": {
                "strategy_sha256": indicator["source"]["sha256"],
                "config_sha256": _config_identity_sha256(portfolio_config),
                "compiler_source_fingerprint": _compiler_source_fingerprint(),
                "selected_class": class_name,
            },
            "config": portfolio_config,
            "compile_context": {"run_mode": "backtest", "trading_mode": trading_mode},
            "programs": program_documents,
            "run": {
                "trading_mode": trading_mode,
                "timerange": {"start_ms": start_ms, "stop_ms": stop_ms},
                "startup_candles": raw_startup,
                "base_timeframe": base_timeframe,
                "source_row_shift": 1,
            },
            "retained_features": {
                "columns": retained_features,
                "fingerprint": retained_fingerprint,
            },
            "pairs": pair_documents,
            "frames": frame_documents,
            "futures": futures_documents or None,
        }
        validate_schema(document, FULL_NATIVE_VECTOR_MANIFEST_SCHEMA)
        _canonical_json_bytes(document)
        os.replace(staging, artifact_root)
        installed = True
        temporary_manifest = target.with_name(f".{target.name}.tmp")
        try:
            write_json(temporary_manifest, document)
            os.replace(temporary_manifest, target)
        except Exception:
            temporary_manifest.unlink(missing_ok=True)
            shutil.rmtree(artifact_root, ignore_errors=True)
            raise
        return document
    finally:
        if not installed:
            shutil.rmtree(staging, ignore_errors=True)


def compile_full_native_programs(
    strategy_path: str | Path,
    *,
    class_name: str,
    config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Compile the three independent programs once for policy and manifest stages."""
    source = Path(strategy_path).resolve()
    trading_mode = str(config.get("trading_mode", "spot"))
    programs = {
        "indicator": compile_indicator_program(source, class_name=class_name, config=config),
        "signal": compile_signal_program(
            source,
            class_name=class_name,
            trading_mode=trading_mode,
            config=config,
        ),
        "tag": compile_tag_program(
            source,
            class_name=class_name,
            trading_mode=trading_mode,
            config=config,
        ),
    }
    _validate_program_identity(programs, class_name=class_name, trading_mode=trading_mode)
    return programs


def _validate_program_identity(
    programs: dict[str, dict[str, Any]],
    *,
    class_name: str,
    trading_mode: str,
) -> None:
    identities = {
        (program["source"]["sha256"], program["selected_class"])
        for program in programs.values()
    }
    if identities != {(next(iter(programs.values()))["source"]["sha256"], class_name)}:
        raise StrategyAnalysisError("compiled program source or selected class differs")
    for name in ("signal", "tag"):
        context = programs[name].get("compile_context")
        if context != {"run_mode": "backtest", "trading_mode": trading_mode}:
            raise StrategyAnalysisError(f"compiled {name} context differs from the run")


def _portfolio_contract(
    *,
    analysis: dict[str, Any],
    hot_ir: dict[str, Any],
    config: dict[str, Any],
    pairs: list[str],
    markets: dict[str, Any],
    frame_sources: dict[tuple[str, str], Path],
    base_timeframe: str,
    funding_fee_interval_ms: int | None,
    market_snapshot: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    can_short = config.get("trading_mode", "spot") == "futures"
    pair_documents: list[dict[str, Any]] = []
    fee_rates: list[float] = []
    maximum_leverage_by_pair: dict[str, float] = {}
    for pair in pairs:
        market = markets.get(pair)
        if not isinstance(market, dict):
            raise StrategyAnalysisError(f"market snapshot is missing {pair}")
        precision = market.get("precision")
        limits = market.get("limits")
        if not isinstance(precision, dict) or not isinstance(limits, dict):
            raise StrategyAnalysisError(f"market precision or limits are missing for {pair}")
        amount_limits = limits.get("amount")
        cost_limits = limits.get("cost")
        if not isinstance(amount_limits, dict) or not isinstance(cost_limits, dict):
            raise StrategyAnalysisError(f"market amount or cost limits are missing for {pair}")
        amount_step = _positive_float(precision.get("amount"), f"{pair} amount precision")
        price_step = _positive_float(precision.get("price"), f"{pair} price precision")
        fee_rates.append(_non_negative_float(config.get("fee", market.get("taker")), f"{pair} fee"))
        maximum_leverage = _market_maximum_leverage(market, pair)
        if maximum_leverage is not None:
            maximum_leverage_by_pair[pair] = maximum_leverage
        base_path = frame_sources.get((pair, base_timeframe))
        if base_path is None:
            raise StrategyAnalysisError(f"resolved frames are missing {pair} {base_timeframe}")
        precision_frame = pd.read_feather(
            base_path,
            columns=["date", "open", "high", "low", "close"],
        )
        pair_documents.append(
            {
                "identity": {"pair": pair, "timeframe": base_timeframe},
                "metadata": {"pair": pair},
                "precision": {"amount_step": amount_step, "price_step": price_step},
                "limits": {
                    "minimum_stake": None,
                    "minimum_amount": _optional_non_negative_float(
                        amount_limits.get("min"), f"{pair} minimum amount"
                    ),
                    "minimum_cost": _optional_non_negative_float(
                        cost_limits.get("min"), f"{pair} minimum cost"
                    ),
                },
                "price_steps": historic_price_steps(precision_frame),
                "options": {
                    "can_short": can_short,
                    "include_funding": can_short,
                    "use_exit_signal": True,
                    "include_previous_close": True,
                },
            }
        )
    if any(rate != fee_rates[0] for rate in fee_rates[1:]):
        raise StrategyAnalysisError("full native execution requires one exact fee")
    nfi_manager = _nfi_trade_manager_config(hot_ir)
    portfolio_config = _x7_portfolio_config(
        analysis=analysis,
        hot_ir=hot_ir,
        config=config,
        nfi_manager=nfi_manager,
        fee_rate=fee_rates[0],
        amount_step=pair_documents[0]["precision"]["amount_step"],
        price_step=pair_documents[0]["precision"]["price_step"],
        pair_count=len(pair_documents),
        maximum_leverage_by_pair=maximum_leverage_by_pair,
        funding_fee_interval_ms=funding_fee_interval_ms,
        liquidation_model=_x7_liquidation_contract(config, market_snapshot, pairs),
    )
    return pair_documents, portfolio_config


def _shared_futures_execution(
    sources: dict[str, PairFundingData],
) -> dict[str, Any] | None:
    shared: dict[str, Any] | None = None
    for pair, source in sources.items():
        contract = source.execution_contract
        if shared is None:
            shared = contract
        elif contract != shared:
            raise StrategyAnalysisError(
                f"selected Futures pairs use different funding execution contracts: {pair}"
            )
    return shared


def _write_program_artifacts(
    programs: dict[str, dict[str, Any]],
    *,
    staging: Path,
    artifact_root_name: str,
) -> dict[str, Any]:
    directory = staging / "programs"
    directory.mkdir(parents=True)
    result: dict[str, Any] = {}
    for name in _PROGRAM_NAMES:
        path = directory / f"{name}.json"
        write_json(path, programs[name])
        result[name] = {
            "artifact": {
                "path": f"{artifact_root_name}/programs/{name}.json",
                "sha256": sha256_file(path),
            },
            "fingerprint": programs[name]["fingerprint"],
        }
    return result


def _seal_frame_sources(
    sources: dict[tuple[str, str], Path],
    *,
    staging: Path,
    artifact_root_name: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, ((pair, timeframe), source) in enumerate(sorted(sources.items())):
        relative = f"frames/frame-{index:04d}.feather"
        linked = _hardlink(source, staging / relative)
        result.append(
            {
                "identity": {"pair": pair, "timeframe": timeframe},
                "rows": _feather_rows(linked),
                "artifact": {
                    "path": f"{artifact_root_name}/{relative}",
                    "sha256": sha256_file(linked),
                },
            }
        )
    return result


def _seal_futures_sources(
    sources: dict[str, PairFundingData],
    *,
    staging: Path,
    artifact_root_name: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, (pair, source) in enumerate(sources.items()):
        roles = {
            "funding_rate": (source.funding_rate_path, source.funding_fee_timeframe),
            "mark": (source.mark_path, source.mark_timeframe),
        }
        document: dict[str, Any] = {"pair": pair}
        for role, (path, timeframe) in roles.items():
            relative = f"futures/futures-{index:04d}-{role}.feather"
            linked = _hardlink(path, staging / relative)
            document[role] = {
                "identity": {"pair": pair, "timeframe": timeframe},
                "rows": _feather_rows(linked),
                "artifact": {
                    "path": f"{artifact_root_name}/{relative}",
                    "sha256": sha256_file(linked),
                },
            }
        result.append(document)
    return result


def _hardlink(source: Path, destination: Path) -> Path:
    resolved = source.resolve()
    if resolved.suffix.lower() != ".feather" or not resolved.is_file():
        raise StrategyAnalysisError(f"full native input must be a Feather file: {resolved}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(resolved, destination)
    except OSError as exc:
        raise StrategyAnalysisError(
            "cannot hard-link full native candle input; keep the run output on the same "
            f"filesystem as the data directory: {resolved}"
        ) from exc
    if not os.path.samefile(resolved, destination):  # pragma: no cover - OS contract
        raise StrategyAnalysisError(f"hard-linked artifact identity differs: {resolved}")
    return destination


def _feather_rows(path: Path) -> int:
    frame = pd.read_feather(path, columns=["date"])
    return len(frame)


def _retained_feature_fingerprint(columns: list[str]) -> str:
    digest = hashlib.sha256(b"full-native-retained-features-v1\0")
    for column in columns:
        encoded = column.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _compiler_source_fingerprint() -> str:
    package = Path(__file__).resolve().parent
    paths = [
        package / "_indicator_ast.py",
        package / "_indicator_contract.py",
        package / "indicator_program.py",
        *(package / "signal_program").glob("*.py"),
        *(package / "tag_program").glob("*.py"),
    ]
    digest = hashlib.sha256(b"full-native-python-compilers-v1\0")
    for path in sorted(paths, key=lambda item: item.relative_to(package).as_posix()):
        relative = path.relative_to(package).as_posix().encode()
        contents = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _config_identity_sha256(value: Any) -> str:
    """Hash JSON data without depending on a language's float formatter."""
    digest = hashlib.sha256()

    def update(item: Any) -> None:
        if item is None:
            digest.update(b"N")
        elif isinstance(item, bool):
            digest.update(b"B\x01" if item else b"B\x00")
        elif isinstance(item, int):
            encoded = str(item).encode("ascii")
            digest.update(b"I")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise StrategyAnalysisError("full native config contains a non-finite number")
            digest.update(b"F")
            digest.update(struct.pack(">d", item))
        elif isinstance(item, str):
            encoded = item.encode("utf-8")
            digest.update(b"S")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        elif isinstance(item, list):
            digest.update(b"L")
            digest.update(len(item).to_bytes(8, "big"))
            for value_item in item:
                update(value_item)
        elif isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise StrategyAnalysisError("full native config keys must be strings")
            keys = sorted(item)
            digest.update(b"O")
            digest.update(len(keys).to_bytes(8, "big"))
            for key in keys:
                update(key)
                update(item[key])
        else:
            raise StrategyAnalysisError(
                f"full native config contains unsupported {type(item).__name__} data"
            )

    update(value)
    return digest.hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
