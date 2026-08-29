"""Promotion-time execution checks for changed-signal source mutants."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from .changed_signal_filesystem_trust import TrustedFileSnapshot, read_stable_file
from .changed_signal_mutation import execute_native_mutant_snapshot, source_mutants
from .changed_signal_official_mutants import OfficialMutantSource, fresh_official_mutants
from .changed_signal_trust import expected_official_capture_attestation
from .errors import SpecValidationError
from .parity import first_difference
from .reference.contracts import REFERENCE_VERSION

_REPOSITORY: Final = Path(__file__).resolve().parents[2]
_INTERFACE_SHA256: Final = "93ddb2f5579acd7a20d489174ffb68cd191428ff996d291b33be81d97fa9bf66"
Mode = Literal["spot", "futures"]


@dataclass(frozen=True, slots=True)
class MutationValidation:
    """All bounded inputs needed to freshly validate one mode's mutants."""

    mode: Mode
    records: Sequence[Mapping[str, Any]]
    baseline: list[int]
    callbacks: Mapping[str, Any]
    contract_source: str


def validate_executed_mutations(validation: MutationValidation) -> None:
    """Recapture official and recompile Native from authenticated mutant snapshots."""
    mode = validation.mode
    records = validation.records
    expected_mutants = source_mutants(validation.contract_source)
    if [record["mutation_id"] for record in records] != [
        mutant.identifier for mutant in expected_mutants
    ]:
        raise SpecValidationError("changed signal mutation inventory differs")
    sources = tuple(_artifact(record, "source") for record in records)
    official_fresh = fresh_official_mutants(
        mode,
        tuple(
            OfficialMutantSource(mutant.identifier, source.payload)
            for mutant, source in zip(expected_mutants, sources, strict=True)
        ),
    )
    typed_callbacks = {
        name: list(values) for name, values in validation.callbacks.items()
    }
    for record, mutant, source, recaptured in zip(
        records,
        expected_mutants,
        sources,
        official_fresh,
        strict=True,
    ):
        if record["kind"] != mutant.kind or record["term_index"] != mutant.term_index:
            raise SpecValidationError("changed signal mutation identity differs")
        if source.payload.decode("utf-8") != mutant.source:
            raise SpecValidationError("changed signal mutant source differs")
        official = _json_artifact(record, "official_output")
        if (
            official.get("freqtrade_version") != REFERENCE_VERSION
            or official.get("interface_sha256") != _INTERFACE_SHA256
            or official.get("trading_mode") != mode
            or official.get("capture_contract")
            != expected_official_capture_attestation(mode)
            or official != recaptured
        ):
            raise SpecValidationError("changed signal official mutant capture differs")
        official_values = [
            row.get("enter_short") for row in json.loads(official["output"])["data"]
        ]
        native = _json_artifact(record, "native_output")
        compiled_values = execute_native_mutant_snapshot(
            source.payload,
            mode,
            typed_callbacks,
        )
        if native.get("enter_short") != compiled_values or official_values != compiled_values:
            raise SpecValidationError("changed signal mutant lanes differ from execution")
        difference = first_difference(validation.baseline, compiled_values)
        if difference is None:
            raise SpecValidationError("changed signal mutant was not detected")
        expected_difference = {
            "path": f"$.signal_tag.enter_short{difference.path.removeprefix('$')}",
            "reason": difference.reason,
            "expected": difference.expected,
            "actual": difference.actual,
        }
        if record["detected"] is not True or record["first_difference"] != expected_difference:
            raise SpecValidationError("changed signal mutant first difference differs")


def _json_artifact(record: Mapping[str, Any], key: str) -> dict[str, Any]:
    try:
        value = json.loads(_artifact(record, key).payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecValidationError("changed signal mutant JSON artifact is malformed") from exc
    if not isinstance(value, dict):
        raise SpecValidationError("changed signal mutant JSON artifact is not an object")
    return value


def _artifact(record: Mapping[str, Any], key: str) -> TrustedFileSnapshot:
    artifact = record[key]
    if not isinstance(artifact, Mapping):
        raise SpecValidationError("changed signal mutant artifact must be an object")
    path = (_REPOSITORY / artifact["path"]).absolute()
    if not path.is_relative_to(_REPOSITORY):
        raise SpecValidationError("changed signal mutant artifact path differs")
    snapshot = read_stable_file(path, path)
    if hashlib.sha256(snapshot.payload).hexdigest() != artifact["sha256"]:
        raise SpecValidationError("changed signal mutant artifact hash differs")
    return snapshot
