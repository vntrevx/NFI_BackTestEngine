"""Digest-bound contract for the Freqtrade semantics observed by this engine."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import SpecValidationError
from .fixture import sha256_file
from .reference.contracts import (
    REFERENCE_CCXT_VERSION,
    REFERENCE_CONFIG_DIGEST,
    REFERENCE_IMAGE,
    REFERENCE_INDEX_DIGEST,
    REFERENCE_PLATFORM,
    REFERENCE_PLATFORM_DIGEST,
    REFERENCE_TRACER_VERSION,
    REFERENCE_VERSION,
)
from .reference_assets import reference_tracer_root
from .reference_tracer import nfi_reference_trace
from .specs import FREQTRADE_SEMANTIC_PROFILE_SCHEMA, validate_schema
from .state_trace import TRACE_SCHEMA_VERSION

FREQTRADE_SEMANTIC_PROFILE_VERSION = "freqtrade-semantic-profile-v1"

_CANONICAL_STATE_FIELDS = (
    "quote_free",
    "base_balances",
    "open_trade_count",
    "realized_profit",
    "closed_trade_count",
    "rejected_signals",
    "trade_id_counter",
    "order_id_counter",
    "locks",
)

_EXCLUDED_SCOPE = (
    "live order placement and reconciliation",
    "telegram and webserver control surfaces",
    "hyperopt execution",
    "dynamic pairlist selection during a run",
)


def build_current_freqtrade_semantic_profile() -> dict[str, Any]:
    """Build the profile from immutable reference pins and observer source."""
    tracer_path = reference_tracer_root() / "nfi_reference_trace.py"
    projection_path = Path(__file__).with_name("trace_projection.py")
    semantic_observer_path = Path(__file__).with_name("semantic_observer.py")
    events = _observed_events(tracer_path)
    profile: dict[str, Any] = {
        "schema_version": FREQTRADE_SEMANTIC_PROFILE_VERSION,
        "reference": {
            "version": REFERENCE_VERSION,
            "image": REFERENCE_IMAGE,
            "image_index_digest": REFERENCE_INDEX_DIGEST,
            "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
            "image_config_digest": REFERENCE_CONFIG_DIGEST,
            "platform": REFERENCE_PLATFORM,
            "ccxt_version": REFERENCE_CCXT_VERSION,
        },
        "observer": {
            "tracer_version": REFERENCE_TRACER_VERSION,
            "trace_schema_version": TRACE_SCHEMA_VERSION,
            "tracer_source_sha256": sha256_file(tracer_path),
            "state_projection_source_sha256": sha256_file(projection_path),
            "semantic_projection_source_sha256": sha256_file(semantic_observer_path),
            "observed_methods": _observed_methods(),
            "events": events,
            "canonical_state_fields": list(_CANONICAL_STATE_FIELDS),
        },
        "scope": {
            "contract": "NFI-reachable Freqtrade backtesting semantics only",
            "excluded": list(_EXCLUDED_SCOPE),
            "unknown_behavior": "fail-before-native-promotion",
            "parity_requirement": "trade-surface-and-full-state-exact",
        },
    }
    profile["fingerprint"] = _profile_fingerprint(profile)
    validate_schema(profile, FREQTRADE_SEMANTIC_PROFILE_SCHEMA)
    return profile


def load_freqtrade_semantic_profile(
    source: str | Path,
    *,
    require_current: bool = True,
) -> dict[str, Any]:
    """Load, validate, and optionally bind a profile to the current observer."""
    profile = read_json(source)
    validate_schema(profile, FREQTRADE_SEMANTIC_PROFILE_SCHEMA)
    actual_fingerprint = _profile_fingerprint(profile)
    if profile["fingerprint"] != actual_fingerprint:
        raise SpecValidationError(
            "Freqtrade semantic profile fingerprint differs from its canonical content"
        )
    if require_current:
        current = build_current_freqtrade_semantic_profile()
        if profile != current:
            raise SpecValidationError(
                "Freqtrade semantic profile differs from the current pinned observer"
            )
    return profile


def write_current_freqtrade_semantic_profile(destination: str | Path) -> dict[str, Any]:
    profile = build_current_freqtrade_semantic_profile()
    write_json(destination, profile)
    return profile


def _observed_methods() -> list[dict[str, str]]:
    groups = (
        (
            "freqtrade.optimize.backtesting.Backtesting",
            nfi_reference_trace.PINNED_METHOD_HASHES,
        ),
        (
            "freqtrade.exchange.exchange.Exchange",
            nfi_reference_trace.PINNED_EXCHANGE_METHOD_HASHES,
        ),
        (
            "freqtrade.data.dataprovider.DataProvider",
            nfi_reference_trace.PINNED_DATA_PROVIDER_METHOD_HASHES,
        ),
        (
            "freqtrade.strategy.interface.IStrategy",
            nfi_reference_trace.PINNED_STRATEGY_METHOD_HASHES,
        ),
    )
    return [
        {
            "owner": owner,
            "method": method,
            "source_sha256": source_sha256,
        }
        for owner, methods in groups
        for method, source_sha256 in sorted(methods.items())
    ]


def _observed_events(tracer_path: Path) -> list[dict[str, Any]]:
    """Extract literal observer events from the read-only tracer AST."""
    tree = ast.parse(tracer_path.read_text(encoding="utf-8"), filename=str(tracer_path))
    callbacks: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "_append" or len(node.args) < 3:
            continue
        phase_node = node.args[2]
        if not isinstance(phase_node, ast.Constant) or not isinstance(phase_node.value, str):
            raise SpecValidationError("reference observer contains a dynamic event phase")
        phase = phase_node.value
        phase_callbacks = callbacks.setdefault(phase, set())
        callback_node = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "callback"),
            None,
        )
        if callback_node is None:
            continue
        if not isinstance(callback_node, ast.Constant) or not isinstance(
            callback_node.value, str
        ):
            raise SpecValidationError(
                f"reference observer event {phase!r} contains a dynamic callback identity"
            )
        phase_callbacks.add(callback_node.value)
    if not callbacks:
        raise SpecValidationError("reference observer defines no literal semantic events")
    return [
        {
            "phase": phase,
            "callbacks": sorted(callbacks[phase]),
        }
        for phase in sorted(callbacks)
    ]


def _profile_fingerprint(profile: dict[str, Any]) -> str:
    identity = {key: value for key, value in profile.items() if key != "fingerprint"}
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
