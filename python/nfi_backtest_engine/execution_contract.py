"""Versioned Spot order, wallet, fee, and precision semantics."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import SpecValidationError
from .freqtrade_semantic_profile import load_freqtrade_semantic_profile
from .scheduler_contract import load_scheduler_contract
from .specs import EXECUTION_CONTRACT_SCHEMA, validate_schema

EXECUTION_CONTRACT_VERSION = "freqtrade-execution-contract-v1"


def build_execution_contract(
    semantic_profile_path: str | Path,
    scheduler_contract_path: str | Path,
) -> dict[str, Any]:
    """Build the exact Spot execution contract from its versioned dependencies."""
    profile = load_freqtrade_semantic_profile(semantic_profile_path)
    scheduler = load_scheduler_contract(
        scheduler_contract_path,
        semantic_profile_path=semantic_profile_path,
    )
    contract: dict[str, Any] = {
        "schema_version": EXECUTION_CONTRACT_VERSION,
        "semantic_profile_sha256": profile["fingerprint"],
        "scheduler_contract_sha256": scheduler["fingerprint"],
        "scope": {
            "trading_modes": ["spot"],
            "order_types": ["market"],
            "unknown_semantics": "fail-before-native-promotion",
            "parity_requirement": "trade-surface-and-full-state-exact-zero-tolerance",
        },
        "entry": {
            "gate_order": [
                "pair-lock",
                "available-slot",
                "compiled-route-support",
                "available-stake",
                "custom-stake",
                "amount-precision",
                "confirm-trade-entry",
                "fill",
                "order-filled-callback",
                "wallet-recompute",
            ],
            "fill_rate": "execution-candle-open",
            "lock_rejection_increments_rejected_signals": False,
            "slot_rejection_increments_rejected_signals": True,
            "order_id_reserved_before_amount_precision_and_confirmation": True,
            "pre_order_gate_rejection_consumes_order_id": False,
            "amount_or_confirmation_rejection_consumes_order_id": True,
            "rejected_attempt_consumes_trade_id": False,
            "trade_id_consumed_on_fill_only": True,
        },
        "adjustment": {
            "callback_position": "after-exit-check-and-entry-fill-on-same-candle",
            "positive_fill_rate": "execution-candle-open",
            "partial_exit_fill_rate": "execution-candle-open-rounded-to-frozen-price-step",
            "positive_stake_limited_by_available_wallet": True,
            "filled_orders_replayed_in_source_order": True,
            "order_filled_callback_after_replay": True,
        },
        "exit": {
            "candidate_order": [
                "explicit-signal",
                "compiled-state-machine-custom-exit",
                "compiled-nfi-custom-exit",
                "compiled-callback-custom-exit",
                "contract-timed-exit",
                "stop-loss",
                "liquidation-extension",
            ],
            "strategy_fill_rate": "execution-candle-open-rounded-to-frozen-price-step",
            "confirmation_before_fill": True,
            "liquidation_confirmation": False,
            "filled_order_appended_before_profit_replay": True,
        },
        "wallet": {
            "scope": "single-global-quote-wallet",
            "mutation_order": "serial-scheduler-event-order",
            "free_balance_components": [
                "starting-balance",
                "closed-realized-profit",
                "open-realized-partial-profit",
                "minus-open-trade-stake",
            ],
            "running_funding_excluded_until_realized": True,
            "unlimited_stake_policy": "equal-configured-slot-allocation-capped-by-available",
            "tradable_balance_ratio_applied_before_stake_selection": True,
            "slot_limit": "max-open-trades-global",
        },
        "fees": {
            "open_rate": "fee-open-override-else-common-fee",
            "close_rate": "fee-close-override-else-common-fee",
            "entry_fee_in_cost_basis": True,
            "exit_fee_in_realized_profit": True,
            "each_adjustment_has_its_own_fee": True,
        },
        "precision": {
            "arithmetic_domain": "shortest-f64-decimal-text-exact-rational",
            "amount": "floor-to-exchange-step",
            "entry_price": "execution-candle-open",
            "exit_price": "round-to-trade-frozen-exchange-step",
            "weighted_basis_division": "ccxt-precise-truncate-18-decimals",
            "realized_exit_profit": "python-format-ties-to-even-8-decimals",
            "aggregate_profit": "numpy-pairwise-f64-order",
            "total_volume": "cpython-compensated-float-sum",
        },
        "observer": {
            "required_phases": [
                "trade.entry",
                "trade.exit_check",
                "trade.adjustment_check",
                "trade.exit_order",
                "candle.after",
            ],
            "canonical_state_fields": profile["observer"]["canonical_state_fields"],
        },
    }
    contract["fingerprint"] = _contract_fingerprint(contract)
    validate_schema(contract, EXECUTION_CONTRACT_SCHEMA)
    return contract


def load_execution_contract(
    source: str | Path,
    *,
    semantic_profile_path: str | Path | None = None,
    scheduler_contract_path: str | Path | None = None,
) -> dict[str, Any]:
    contract = read_json(source)
    validate_schema(contract, EXECUTION_CONTRACT_SCHEMA)
    if contract["fingerprint"] != _contract_fingerprint(contract):
        raise SpecValidationError(
            "execution contract fingerprint differs from its canonical content"
        )
    if semantic_profile_path is not None or scheduler_contract_path is not None:
        if semantic_profile_path is None or scheduler_contract_path is None:
            raise SpecValidationError(
                "execution contract validation requires both dependency contracts"
            )
        expected = build_execution_contract(
            semantic_profile_path,
            scheduler_contract_path,
        )
        if contract != expected:
            raise SpecValidationError(
                "execution contract differs from its semantic dependencies"
            )
    return contract


def write_execution_contract(
    semantic_profile_path: str | Path,
    scheduler_contract_path: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    contract = build_execution_contract(
        semantic_profile_path,
        scheduler_contract_path,
    )
    write_json(destination, contract)
    return contract


def validate_native_execution_contract(
    contract: dict[str, Any],
    native_json: str,
) -> None:
    """Require the compiled Rust execution descriptor to match Python exactly."""
    try:
        native = json.loads(native_json)
    except json.JSONDecodeError as exc:
        raise SpecValidationError("Native execution contract is not valid JSON") from exc
    validate_schema(native, EXECUTION_CONTRACT_SCHEMA)
    if native != contract:
        raise SpecValidationError("Native execution contract differs from Python contract")


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
