"""Closed Todo 16 execution-proof obligations bound to source and contract identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import SpecValidationError
from .semantic_registry_callback_data import (
    FREQTRADE_COMMIT,
    FREQTRADE_METHOD_MERKLE,
    SOURCE_METHODS,
)

_OWNER = "freqtrade.optimize.backtesting.Backtesting"
_FIELDS = frozenset(
    {
        "dimension",
        "rule",
        "source_owner",
        "source_method",
        "source_sha256",
        "boundary_row",
        "fixture_requirement",
        "contract_sha256",
    }
)
_DIMENSIONS = (
    ("event-order", "backtest_loop", "ordered execution events are exact"),
    ("event-phase", "backtest_loop", "entry exit and adjustment phases are exact"),
    ("event-duplicate-omission", "backtest_loop", "no execution event is duplicated or omitted"),
    ("candidate-order", "_check_trade_exit", "candidate evaluation order is exact"),
    ("candidate-list", "_check_trade_exit", "candidate list is exact and unsorted"),
    ("candidate-winner", "_check_trade_exit", "candidate winner is exact"),
    (
        "candidate-attempt-confirmation",
        "_check_trade_exit",
        "ordered candidate confirmation attempts are exact",
    ),
    (
        "candidate-rejection-continue",
        "_check_trade_exit",
        "rejected candidate continuation to the next candidate is exact",
    ),
    (
        "candidate-rejection-stop",
        "_check_trade_exit",
        "terminal rejection and all-rejected stop is exact",
    ),
    (
        "candidate-all-rejected",
        "_check_trade_exit",
        "all-rejected outcome and absent winner are exact",
    ),
    ("candle-ohlc", "backtest_loop", "candle open high low close inputs are exact"),
    ("ambiguity", "_check_trade_exit", "candle ambiguity resolution is exact"),
    ("fill-entry", "_enter_trade", "entry fill boundary order type and rate are exact"),
    ("fill-exit", "_exit_trade", "exit fill boundary order type and rate are exact"),
    ("fill-order-type", "_enter_trade", "market and limit order type paths are exact"),
    (
        "limit-requested-rate",
        "_enter_trade",
        "requested limit rate is exact",
    ),
    (
        "limit-adjusted-rate",
        "_enter_trade",
        "adjusted limit rate is exact",
    ),
    (
        "limit-candle-cross",
        "_enter_trade",
        "candle crossing and fill predicate are exact",
    ),
    ("limit-timeout", "_enter_trade", "limit timeout is exact"),
    ("limit-unfilled", "_enter_trade", "unfilled limit state is exact"),
    ("limit-retry", "_enter_trade", "limit retry path is exact"),
    (
        "fill-adjustment",
        "_check_adjust_trade_for_candle",
        "adjustment fill boundary order type and rate are exact",
    ),
    ("precision-amount-input", "_enter_trade", "amount input is exact rational text"),
    ("precision-amount-step", "_enter_trade", "amount step is exact"),
    ("precision-amount-frozen-step", "_enter_trade", "amount frozen step is exact"),
    (
        "precision-amount-round",
        "_enter_trade",
        "amount direction ties and rounded output are exact",
    ),
    ("precision-price-input", "_exit_trade", "price input is exact rational text"),
    ("precision-price-step", "_exit_trade", "price step is exact"),
    ("precision-price-frozen-step", "_exit_trade", "price frozen step is exact"),
    ("precision-price-round", "_exit_trade", "price direction ties and rounded output are exact"),
    ("min-stake-stage", "_enter_trade", "minimum stake validation stage is exact"),
    ("min-stake-result", "_enter_trade", "minimum stake result is exact"),
    ("fee-open-rate", "_enter_trade", "open fee rate is exact"),
    ("fee-close-rate", "_exit_trade", "close fee rate is exact"),
    ("fee-per-fill-rate", "_check_adjust_trade_for_candle", "per fill fee rate is exact"),
    (
        "fee-per-fill-count",
        "_check_adjust_trade_for_candle",
        "per fill fee count and order are exact",
    ),
    ("stake-intermediate", "_enter_trade", "stake intermediate is exact"),
    ("basis-intermediate", "_exit_trade", "cost basis intermediate is exact"),
    ("profit-intermediate", "_exit_trade", "profit intermediate is exact"),
    ("partial-exit-amount", "_check_adjust_trade_for_candle", "partial exit amount is exact"),
    ("trade-id", "_enter_trade", "trade identifier allocation is exact"),
    ("order-id", "_enter_trade", "order identifier allocation is exact"),
    ("rejection-reason", "_enter_trade", "rejection reason and rejected state are exact"),
    (
        "visibility-before-after",
        "backtest_loop",
        "before and after wallet trade order state is exact",
    ),
    (
        "identity-source-contract",
        "backtest_one_strategy",
        "source and contract identities are exact",
    ),
    ("identity-input-artifact", "backtest_one_strategy", "input artifact hash and path are exact"),
    ("identity-binary-path", "backtest_one_strategy", "native binary hash and path are exact"),
)


def execution_semantic_obligation_rows(
    freqtrade: Mapping[str, Any], execution_contract: Mapping[str, Any]
) -> tuple[dict[str, str], ...]:
    """Return the exhaustive source- and execution-contract-bound Todo 16 matrix."""
    _validate_identity(freqtrade, execution_contract)
    rows = tuple(
        _row(dimension, method, rule, str(execution_contract["fingerprint"]))
        for dimension, method, rule in _DIMENSIONS
    )
    validate_execution_semantic_obligation_rows(rows, freqtrade, execution_contract)
    return rows


def validate_execution_semantic_obligation_rows(
    rows: Sequence[Mapping[str, Any]],
    freqtrade: Mapping[str, Any],
    execution_contract: Mapping[str, Any],
) -> None:
    """Reject missing, changed, duplicate, reordered, or unbound execution obligations."""
    _validate_identity(freqtrade, execution_contract)
    normalized = [dict(row) for row in rows]
    if any(set(row) != _FIELDS for row in normalized):
        raise SpecValidationError("EXECUTION_SEMANTIC_REGISTRY: row fields differ")
    boundaries = [row["boundary_row"] for row in normalized]
    if len(boundaries) != len(set(boundaries)):
        raise SpecValidationError("EXECUTION_SEMANTIC_REGISTRY: duplicate boundary row")
    expected = [
        _row(dimension, method, rule, str(execution_contract["fingerprint"]))
        for dimension, method, rule in _DIMENSIONS
    ]
    if normalized != expected:
        raise SpecValidationError(
            "EXECUTION_SEMANTIC_REGISTRY: obligations differ from canonical matrix"
        )


def execution_registry_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    """Return a stable additive migration fingerprint for the execution matrix."""
    return hashlib.sha256(
        json.dumps(list(rows), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _row(dimension: str, method: str, rule: str, contract_sha: str) -> dict[str, str]:
    return {
        "dimension": dimension,
        "rule": rule,
        "source_owner": _OWNER,
        "source_method": method,
        "source_sha256": SOURCE_METHODS[(_OWNER, method)],
        "boundary_row": f"ET-{dimension}",
        "fixture_requirement": f"official-execution-trace:ET-{dimension}",
        "contract_sha256": contract_sha,
    }


def _validate_identity(freqtrade: Mapping[str, Any], execution_contract: Mapping[str, Any]) -> None:
    source, fingerprint = freqtrade.get("source"), execution_contract.get("fingerprint")
    if (
        not isinstance(source, Mapping)
        or source.get("commit") != FREQTRADE_COMMIT
        or source.get("observed_method_count") != 15
        or source.get("observed_method_merkle_root") != FREQTRADE_METHOD_MERKLE
    ):
        raise SpecValidationError("EXECUTION_SEMANTIC_REGISTRY: Freqtrade source identity differs")
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in fingerprint)
    ):
        raise SpecValidationError(
            "EXECUTION_SEMANTIC_REGISTRY: execution contract identity differs"
        )
