"""Authenticated producer and signed run identity for score domain documents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from .errors import SpecValidationError
from .release_provenance import canonical_sha256

_MAX_PRODUCER_LIFETIME_SECONDS: Final = 24 * 60 * 60
_MAX_VERIFICATION_CLOCK_SKEW: Final = timedelta(minutes=5)
_PRODUCER_ROLE_BY_TYPE: Final = {
    "identity_component": "build-identity-observer",
    "provenance_identity": "provenance-verifier",
    "producer_run": "independence-auditor",
    "execution_trace": "process-tracer",
    "semantic_obligation": "semantic-registry-verifier",
    "obligation_coverage": "coverage-runner",
    "changed_target": "changed-target-runner",
    "mcdc_term": "mcdc-runner",
    "transition": "transition-runner",
    "vector": "oracle-comparator",
    "decision": "oracle-comparator",
    "callback": "callback-comparator",
    "state_delta": "state-comparator",
    "execution_state": "complete-state-comparator",
    "generative_case": "corpus-runner",
    "metamorphic_case": "metamorphic-runner",
    "mutant_outcome": "mutation-runner",
    "portfolio_certificate": "portfolio-certificate-verifier",
    "performance_process_sample": "process-resource-meter",
    "full_x7_performance_certificate": "full-x7-performance-certificate-verifier",
}


@dataclass(frozen=True, slots=True)
class VerificationClockPolicy:
    """One score-boundary clock with five minutes of distributed-run clock skew."""

    now: datetime
    maximum_clock_skew: timedelta = _MAX_VERIFICATION_CLOCK_SKEW

    def __post_init__(self) -> None:
        if self.now.tzinfo is None or self.maximum_clock_skew != _MAX_VERIFICATION_CLOCK_SKEW:
            raise SpecValidationError("Native score verification clock policy is invalid")

    @classmethod
    def capture(cls) -> VerificationClockPolicy:
        """Capture the verifier clock exactly once for one score evaluation."""
        return cls(now=datetime.now(UTC))


def native_score_producer_role(record_type: str) -> str:
    """Return the sole producer role authorized for a raw record domain."""
    try:
        return _PRODUCER_ROLE_BY_TYPE[record_type]
    except KeyError as exc:
        raise SpecValidationError(f"unknown native score raw record type: {record_type}") from exc


def producer_identity(
    *,
    role: str,
    candidate_identity_sha256: str,
    workload_sha256: str,
    run_id: str,
    nonce: str,
    issued_at: str,
    expires_at: str,
) -> str:
    """Derive the authenticated producer identity from its complete run preimage."""
    return canonical_sha256(
        {
            "role": role,
            "candidate_identity_sha256": candidate_identity_sha256,
            "workload_sha256": workload_sha256,
            "run_id": run_id,
            "nonce": nonce,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
    )


def validate_producer(
    producer: Any,
    *,
    record: dict[str, Any],
    verification_clock: VerificationClockPolicy,
) -> None:
    """Bind authorized role, candidate, freshness, run, and nonce to a score leaf."""
    exact_fields(
        producer,
        {"role", "identity_sha256", "issued_at", "expires_at", "run_id", "nonce"},
        "domain producer",
    )
    issued = _timestamp(producer["issued_at"], "issued_at")
    expires = _timestamp(producer["expires_at"], "expires_at")
    if issued > verification_clock.now + verification_clock.maximum_clock_skew:
        raise SpecValidationError("domain producer evidence is issued in the future")
    if (
        expires <= issued
        or (expires - issued).total_seconds() > _MAX_PRODUCER_LIFETIME_SECONDS
        or verification_clock.now >= expires
    ):
        raise SpecValidationError("domain producer evidence is stale")
    authorized_role = native_score_producer_role(record["record_type"])
    if producer["role"] != authorized_role:
        raise SpecValidationError("domain producer role is unauthorized")
    expected = producer_identity(
        role=producer["role"],
        candidate_identity_sha256=record["candidate_identity_sha256"],
        workload_sha256=record["workload_sha256"],
        run_id=record["run_id"],
        nonce=record["nonce"],
        issued_at=producer["issued_at"],
        expires_at=producer["expires_at"],
    )
    if (
        producer["identity_sha256"] != expected
        or producer["run_id"] != record["run_id"]
        or producer["nonce"] != record["nonce"]
    ):
        raise SpecValidationError("domain producer identity is unauthenticated")


def score_context(record: dict[str, Any]) -> dict[str, Any]:
    """Project the exact signed candidate/run context shared by every domain."""
    return {
        field: record[field]
        for field in (
            "source_identity_sha256",
            "candidate_identity_sha256",
            "mode_contract",
            "platform",
            "workload_sha256",
            "run_id",
            "nonce",
        )
    }


def exact_fields(value: Any, fields: set[str], label: str) -> None:
    """Require a machine document to have exactly its versioned contract fields."""
    if not isinstance(value, dict) or set(value) != fields:
        raise SpecValidationError(f"{label} fields differ")


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise SpecValidationError(f"domain producer {label} is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SpecValidationError(f"domain producer {label} is malformed") from exc
    if parsed.tzinfo is None:
        raise SpecValidationError(f"domain producer {label} is malformed")
    return parsed.astimezone(UTC)
