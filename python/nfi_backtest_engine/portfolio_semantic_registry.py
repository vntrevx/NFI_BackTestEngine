"""Closed Todo 15 portfolio proof obligations bound to authenticated contracts."""

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

_DIMENSIONS = (
    ("timestamp-batch", "backtest_loop", "exact ascending timestamp batches"),
    ("configured-order-2", "backtest_loop", "both two-pair configured-order permutations"),
    ("configured-order-3", "backtest_loop", "all three-pair configured-order permutations"),
    ("open-trade-first", "backtest_loop", "open trades precede remaining configured pairs"),
    ("pair-once", "backtest_loop", "each configured pair is processed once per timestamp"),
    ("wallet-free", "_enter_trade", "wallet free balance is exact after every event"),
    ("wallet-tied", "_enter_trade", "wallet tied stake is exact after every event"),
    ("wallet-realized", "_exit_trade", "wallet realized profit is exact after every event"),
    ("slot-occupancy", "_enter_trade", "slot occupancy and configured limit are exact"),
    ("rejected-stake", "_enter_trade", "rejected stake retains its exact rejection reason"),
    ("partial-exit-release", "_check_adjust_trade_for_candle", "partial exit release is exact"),
    ("compounding-base", "_enter_trade", "compounding base is exact"),
    ("trade-id", "_enter_trade", "trade identifier allocation is exact"),
    ("order-id", "_enter_trade", "order identifier allocation is exact"),
    ("final-trades", "_exit_trade", "final trades preserve closure and order identities"),
    ("artifact-identity", "backtest_one_strategy", "fixture artifact identity is exact"),
    ("source-hash", "backtest_one_strategy", "authenticated observer source hash is exact"),
    ("contract-hash", "backtest_one_strategy", "scheduler contract hash is exact"),
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


def portfolio_semantic_obligation_rows(
    freqtrade: Mapping[str, Any], scheduler_contract: Mapping[str, Any]
) -> tuple[dict[str, str], ...]:
    """Return the exhaustive source- and scheduler-bound Todo 15 matrix."""
    _validate_identity(freqtrade, scheduler_contract)
    contract_sha = str(scheduler_contract["fingerprint"])
    rows = tuple(
        {
            "dimension": dimension,
            "rule": rule,
            "source_owner": _OWNER,
            "source_method": method,
            "source_sha256": SOURCE_METHODS[(_OWNER, method)],
            "boundary_row": f"PT-{dimension}",
            "fixture_requirement": f"official-portfolio-trace:PT-{dimension}",
            "contract_sha256": contract_sha,
        }
        for dimension, method, rule in _DIMENSIONS
    )
    validate_portfolio_semantic_obligation_rows(rows, freqtrade, scheduler_contract)
    return rows


def validate_portfolio_semantic_obligation_rows(
    rows: Sequence[Mapping[str, Any]],
    freqtrade: Mapping[str, Any],
    scheduler_contract: Mapping[str, Any],
) -> None:
    """Reject a missing, changed, duplicate, reordered, or unbound obligation."""
    _validate_identity(freqtrade, scheduler_contract)
    normalized = [dict(row) for row in rows]
    if any(set(row) != _FIELDS for row in normalized):
        raise SpecValidationError("PORTFOLIO_SEMANTIC_REGISTRY: row fields differ")
    boundaries = [row["boundary_row"] for row in normalized]
    if len(boundaries) != len(set(boundaries)):
        raise SpecValidationError("PORTFOLIO_SEMANTIC_REGISTRY: duplicate boundary row")
    expected = _expected_rows(scheduler_contract)
    if normalized != expected:
        raise SpecValidationError(
            "PORTFOLIO_SEMANTIC_REGISTRY: obligations differ from canonical matrix"
        )


def _expected_rows(scheduler_contract: Mapping[str, Any]) -> list[dict[str, str]]:
    contract_sha = str(scheduler_contract["fingerprint"])
    return [
        {
            "dimension": dimension,
            "rule": rule,
            "source_owner": _OWNER,
            "source_method": method,
            "source_sha256": SOURCE_METHODS[(_OWNER, method)],
            "boundary_row": f"PT-{dimension}",
            "fixture_requirement": f"official-portfolio-trace:PT-{dimension}",
            "contract_sha256": contract_sha,
        }
        for dimension, method, rule in _DIMENSIONS
    ]


def _validate_identity(freqtrade: Mapping[str, Any], scheduler_contract: Mapping[str, Any]) -> None:
    source = freqtrade.get("source")
    fingerprint = scheduler_contract.get("fingerprint")
    if not isinstance(source, Mapping) or (
        source.get("commit") != FREQTRADE_COMMIT
        or source.get("observed_method_count") != 15
        or source.get("observed_method_merkle_root") != FREQTRADE_METHOD_MERKLE
    ):
        raise SpecValidationError("PORTFOLIO_SEMANTIC_REGISTRY: Freqtrade source identity differs")
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in fingerprint)
    ):
        raise SpecValidationError(
            "PORTFOLIO_SEMANTIC_REGISTRY: scheduler contract identity differs"
        )


def portfolio_registry_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    """Return a stable migration fingerprint for the additive portfolio matrix."""
    return hashlib.sha256(
        json.dumps(list(rows), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
