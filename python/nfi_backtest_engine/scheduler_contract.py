"""Versioned Freqtrade-compatible scheduler contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import SpecValidationError
from .freqtrade_semantic_profile import load_freqtrade_semantic_profile
from .specs import SCHEDULER_CONTRACT_SCHEMA, validate_schema

SCHEDULER_CONTRACT_VERSION = "freqtrade-scheduler-contract-v1"
SIGNAL_SOURCE_ROW_SHIFT = 1
CALLBACK_FEATURE_ROW_OFFSET = -1


def build_scheduler_contract(
    semantic_profile_path: str | Path,
) -> dict[str, Any]:
    """Build the scheduler contract bound to one current semantic profile."""
    semantic_profile = load_freqtrade_semantic_profile(semantic_profile_path)
    contract: dict[str, Any] = {
        "schema_version": SCHEDULER_CONTRACT_VERSION,
        "semantic_profile_sha256": semantic_profile["fingerprint"],
        "chronology": {
            "timestamp_order": "ascending",
            "same_timestamp_pair_order": [
                "open-trade-insertion-order",
                "configured-order-for-remaining-pairs",
            ],
            "pair_processed_once_per_timestamp": True,
            "wallet_mutation": "serial-global-event-loop",
            "final_force_exit_order": "reverse-open-trade-insertion-order",
        },
        "visibility": {
            "signal_source_row_shift": SIGNAL_SOURCE_ROW_SHIFT,
            "callback_feature_row_offset": CALLBACK_FEATURE_ROW_OFFSET,
            "startup_context_is_executable": False,
            "timerange_stop_callback_visible": True,
            "timerange_stop_entry_allowed": False,
        },
        "preparation": {
            "pair_preparation_parallel": True,
            "published_pair_order": "configured-pair-order",
            "wallet_event_parallel": False,
        },
        "observer": {
            "official_phase": "candle.after",
            "native_phase": "pair.after_candle",
            "comparison_key": ["timestamp_ms", "pair"],
            "unknown_schedule": "fail-before-native-promotion",
        },
    }
    contract["fingerprint"] = _contract_fingerprint(contract)
    validate_schema(contract, SCHEDULER_CONTRACT_SCHEMA)
    return contract


def load_scheduler_contract(
    source: str | Path,
    *,
    semantic_profile_path: str | Path | None = None,
) -> dict[str, Any]:
    contract = read_json(source)
    validate_schema(contract, SCHEDULER_CONTRACT_SCHEMA)
    if contract["fingerprint"] != _contract_fingerprint(contract):
        raise SpecValidationError(
            "scheduler contract fingerprint differs from its canonical content"
        )
    if semantic_profile_path is not None:
        expected = build_scheduler_contract(semantic_profile_path)
        if contract != expected:
            raise SpecValidationError(
                "scheduler contract differs from the current semantic profile"
            )
    return contract


def write_scheduler_contract(
    semantic_profile_path: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    contract = build_scheduler_contract(semantic_profile_path)
    write_json(destination, contract)
    return contract


def validate_native_scheduler_contract(
    contract: dict[str, Any],
    native_json: str,
) -> None:
    """Require the compiled Rust scheduler descriptor to match byte semantics."""
    try:
        native = json.loads(native_json)
    except json.JSONDecodeError as exc:
        raise SpecValidationError("Native scheduler contract is not valid JSON") from exc
    validate_schema(native, SCHEDULER_CONTRACT_SCHEMA)
    if native != contract:
        raise SpecValidationError("Native scheduler contract differs from Python contract")


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
