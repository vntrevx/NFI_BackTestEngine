"""Typed status artifact for an untrusted compatibility identity boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

from .canonical import write_json
from .compatibility_status import CompatibilityRunObservation, DiscoveryExecution

_STATUS_VERSION: Final = "compatibility-product-status-v1"
type IdentityFailureReason = Literal["malformed_identity", "missing_identity"]
type JsonValue = str | bool | None | dict[str, "JsonValue"]


def write_identity_failure_status(
    observation: CompatibilityRunObservation,
    reason: IdentityFailureReason,
    output_path: Path,
) -> Mapping[str, JsonValue]:
    """Write a schema-valid failure without inventing a trusted identity."""
    status: dict[str, JsonValue] = {
        "schema_version": _STATUS_VERSION,
        "identity": None,
        "observed_identity": {
            "engine_sha": observation.current_engine_sha,
            "upstream_sha": observation.current_upstream_sha,
        },
        "workflow": {"state": observation.workflow_execution.value},
        "product": {"state": "inconclusive", "reason": reason},
        "discovery_execution": {
            mode: observation.discovery_execution.get(
                mode,
                DiscoveryExecution.SKIPPED,
            ).value
            for mode in ("spot", "futures")
        },
        "same_engine_proof": {
            "complete": False,
            "engine_sha": observation.current_engine_sha,
            "source_run_id": None,
            "manifest_sha256": None,
            "artifact_sha256": {},
        },
        "required_status_passed": False,
    }
    status["fingerprint"] = _canonical_sha256(status)
    write_json(output_path, status)
    return status


def _canonical_sha256(value: Mapping[str, JsonValue]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
