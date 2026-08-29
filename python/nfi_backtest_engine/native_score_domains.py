"""Strict parsing of machine-consumed Native score proof documents."""

from __future__ import annotations

import base64
from typing import Any

from .errors import SpecValidationError
from .native_score_certification_domains import (
    PROCESS_EVIDENCE_VERSION,
    REPLAY_EVIDENCE_VERSION,
    validate_certification_input,
)
from .native_score_domain_identity import (
    VerificationClockPolicy,
    producer_identity,
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
from .specs import NATIVE_SCORE_DOMAIN_EVIDENCE_SCHEMA, validate_schema

DOMAIN_EVIDENCE_VERSION = "native-score-domain-evidence-v1"

__all__ = [
    "DOMAIN_EVIDENCE_VERSION",
    "PROCESS_EVIDENCE_VERSION",
    "REPLAY_EVIDENCE_VERSION",
    "producer_identity",
    "validate_domain_input",
]


def validate_domain_input(
    document: Any,
    *,
    record: dict[str, Any],
    field: str,
    registry_identity: dict[str, Any],
    verification_clock: VerificationClockPolicy,
) -> None:
    """Reject opaque files and validate one domain document against its signed leaf."""
    if record["record_type"] != "portfolio_certificate" or field != "certificate_sha256":
        validate_schema(document, NATIVE_SCORE_DOMAIN_EVIDENCE_SCHEMA)
    if record["record_type"] in {
        "portfolio_certificate",
        "performance_process_sample",
    }:
        validate_certification_input(
            document,
            record=record,
            field=field,
            verification_clock=verification_clock,
        )
        return
    required = {
        "schema_version",
        "document_type",
        "producer",
        "context",
        "observation",
    }
    _exact(document, required, "score domain evidence")
    if document["schema_version"] != DOMAIN_EVIDENCE_VERSION:
        raise SpecValidationError("score domain evidence schema version differs")
    expected_type = _document_type(record["record_type"])
    if document["document_type"] != expected_type:
        raise SpecValidationError("score domain evidence type differs")
    _validate_producer(
        document["producer"],
        record=record,
        verification_clock=verification_clock,
    )
    if document["context"] != _context(record):
        raise SpecValidationError("score domain evidence context differs")
    observation = document["observation"]
    _validate_observation(
        observation,
        record=record,
        registry_identity=registry_identity,
    )


def _document_type(record_type: str) -> str:
    if record_type == "producer_run":
        return "observer-trace"
    if record_type == "execution_trace":
        return "native-execution-trace"
    if record_type == "semantic_obligation":
        return "semantic-obligation-coverage"
    if record_type in {"obligation_coverage", "changed_target", "mcdc_term", "transition"}:
        return "coverage-witness"
    if record_type in {"vector", "decision", "callback", "state_delta"}:
        return "exact-comparison"
    if record_type == "execution_state":
        return "complete-state-trace"
    if record_type in {"generative_case", "metamorphic_case"}:
        return "generated-corpus"
    if record_type == "mutant_outcome":
        return "mutation-execution"
    raise SpecValidationError(f"score domain evidence is unsupported for {record_type}")


def _validate_observation(
    observation: Any,
    *,
    record: dict[str, Any],
    registry_identity: dict[str, Any],
) -> None:
    record_type = record["record_type"]
    if record_type == "semantic_obligation":
        _exact(
            observation,
            {
                "registry_fingerprint",
                "obligation_count",
                "ordered_coverage_encoding",
                "coverage_bits_base64",
            },
            "semantic coverage observation",
        )
        count = registry_identity["total_obligations"]
        if (
            observation["registry_fingerprint"] != registry_identity["registry_fingerprint"]
            or observation["obligation_count"] != count
            or observation["ordered_coverage_encoding"] != "registry-order-bitset-v1"
        ):
            raise SpecValidationError("semantic coverage does not identify the current registry")
        try:
            bits = base64.b64decode(observation["coverage_bits_base64"], validate=True)
        except (TypeError, ValueError) as exc:
            raise SpecValidationError("semantic coverage bitset is malformed") from exc
        expected_bytes = (count + 7) // 8
        if len(bits) != expected_bytes:
            raise SpecValidationError("semantic coverage cardinality differs from registry")
        if any(byte != 0xFF for byte in bits[:-1]):
            raise SpecValidationError("semantic registry contains an uncovered obligation")
        tail_bits = count % 8
        expected_tail = 0xFF if tail_bits == 0 else (1 << tail_bits) - 1
        if not bits or bits[-1] != expected_tail:
            raise SpecValidationError("semantic registry contains an uncovered obligation")
        return
    shapes = {
        "producer_run": {"events", "complete"},
        "execution_trace": {"events", "complete"},
        "obligation_coverage": {"semantic_id", "observations"},
        "changed_target": {"semantic_id", "observations"},
        "mcdc_term": {"semantic_id", "observations"},
        "transition": {"semantic_id", "observations"},
        "vector": {"semantic_id", "records"},
        "decision": {"semantic_id", "records"},
        "callback": {"semantic_id", "records"},
        "state_delta": {"semantic_id", "records"},
        "execution_state": {"semantic_id", "state_fields", "events"},
        "generative_case": {"domain", "case_id", "seed", "records"},
        "metamorphic_case": {"domain", "case_id", "seed", "relation", "records"},
        "mutant_outcome": {"mutant_id", "operator", "executions"},
    }
    required = shapes.get(record_type)
    if required is None:
        raise SpecValidationError("score domain evidence observation type is unsupported")
    _exact(observation, required, f"{record_type} domain observation")
    collection = next(
        (
            observation[name]
            for name in ("events", "observations", "records", "executions")
            if name in observation
        ),
        None,
    )
    if not isinstance(collection, list) or not collection:
        raise SpecValidationError(f"{record_type} domain observation has no machine records")
    for index, item in enumerate(collection):
        if not isinstance(item, dict) or item.get("sequence") != index:
            raise SpecValidationError(f"{record_type} domain observation sequence differs")
    payload = record["payload"]
    if record_type == "producer_run":
        for item in collection:
            _exact(item, {"sequence", "event_sha256"}, "observer event")
    elif record_type == "execution_trace":
        for item in collection:
            _exact(item, {"sequence", "kind", "module", "route"}, "native trace event")
        if observation["events"] != payload["events"]:
            raise SpecValidationError("native execution trace events differ")
    elif record_type in {"obligation_coverage", "changed_target", "mcdc_term", "transition"}:
        if observation["semantic_id"] != payload["target_id"]:
            raise SpecValidationError("coverage witness semantic identity differs")
        for item in collection:
            _exact(item, {"sequence", "witness_sha256", "reached"}, "coverage witness")
            if item["reached"] is not True:
                raise SpecValidationError("coverage witness did not reach its obligation")
    elif record_type in {"vector", "decision", "callback", "state_delta"}:
        if observation["semantic_id"] != payload["comparison_id"]:
            raise SpecValidationError("comparison semantic identity differs")
        for item in collection:
            _exact(item, {"sequence", "value_sha256"}, "comparison record")
    elif record_type == "execution_state":
        if (
            observation["semantic_id"] != payload["event_id"]
            or observation["state_fields"] != payload["state_fields"]
        ):
            raise SpecValidationError("complete state trace identity or fields differ")
        for item in collection:
            _exact(item, {"sequence", "value_sha256"}, "complete state event")
    elif record_type in {"generative_case", "metamorphic_case"}:
        if observation["case_id"] != payload["case_id"] or observation["seed"] != payload["seed"]:
            raise SpecValidationError("generated corpus case identity differs")
        if record_type == "metamorphic_case" and observation["relation"] != payload["relation"]:
            raise SpecValidationError("metamorphic relation differs")
        for item in collection:
            _exact(item, {"sequence", "value_sha256"}, "generated corpus record")
    else:
        if (
            observation["mutant_id"] != payload["mutant_id"]
            or observation["operator"] != payload["operator"]
        ):
            raise SpecValidationError("mutation execution identity differs")
        for item in collection:
            _exact(item, {"sequence", "result_sha256"}, "mutation execution")
