"""Independent provenance and semantics checks for changed-signal evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

from .changed_signal_execution_validation import (
    COLUMNS,
    SURFACES,
    term_matrix,
    validate_trade_state,
)
from .changed_signal_filesystem_trust import read_stable_file
from .changed_signal_json import canonical_sha256
from .changed_signal_mutation_validation import (
    MutationValidation,
    validate_executed_mutations,
)
from .changed_signal_provenance import (
    LaneProvenanceValidation,
    validate_lane_provenance,
)
from .changed_signal_source import (
    compact_predicate_from_bytes,
    upstream_signal_562_terms_from_bytes,
)
from .changed_signal_trust import trusted_upstream_source
from .errors import SpecValidationError
from .parity import first_difference
from .reference.contracts import (
    REFERENCE_IMAGE_REF,
    REFERENCE_INDEX_DIGEST,
    REFERENCE_VERSION,
)

_REPOSITORY: Final = Path(__file__).resolve().parents[2]
_CONTRACT: Final = Path("benchmarks/reference/strategies/CurrentChangedPredicateContract.py")
_UPSTREAM_SOURCE: Final = Path(
    "benchmarks/evidence/m22/current-x7-raw/upstream-NostalgiaForInfinityX7.source"
)
_INTERFACE_SHA256: Final = "93ddb2f5579acd7a20d489174ffb68cd191428ff996d291b33be81d97fa9bf66"
_UPSTREAM_COMMIT: Final = "eebaf97c1434bd8f208b7cd9c417606646e1e478"
_SOURCE_SHA256: Final = "a4ba29b94b459511163f05cce6687b5b84542147b11715a69e3fa468fab2767a"
_SEMANTIC_PROFILE_SHA256: Final = (
    "87f9549fcdea09a4b55be8bed7ef549a8e1df99fc532ea1110e04c252e61483f"
)
_REGISTRY_FINGERPRINT: Final = "4a99c6aaf6a4379d5afcf8010213fe182cef4bf728fbbd8328d062b67c6c2e73"
_TARGET_ID: Final = "286b19a0914ff96dec95adc322e7bbc7cf6e6c6ca357e4a063300fef8f2dbd47"
_CALLBACK_TARGET_ID: Final = "2c763d57afc84e9a5b0f61349d9c8b9136160544ac4e31952b1ad5d9e076a185"


def validate_authoritative_proof(document: Mapping[str, Any]) -> None:
    """Recompute source, capture, artifact, coverage, mutation, and parity claims."""
    identity = _mapping(document, "identity")
    expected_identity = {
        "upstream_commit": _UPSTREAM_COMMIT,
        "source_sha256": _SOURCE_SHA256,
        "semantic_profile_sha256": _SEMANTIC_PROFILE_SHA256,
        "semantic_registry_fingerprint": _REGISTRY_FINGERPRINT,
        "freqtrade_digest": REFERENCE_INDEX_DIGEST,
        "target_id": _TARGET_ID,
        "callback_target_id": _CALLBACK_TARGET_ID,
    }
    if any(identity.get(key) != value for key, value in expected_identity.items()):
        raise SpecValidationError("changed signal authoritative identity differs")

    capture = _mapping(document, "capture")
    expected_capture = {
        "kind": "sealed-freqtrade-backtest",
        "freqtrade_version": REFERENCE_VERSION,
        "image_ref": REFERENCE_IMAGE_REF,
        "image_index_digest": REFERENCE_INDEX_DIGEST,
        "contract_path": _CONTRACT.as_posix(),
        "contract_sha256": _file_sha256(_CONTRACT),
        "interface_sha256": _INTERFACE_SHA256,
        "bounded_rows": 5,
        "network": "none",
    }
    if any(capture.get(key) != value for key, value in expected_capture.items()):
        raise SpecValidationError("changed signal sealed capture identity differs")

    contract_snapshot = read_stable_file(
        _REPOSITORY / _CONTRACT,
        _REPOSITORY / _CONTRACT,
    )
    expression, terms, span = compact_predicate_from_bytes(contract_snapshot.payload)
    replay_source = _REPOSITORY / _UPSTREAM_SOURCE
    canonical_source = trusted_upstream_source(_REPOSITORY, replay_source).payload
    replay_terms = upstream_signal_562_terms_from_bytes(canonical_source)
    canonical_terms = upstream_signal_562_terms_from_bytes(canonical_source)
    if terms != replay_terms or replay_terms != canonical_terms:
        raise SpecValidationError("compact predicate differs from authenticated upstream source")
    predicate = _mapping(document, "predicate")
    expected_predicate = {
        "target_id": _TARGET_ID,
        "callback_target_id": _CALLBACK_TARGET_ID,
        "source_expression": expression,
        "source_expression_sha256": hashlib.sha256(expression.encode()).hexdigest(),
        "source_span": span,
        "required_columns": list(COLUMNS),
        "atomic_terms": terms,
        "atomic_term_count": 4,
    }
    if predicate != expected_predicate:
        raise SpecValidationError("changed signal predicate differs from bound source")

    modes = _mapping(document, "modes")
    for mode in ("spot", "futures"):
        _validate_mode(mode, _mapping(modes, mode), capture, identity)


def _validate_mode(
    mode_name: Literal["spot", "futures"],
    mode: Mapping[str, Any],
    capture: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> None:
    official = _mapping(mode, "official")
    native = _mapping(mode, "native")
    execution_contract = _mapping(mode, "execution_contract")
    supports_short = mode_name == "futures"
    if execution_contract != {
        "can_short": supports_short,
        "short_execution_supported": supports_short,
        "changed_signal_reached": True,
    }:
        raise SpecValidationError("changed signal mode execution contract differs")
    official_provenance = _mapping(mode, "official_provenance")
    native_provenance = _mapping(mode, "native_provenance")
    validate_lane_provenance(
        LaneProvenanceValidation(
            mode=mode_name,
            provenance=official_provenance,
            lane=official,
            capture=capture,
            official=True,
        )
    )
    validate_lane_provenance(
        LaneProvenanceValidation(
            mode=mode_name,
            provenance=native_provenance,
            lane=native,
            capture=capture,
            official=False,
        )
    )
    if official_provenance["raw_output_sha256"] == native_provenance["raw_output_sha256"]:
        raise SpecValidationError("changed signal Oracle and Native lanes are copied")

    callbacks = _mapping(official, "callback_columns")
    if callbacks != _mapping(native, "callback_columns") or tuple(callbacks) != COLUMNS:
        raise SpecValidationError("changed signal callback input identity differs")
    if canonical_sha256(callbacks) != identity["market_data_sha256"]:
        raise SpecValidationError("changed signal bounded data identity differs")
    matrix = term_matrix(callbacks)
    expected_signal = [int(any(row)) for row in matrix]
    for lane in (official, native):
        signal = _mapping(lane, "signal_tag")
        if signal.get("enter_short") != expected_signal:
            raise SpecValidationError("changed signal output is not derived from bound inputs")
        validate_trade_state(lane, mode_name)

    passing = [index for index, value in enumerate(expected_signal) if value == 1]
    failing = [index for index, value in enumerate(expected_signal) if value == 0]
    independent = [
        next(index for index, row in enumerate(matrix) if row[term] and sum(row) == 1)
        for term in range(4)
    ]
    expected_coverage = {
        "passing_rows": passing,
        "failing_rows": failing,
        "independent_term_rows": independent,
    }
    if _mapping(mode, "coverage") != expected_coverage:
        raise SpecValidationError("changed signal coverage is fabricated")

    parity = _mapping(mode, "parity")
    for surface in SURFACES:
        comparison = _mapping(parity, surface)
        if first_difference(official[surface], native[surface]) is not None or comparison != {
            "exact": True,
            "first_difference": None,
        }:
            raise SpecValidationError(f"changed signal {mode_name} {surface} parity differs")
    contract_snapshot = read_stable_file(
        _REPOSITORY / _CONTRACT,
        _REPOSITORY / _CONTRACT,
    )
    validate_executed_mutations(
        MutationValidation(
            mode=mode_name,
            records=mode["mutations"],
            baseline=expected_signal,
            callbacks=callbacks,
            contract_source=contract_snapshot.payload.decode("utf-8"),
        )
    )


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    candidate = value[key]
    if not isinstance(candidate, Mapping):
        raise SpecValidationError(f"changed signal proof field {key!r} must be an object")
    return candidate


def _file_sha256(relative: Path | str) -> str:
    return hashlib.sha256((_REPOSITORY / relative).read_bytes()).hexdigest()
