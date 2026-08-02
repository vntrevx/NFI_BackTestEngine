"""Compiled NFI X7 adapter for the Rust simulator input contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from ..canonical import read_json, write_json
from ..errors import StrategyAnalysisError
from ..market_precision import historic_price_steps
from ..vector_manifest import (
    VECTOR_MANIFEST_VERSION,
    artifact_execution_start_index,
    contained_vector_path,
    declared_vector_sha256,
    feather_column_names,
    require_columns,
)
from .contracts import (
    _market_maximum_leverage,
    _non_negative_float,
    _optional_non_negative_float,
    _positive_float,
    _x7_funding_fee_interval_ms,
    _x7_liquidation_contract,
    x7_adapter_blockers,
)
from .serialization import (
    _nfi_trade_manager_config,
    _required_trade_features,
    _x7_portfolio_config,
)
from .vectors import (
    _validate_nfi_frame_scope,
    _x7_feature_columns,
    _x7_signal_candles,
)

X7_ADAPTER_VERSION = "0.26.0"


def build_x7_simulation_input(
    *,
    analysis: dict[str, Any],
    hot_ir: dict[str, Any],
    config: dict[str, Any],
    vector_report: dict[str, Any],
    market_metadata_path: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    blockers = x7_adapter_blockers(
        analysis,
        hot_ir,
        config,
        market_metadata_path=market_metadata_path,
    )
    if blockers:
        raise StrategyAnalysisError(blockers[0]["message"])
    market_snapshot = read_json(market_metadata_path)
    markets = market_snapshot["markets"]
    required_features = _required_trade_features(hot_ir)
    nfi_manager = _nfi_trade_manager_config(hot_ir)
    can_short = config.get("trading_mode", "spot") == "futures"
    funding_fee_interval_ms = _x7_funding_fee_interval_ms(config, vector_report)
    pairs = []
    fee_rates = []
    maximum_leverage_by_pair: dict[str, float] = {}
    for artifact in vector_report["outputs"]:
        pair = artifact["pair"]
        market = markets[pair]
        precision = market["precision"]
        limits = market["limits"]
        fee = config.get("fee", market.get("taker"))
        fee_rates.append(_non_negative_float(fee, f"{pair} fee"))
        maximum_leverage = _market_maximum_leverage(market, pair)
        if maximum_leverage is not None:
            maximum_leverage_by_pair[pair] = maximum_leverage
        frame = pd.read_feather(artifact["path"])
        if can_short:
            require_columns(
                set(frame.columns),
                {"nfi_exec_funding_rate", "nfi_exec_funding_mark_price"},
                pair,
            )
        if nfi_manager is not None:
            _validate_nfi_frame_scope(frame, pair, nfi_manager, can_short=can_short)
        execution_start_index = artifact_execution_start_index(
            artifact,
            pair,
            len(frame),
        )
        pairs.append(
            {
                "pair": pair,
                "execution_start_index": execution_start_index,
                "amount_step": _positive_float(
                    precision.get("amount"),
                    f"{pair} amount precision",
                ),
                "price_step": _positive_float(
                    precision.get("price"),
                    f"{pair} price precision",
                ),
                "price_steps": historic_price_steps(frame),
                "minimum_stake": None,
                "minimum_amount": _optional_non_negative_float(
                    limits["amount"].get("min"),
                    f"{pair} minimum amount",
                ),
                "minimum_cost": _optional_non_negative_float(
                    limits["cost"].get("min"),
                    f"{pair} minimum cost",
                ),
                "feature_columns": _x7_feature_columns(
                    frame,
                    required_features,
                ),
                "candles": _x7_signal_candles(frame, can_short=can_short),
            }
        )
    if not pairs:
        raise StrategyAnalysisError("compiled X7 adapter requires vector outputs")
    if any(rate != fee_rates[0] for rate in fee_rates[1:]):
        raise StrategyAnalysisError(
            "compiled X7 adapter requires one exact fee across selected markets"
        )
    portfolio_config = _x7_portfolio_config(
        analysis=analysis,
        hot_ir=hot_ir,
        config=config,
        nfi_manager=nfi_manager,
        fee_rate=fee_rates[0],
        amount_step=pairs[0]["amount_step"],
        price_step=pairs[0]["price_step"],
        pair_count=len(pairs),
        maximum_leverage_by_pair=maximum_leverage_by_pair,
        funding_fee_interval_ms=funding_fee_interval_ms,
        liquidation_model=_x7_liquidation_contract(
            config,
            market_snapshot,
            [pair["pair"] for pair in pairs],
        ),
    )
    document = {
        "schema_version": "1.0.0",
        "config": portfolio_config,
        "pairs": pairs,
    }
    write_json(destination, document)
    return document


def build_x7_vector_manifest(
    *,
    analysis: dict[str, Any],
    hot_ir: dict[str, Any],
    config: dict[str, Any],
    vector_report: dict[str, Any],
    market_metadata_path: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    """Write the compact, SHA-bound Feather input used by release runs.

    Only OHLC columns needed for historical tick reconstruction and two signal
    columns needed for the NFI route gate cross Python. Rust reads the candles,
    tags, and 100+ callback features directly from the same sealed Feather
    file. This removes the old 300+ MB JSON copy without weakening the
    source-bound NFI entry-tag check.
    """
    blockers = x7_adapter_blockers(
        analysis,
        hot_ir,
        config,
        market_metadata_path=market_metadata_path,
    )
    if blockers:
        raise StrategyAnalysisError(blockers[0]["message"])
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    market_snapshot = read_json(market_metadata_path)
    markets = market_snapshot["markets"]
    required_features = _required_trade_features(hot_ir)
    nfi_manager = _nfi_trade_manager_config(hot_ir)
    can_short = config.get("trading_mode", "spot") == "futures"
    funding_fee_interval_ms = _x7_funding_fee_interval_ms(config, vector_report)
    pairs: list[dict[str, Any]] = []
    fee_rates: list[float] = []
    maximum_leverage_by_pair: dict[str, float] = {}

    for artifact in vector_report["outputs"]:
        pair = artifact["pair"]
        source = Path(artifact["path"]).resolve()
        vector_sha256 = declared_vector_sha256(source, artifact, pair)
        columns = feather_column_names(source, pair)
        required_columns = {
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "nfi_exec_enter_long",
            "nfi_exec_exit_long",
            "nfi_exec_enter_tag",
            *required_features,
        }
        if can_short:
            required_columns.update(
                {
                    "nfi_exec_enter_short",
                    "nfi_exec_exit_short",
                    "nfi_exec_funding_rate",
                    "nfi_exec_funding_mark_price",
                }
            )
        require_columns(columns, required_columns, pair)
        precision_columns = [
            "date",
            "open",
            "high",
            "low",
            "close",
            "nfi_exec_enter_long",
            "nfi_exec_enter_tag",
        ]
        if can_short:
            precision_columns.extend(
                [
                    "nfi_exec_enter_short",
                    "nfi_exec_exit_short",
                    "nfi_exec_funding_rate",
                    "nfi_exec_funding_mark_price",
                ]
            )
        precision_frame = pd.read_feather(
            source,
            columns=precision_columns,
        )
        if nfi_manager is not None:
            _validate_nfi_frame_scope(
                precision_frame,
                pair,
                nfi_manager,
                can_short=can_short,
            )
        execution_start_index = artifact_execution_start_index(
            artifact,
            pair,
            len(precision_frame),
        )
        market = markets[pair]
        precision = market["precision"]
        limits = market["limits"]
        fee = config.get("fee", market.get("taker"))
        fee_rates.append(_non_negative_float(fee, f"{pair} fee"))
        maximum_leverage = _market_maximum_leverage(market, pair)
        if maximum_leverage is not None:
            maximum_leverage_by_pair[pair] = maximum_leverage
        relative_path = contained_vector_path(source, target.parent, pair)
        pairs.append(
            {
                "pair": pair,
                "execution_start_index": execution_start_index,
                "amount_step": _positive_float(
                    precision.get("amount"),
                    f"{pair} amount precision",
                ),
                "price_step": _positive_float(
                    precision.get("price"),
                    f"{pair} price precision",
                ),
                "price_steps": historic_price_steps(precision_frame),
                "minimum_stake": None,
                "minimum_amount": _optional_non_negative_float(
                    limits["amount"].get("min"),
                    f"{pair} minimum amount",
                ),
                "minimum_cost": _optional_non_negative_float(
                    limits["cost"].get("min"),
                    f"{pair} minimum cost",
                ),
                "vector": {
                    "path": relative_path,
                    "sha256": vector_sha256,
                    "rows": len(precision_frame),
                    "format": "feather-ipc",
                },
                "feature_columns": required_features,
                "can_short": can_short,
                "include_funding": can_short,
                "use_exit_signal": True,
                # X7 confirm_trade_entry reads the final analyzed close. The
                # vector signal is shifted to the next open, so this must be
                # the preceding row rather than the execution row's close.
                "include_previous_close": True,
            }
        )
    if not pairs:
        raise StrategyAnalysisError("compiled X7 adapter requires vector outputs")
    if any(rate != fee_rates[0] for rate in fee_rates[1:]):
        raise StrategyAnalysisError(
            "compiled X7 adapter requires one exact fee across selected markets"
        )
    document = {
        "schema_version": VECTOR_MANIFEST_VERSION,
        "config": _x7_portfolio_config(
            analysis=analysis,
            hot_ir=hot_ir,
            config=config,
            nfi_manager=nfi_manager,
            fee_rate=fee_rates[0],
            amount_step=pairs[0]["amount_step"],
            price_step=pairs[0]["price_step"],
            pair_count=len(pairs),
            maximum_leverage_by_pair=maximum_leverage_by_pair,
            funding_fee_interval_ms=funding_fee_interval_ms,
            liquidation_model=_x7_liquidation_contract(
                config,
                market_snapshot,
                [pair["pair"] for pair in pairs],
            ),
        ),
        "pairs": pairs,
    }
    write_json(target, document)
    return document
