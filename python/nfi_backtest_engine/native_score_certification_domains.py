"""Full X7 certificate, replay, and raw process score evidence parsing."""

from __future__ import annotations

from typing import Any

from .errors import SpecValidationError
from .native_score_domain_identity import (
    VerificationClockPolicy,
)
from .native_score_domain_identity import (
    exact_fields as _exact,
)
from .native_score_domain_identity import (
    score_context as _context,
)
from .native_score_domain_identity import (
    validate_producer as _validate_producer,
)
from .specs import FULL_X7_CERTIFICATION_V2_SCHEMA, validate_schema

PROCESS_EVIDENCE_VERSION = "native-score-process-evidence-v1"
REPLAY_EVIDENCE_VERSION = "full-x7-certificate-replay-v1"


def validate_certification_input(
    document: Any,
    *,
    record: dict[str, Any],
    field: str,
    verification_clock: VerificationClockPolicy,
) -> None:
    """Open an actual Full X7 certificate/replay or raw process measurement."""
    if record["record_type"] == "portfolio_certificate":
        _validate_portfolio_document(document, record=record, field=field)
        return
    _validate_process_document(
        document,
        record=record,
        verification_clock=verification_clock,
    )


def _validate_portfolio_document(
    document: Any,
    *,
    record: dict[str, Any],
    field: str,
) -> None:
    if field == "certificate_sha256":
        validate_schema(document, FULL_X7_CERTIFICATION_V2_SCHEMA)
        gates = document["gates"]
        if (
            document["status"] != "certified"
            or document["release_certified"] is not True
            or document["claim_scope"]["mode_contract"] != record["mode_contract"]
            or document["claim_scope"]["pair_count"] != 80
            or not isinstance(gates, dict)
            or not gates
            or any(
                not isinstance(gate, dict) or gate.get("met") is not True for gate in gates.values()
            )
        ):
            raise SpecValidationError("Full X7 certificate is failed, incomplete, or cross-mode")
        score = document["inputs"].get("native_score_identity")
        if score != _context(record):
            raise SpecValidationError("Full X7 certificate score identity differs")
        return
    _exact(
        document,
        {
            "schema_version",
            "certificate_sha256",
            "candidate_identity_sha256",
            "context",
            "exact",
            "complete",
        },
        "Full X7 certificate replay",
    )
    if (
        document["schema_version"] != REPLAY_EVIDENCE_VERSION
        or document["certificate_sha256"] != record["payload"]["certificate_sha256"]
        or document["candidate_identity_sha256"] != record["candidate_identity_sha256"]
        or document["context"] != _context(record)
        or document["exact"] is not True
        or document["complete"] is not True
    ):
        raise SpecValidationError("Full X7 certificate replay is stale or incomplete")


def _validate_process_document(
    document: Any,
    *,
    record: dict[str, Any],
    verification_clock: VerificationClockPolicy,
) -> None:
    _exact(
        document,
        {
            "schema_version",
            "producer",
            "context",
            "runtime",
            "population",
            "sample_index",
            "process",
            "output",
        },
        "process measurement",
    )
    if document["schema_version"] != PROCESS_EVIDENCE_VERSION:
        raise SpecValidationError("process measurement schema version differs")
    _validate_producer(
        document["producer"],
        record=record,
        verification_clock=verification_clock,
    )
    if document["context"] != _context(record):
        raise SpecValidationError("process measurement context differs")
    payload = record["payload"]
    if any(
        document[field] != payload[field] for field in ("runtime", "population", "sample_index")
    ):
        raise SpecValidationError("process measurement population differs")
    _exact(
        document["process"],
        {
            "exit_code",
            "wall_seconds",
            "cpu_seconds",
            "peak_rss_bytes",
            "memory_limit_bytes",
            "oom",
            "swap_bytes",
        },
        "process measurement sample",
    )
    process = document["process"]
    if (
        process["exit_code"] != 0
        or process["wall_seconds"] != payload["wall_seconds"]
        or process["peak_rss_bytes"] != payload["peak_rss_bytes"]
        or process["memory_limit_bytes"] != payload["memory_limit_bytes"]
        or process["oom"] != payload["oom"]
        or process["swap_bytes"] != payload["swap_bytes"]
    ):
        raise SpecValidationError("process measurement does not derive the scored sample")
    _exact(document["output"], {"sha256", "bytes", "bounded"}, "process output")
    if (
        document["output"]["sha256"] != payload["output_sha256"]
        or document["output"]["bounded"] is not True
        or document["output"]["bytes"] <= 0
    ):
        raise SpecValidationError("process output is unbound or unbounded")
