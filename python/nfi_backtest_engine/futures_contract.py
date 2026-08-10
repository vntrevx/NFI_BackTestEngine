"""Versioned Binance isolated-Futures semantic extension."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import SpecValidationError
from .execution_contract import load_execution_contract
from .freqtrade_semantic_profile import load_freqtrade_semantic_profile
from .scheduler_contract import load_scheduler_contract
from .specs import FUTURES_CONTRACT_SCHEMA, validate_schema

FUTURES_CONTRACT_VERSION = "freqtrade-futures-contract-v1"


def build_futures_contract(
    semantic_profile_path: str | Path,
    scheduler_contract_path: str | Path,
    execution_contract_path: str | Path,
) -> dict[str, Any]:
    """Build the exact Futures extension from the three kernel dependencies."""
    profile = load_freqtrade_semantic_profile(semantic_profile_path)
    scheduler = load_scheduler_contract(
        scheduler_contract_path,
        semantic_profile_path=semantic_profile_path,
    )
    execution = load_execution_contract(
        execution_contract_path,
        semantic_profile_path=semantic_profile_path,
        scheduler_contract_path=scheduler_contract_path,
    )
    contract: dict[str, Any] = {
        "schema_version": FUTURES_CONTRACT_VERSION,
        "semantic_profile_sha256": profile["fingerprint"],
        "scheduler_contract_sha256": scheduler["fingerprint"],
        "base_execution_contract_sha256": execution["fingerprint"],
        "scope": {
            "trading_mode": "futures",
            "exchange": "binance",
            "margin_mode": "isolated",
            "unknown_exchange_or_margin_semantics": "fail-before-native-promotion",
            "parity_requirement": "trade-surface-and-full-state-exact-zero-tolerance",
        },
        "required_inputs": [
            "funding-rate-candles",
            "mark-price-candles",
            "sealed-leverage-tiers",
            "historical-amount-and-price-precision",
        ],
        "leverage": {
            "source_priority": [
                "signal-explicit",
                "compiled-strategy-program",
                "config-default",
                "one",
            ],
            "tier_cap_selected_from_proposed_stake": True,
            "minimum": "one",
            "direction_preserved_from_entry_signal": True,
        },
        "funding": {
            "event_order": [
                "update-trade-extrema",
                "apply-running-funding",
                "position-adjustment",
                "exit-evaluation",
            ],
            "row_multiplication_order": "funding-rate-then-mark-price-then-position-amount",
            "positive_market_rate": {
                "long": "pays-negative",
                "short": "receives-positive",
            },
            "running_sum": "cpython-compensated-float-sum",
            "running_segment_moves_to_next_filled_order": True,
            "additional_entry_refresh": "inclusive-fill-timestamp-with-post-entry-amount",
            "partial_exit_refresh": "pre-exit-running-seed-then-next-tick-post-exit-rebase",
            "wallet_settlement": "filled-order-realization-only",
        },
        "liquidation": {
            "tier_selection": "last-tier-min-notional-not-greater-than-current-isolated-stake",
            "calculation_inputs": [
                "stake-amount",
                "maintenance-amount",
                "maintenance-margin-rate",
                "position-amount",
                "weighted-open-rate",
                "side",
            ],
            "buffer_direction": {
                "long": "raise-raw-liquidation-price",
                "short": "lower-raw-liquidation-price",
            },
            "recalculation_points": [
                "initial-entry-after-order-filled-state",
                "positive-adjustment-after-order-replay",
                "partial-exit-before-order-replay",
            ],
            "explicit_input_price_is_not_recalculated": True,
        },
        "exit_collision": {
            "candidate_order": [
                "strategy-exit",
                "custom-exit",
                "stop-loss",
                "liquidation",
            ],
            "stop_loss_wins_stop_liquidation_collision": True,
            "rejected_stop_does_not_fall_through_to_liquidation": True,
            "liquidation_bypasses_confirm_trade_exit": True,
            "threshold_beyond_complete_candle_range_fills_at_open": True,
            "opposite_side_signal_may_reopen_after_same_candle_exit": True,
        },
        "protections": {
            "methods": [
                "CooldownPeriod",
                "StoplossGuard",
                "MaxDrawdown",
                "LowProfitPairs",
            ],
            "evaluation_point": "after-confirmed-trade-close-before-wallet-recompute",
            "handler_order": ["all-local-handlers", "all-global-handlers"],
            "recent_trade_window": "close-time-greater-than-cutoff-and-less-than-or-equal-now",
            "lock_end_rounding": "always-advance-to-next-timeframe-boundary",
            "active_boundary": "maximum-lock-end-strictly-greater-than-entry-timestamp",
            "entry_lock_rejection_increments_rejected_signals": False,
            "scopes": ["pair", "global", "side", "all-sides"],
        },
        "observer": {
            "required_phases": [
                "trade.adjustment_check",
                "trade.exit_check",
                "trade.exit_order",
                "entry.lock_rejected",
                "candle.after",
            ],
            "canonical_state_fields": profile["observer"]["canonical_state_fields"],
        },
    }
    contract["fingerprint"] = _contract_fingerprint(contract)
    validate_schema(contract, FUTURES_CONTRACT_SCHEMA)
    return contract


def load_futures_contract(
    source: str | Path,
    *,
    semantic_profile_path: str | Path | None = None,
    scheduler_contract_path: str | Path | None = None,
    execution_contract_path: str | Path | None = None,
) -> dict[str, Any]:
    contract = read_json(source)
    validate_schema(contract, FUTURES_CONTRACT_SCHEMA)
    if contract["fingerprint"] != _contract_fingerprint(contract):
        raise SpecValidationError(
            "Futures contract fingerprint differs from its canonical content"
        )
    dependency_paths = (
        semantic_profile_path,
        scheduler_contract_path,
        execution_contract_path,
    )
    if any(path is not None for path in dependency_paths):
        if any(path is None for path in dependency_paths):
            raise SpecValidationError(
                "Futures contract validation requires all three dependency contracts"
            )
        assert semantic_profile_path is not None
        assert scheduler_contract_path is not None
        assert execution_contract_path is not None
        expected = build_futures_contract(
            semantic_profile_path,
            scheduler_contract_path,
            execution_contract_path,
        )
        if contract != expected:
            raise SpecValidationError(
                "Futures contract differs from its semantic dependencies"
            )
    return contract


def write_futures_contract(
    semantic_profile_path: str | Path,
    scheduler_contract_path: str | Path,
    execution_contract_path: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    contract = build_futures_contract(
        semantic_profile_path,
        scheduler_contract_path,
        execution_contract_path,
    )
    write_json(destination, contract)
    return contract


def validate_native_futures_contract(
    contract: dict[str, Any],
    native_json: str,
) -> None:
    """Require the compiled Rust Futures descriptor to match Python exactly."""
    try:
        native = json.loads(native_json)
    except json.JSONDecodeError as exc:
        raise SpecValidationError("Native Futures contract is not valid JSON") from exc
    validate_schema(native, FUTURES_CONTRACT_SCHEMA)
    if native != contract:
        raise SpecValidationError("Native Futures contract differs from Python contract")


def _contract_fingerprint(contract: dict[str, Any]) -> str:
    identity = {key: value for key, value in contract.items() if key != "fingerprint"}
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
