"""Promotion validation for one identity-bound changed signal proof."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from .changed_signal_filesystem_trust import trusted_file_operation
from .changed_signal_validation import validate_authoritative_proof
from .errors import SpecValidationError
from .parity import first_difference
from .specs import validate_schema

_SCHEMA: Final = "changed-signal-proof-v1.schema.json"
_SURFACES: Final = ("signal_tag", "callback_columns", "trades", "full_state")
_MODES: Final = ("spot", "futures")


@dataclass(frozen=True, slots=True)
class ChangedSignalIdentity:
    """Exact upstream, compiler, Oracle, and bounded-data proof identity."""

    upstream_commit: str
    source_sha256: str
    semantic_profile_sha256: str
    semantic_registry_fingerprint: str
    freqtrade_digest: str
    market_data_sha256: str
    target_id: str
    callback_target_id: str


def validate_changed_signal_proof(
    document: Mapping[str, Any],
    identity: ChangedSignalIdentity,
) -> None:
    """Reject incomplete, stale, partial, or non-detecting changed-signal proof."""
    with trusted_file_operation():
        _validate_changed_signal_proof(document, identity)


def _validate_changed_signal_proof(
    document: Mapping[str, Any],
    identity: ChangedSignalIdentity,
) -> None:
    validate_schema(document, _SCHEMA)
    if document["identity"] != asdict(identity):
        raise SpecValidationError("changed signal proof identity is stale")
    unsigned = dict(document)
    fingerprint = unsigned.pop("fingerprint")
    if fingerprint != _sha256(unsigned):
        raise SpecValidationError("changed signal proof fingerprint differs")
    validate_authoritative_proof(document)

    predicate = _mapping(document, "predicate")
    if (
        predicate["target_id"] != identity.target_id
        or predicate["callback_target_id"] != identity.callback_target_id
    ):
        raise SpecValidationError("changed signal target identity is stale")
    expression = predicate["source_expression"]
    if hashlib.sha256(expression.encode()).hexdigest() != predicate["source_expression_sha256"]:
        raise SpecValidationError("changed signal source expression identity differs")
    required_columns = predicate["required_columns"]
    term_count = predicate["atomic_term_count"]
    if len(predicate["atomic_terms"]) != term_count:
        raise SpecValidationError("changed signal atomic term inventory differs")
    modes = _mapping(document, "modes")
    if tuple(modes) != _MODES:
        raise SpecValidationError("changed signal proof mode inventory differs")
    for mode_name in _MODES:
        mode = _mapping(modes, mode_name)
        official = _mapping(mode, "official")
        native = _mapping(mode, "native")
        callback_columns = _mapping(official, "callback_columns")
        if sorted(required_columns) != sorted(callback_columns):
            raise SpecValidationError("changed signal informative column inventory differs")
        coverage = _mapping(mode, "coverage")
        if (
            not coverage["passing_rows"]
            or not coverage["failing_rows"]
            or len(coverage["independent_term_rows"]) != term_count
        ):
            raise SpecValidationError("changed signal branch coverage is incomplete")
        parity = _mapping(mode, "parity")
        for surface in _SURFACES:
            comparison = _mapping(parity, surface)
            difference = first_difference(official[surface], native[surface])
            if (
                difference is not None
                or comparison["exact"] is not True
                or comparison["first_difference"] is not None
            ):
                raise SpecValidationError(
                    f"changed signal {mode_name} {surface} parity is incomplete"
                )
        mutations = mode["mutations"]
        if {item["term_index"] for item in mutations} != set(range(term_count)):
            raise SpecValidationError("changed signal mutation term inventory differs")
        if any(
            item["detected"] is not True or not isinstance(item["first_difference"], Mapping)
            for item in mutations
        ):
            raise SpecValidationError("changed signal mutation was not detected")
    if document["blockers"] or document["native_promotion_allowed"] is not True:
        raise SpecValidationError("changed signal proof cannot promote with blockers")


def write_changed_signal_proof(
    destination: str | Path,
    document: Mapping[str, Any],
    identity: ChangedSignalIdentity,
) -> None:
    """Atomically publish a validated proof without exposing partial evidence."""
    validate_changed_signal_proof(document, identity)
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    temporary = Path(name)
    try:
        payload = (json.dumps(document, indent=2) + "\n").encode()
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _publication_checkpoint(temporary)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def _publication_checkpoint(_temporary: Path) -> None:
    """Test synchronization point before authoritative publication."""


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    candidate = value[key]
    if not isinstance(candidate, Mapping):
        raise SpecValidationError(f"changed signal proof field {key!r} must be an object")
    return candidate


def _sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
