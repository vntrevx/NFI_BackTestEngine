"""Full X7 certificate, replay, and raw process score evidence parsing."""

from __future__ import annotations

import math
import re
import statistics
from functools import cache
from typing import Any, cast

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
from .semantic_registry import load_immutable_packaged_semantic_registry_for_offline_audit
from .specs import FULL_X7_CERTIFICATION_V2_SCHEMA, validate_schema

PROCESS_EVIDENCE_VERSION = "native-score-process-evidence-v1"
REPLAY_EVIDENCE_VERSION = "full-x7-certificate-replay-v1"
PERFORMANCE_CERTIFICATE_RECORD_TYPE = "full_x7_performance_certificate"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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
    if record["record_type"] == PERFORMANCE_CERTIFICATE_RECORD_TYPE:
        _validate_performance_certificate(document, record=record, field=field)
        return
    _validate_process_document(
        document,
        record=record,
        verification_clock=verification_clock,
    )


def _validate_performance_certificate(
    document: Any,
    *,
    record: dict[str, Any],
    field: str,
) -> None:
    """Recompute a v2 performance observation from one real Full X7 certificate."""

    if field != "certificate_sha256":
        raise SpecValidationError("Full X7 performance input field differs")
    payload = record["payload"]
    expected = performance_observation_from_certificate(
        document,
        certificate_sha256=payload["certificate_sha256"],
        mode_contract=record["mode_contract"],
        record=record,
    )
    if payload != expected:
        raise SpecValidationError("Full X7 performance observation differs from certificate")


def performance_observation_from_certificate(
    document: Any,
    *,
    certificate_sha256: str,
    mode_contract: str,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open one Full X7 certificate and recompute its score performance observation."""

    if _SHA256_PATTERN.fullmatch(certificate_sha256) is None:
        raise SpecValidationError("Full X7 performance certificate digest is malformed")
    validate_schema(document, FULL_X7_CERTIFICATION_V2_SCHEMA)
    if not isinstance(document, dict):
        raise SpecValidationError("Full X7 performance certificate must be an object")
    gates = _validate_certificate_source_and_lock(
        document,
        mode_contract=mode_contract,
        record=record,
    )
    if (
        document.get("status") != "certified"
        or document.get("release_certified") is not True
        or any(not isinstance(gate, dict) or gate.get("met") is not True for gate in gates.values())
    ):
        raise SpecValidationError("Full X7 performance certificate is failed or cross-mode")
    measurement = document.get("measurement")
    installed = gates.get("installed_wheel")
    determinism = gates.get("determinism")
    speed = gates.get("speed")
    memory = gates.get("memory")
    swap = gates.get("swap")
    if not all(
        isinstance(item, dict)
        for item in (measurement, installed, determinism, speed, memory, swap)
    ):
        raise SpecValidationError("Full X7 performance certificate evidence is incomplete")
    measurement = cast(dict[str, Any], measurement)
    installed = cast(dict[str, Any], installed)
    determinism = cast(dict[str, Any], determinism)
    speed = cast(dict[str, Any], speed)
    memory = cast(dict[str, Any], memory)
    swap = cast(dict[str, Any], swap)
    native_sha256 = installed.get("native_member_sha256")
    engine_build = document.get("environment", {}).get("engine_build")
    if (
        installed.get("installed_extension_equal") is not True
        or installed.get("installed_extension_sha256") != native_sha256
        or not isinstance(engine_build, dict)
        or engine_build.get("binary_sha256") != native_sha256
    ):
        raise SpecValidationError("Full X7 performance certificate package identity differs")

    runs = document.get("runs")
    if not isinstance(runs, dict):
        raise SpecValidationError("Full X7 performance run evidence is incomplete")
    engine_runs = runs.get("engine")
    cold_run = runs.get("cold_seed")
    official_run = runs.get("official_reference")
    engine_summary = runs.get("engine_summary")
    cold_summary = runs.get("cold_seed_summary")
    official_summary = runs.get("official_reference_summary")
    if (
        not isinstance(engine_runs, list)
        or not engine_runs
        or not all(isinstance(run, dict) for run in engine_runs)
        or not all(
            isinstance(item, dict)
            for item in (cold_run, official_run, engine_summary, cold_summary, official_summary)
        )
    ):
        raise SpecValidationError("Full X7 performance run evidence is incomplete")
    engine_runs = cast(list[dict[str, Any]], engine_runs)
    cold_run = cast(dict[str, Any], cold_run)
    official_run = cast(dict[str, Any], official_run)
    engine_summary = cast(dict[str, Any], engine_summary)
    cold_summary = cast(dict[str, Any], cold_summary)
    official_summary = cast(dict[str, Any], official_summary)
    if (
        measurement.get("native_measured_repetitions") != len(engine_runs)
        or measurement.get("official_reference_repetitions") != 1
        or measurement.get("native_lane") != "preserved-vector-reuse"
        or _positive_int(
            measurement.get("cold_seed_repetitions"), "cold-seed repetitions"
        )
        != 1
    ):
        raise SpecValidationError("Full X7 performance repetition evidence differs")

    engine_walls = [
        _positive_number(run.get("wall_time_seconds"), "Native wall time")
        for run in engine_runs
    ]
    cold_wall = _positive_number(cold_run.get("wall_time_seconds"), "cold-seed wall time")
    official_wall = _positive_number(
        official_run.get("wall_time_seconds"), "Official wall time"
    )
    engine_median = statistics.median(engine_walls)
    _validate_wall_summary(engine_summary, engine_walls, label="Native")
    _validate_wall_summary(cold_summary, [cold_wall], label="cold-seed")
    _validate_wall_summary(official_summary, [official_wall], label="Official")
    spread = (max(engine_walls) - min(engine_walls)) / engine_median
    speedup = official_wall / engine_median
    if not math.isclose(
        _nonnegative_number(measurement.get("engine_relative_spread"), "Native spread"),
        spread,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise SpecValidationError("Full X7 performance spread summary differs")
    if not math.isclose(
        _positive_number(speed.get("observed_speedup"), "Native speedup"),
        speedup,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise SpecValidationError("Full X7 performance speed summary differs")

    engine_peak = _summary_peak(engine_summary, "peak_rss_bytes", label="Native RSS")
    cold_peak = _summary_peak(cold_summary, "peak_rss_bytes", label="cold-seed RSS")
    if engine_peak < max(
        _nonnegative_int(run.get("peak_rss_bytes"), "Native RSS") for run in engine_runs
    ):
        raise SpecValidationError("Full X7 performance Native RSS summary understates a run")
    if cold_peak < _nonnegative_int(cold_run.get("peak_rss_bytes"), "cold-seed RSS"):
        raise SpecValidationError("Full X7 performance cold-seed RSS summary understates a run")
    observed_peak = max(engine_peak, cold_peak)

    engine_swap = _summary_swap_peak(engine_summary, engine_runs, label="Native swap")
    cold_swap = _summary_swap_peak(cold_summary, [cold_run], label="cold-seed swap")
    official_swap = _summary_swap_peak(
        official_summary, [official_run], label="Official swap"
    )
    native_swap = max(engine_swap, cold_swap)
    profile = document.get("environment", {}).get("execution_profile")
    if not isinstance(profile, dict):
        raise SpecValidationError("Full X7 performance resource profile is incomplete")
    memory_limit = _positive_int(profile.get("working_memory_bytes"), "sealed memory cap")
    swap_limit = _positive_int(profile.get("swap_cap_bytes"), "sealed swap cap")
    gate_memory_limit = _positive_int(memory.get("limit_bytes"), "memory gate limit")
    gate_memory_peak = _nonnegative_int(
        memory.get("observed_peak_bytes"), "memory gate peak"
    )
    if gate_memory_limit != memory_limit or gate_memory_peak != observed_peak:
        raise SpecValidationError("Full X7 performance memory gate differs from measurements")
    gate_swap_limit = _positive_int(swap.get("limit_bytes"), "swap gate limit")
    gate_native_swap = _nonnegative_int(
        swap.get("native_observed_peak_bytes"), "Native swap gate peak"
    )
    gate_official_swap = _nonnegative_int(
        swap.get("official_observed_peak_bytes"), "Official swap gate peak"
    )
    if (
        gate_swap_limit != swap_limit
        or gate_native_swap != native_swap
        or gate_official_swap != official_swap
    ):
        raise SpecValidationError("Full X7 performance swap gate differs from measurements")

    hashes = [
        cold_run.get("result_sha256"),
        *(run.get("result_sha256") for run in engine_runs),
        official_run.get("result_sha256"),
    ]
    determinism_met = all(
        isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None
        for value in hashes
    ) and len(set(hashes)) == 1
    if determinism.get("met") is not determinism_met:
        raise SpecValidationError("Full X7 performance determinism gate differs from runs")

    return {
        "certificate_sha256": certificate_sha256,
        "wheel_sha256": installed.get("sha256"),
        "native_extension_sha256": native_sha256,
        "official_reference_repetitions": measurement.get(
            "official_reference_repetitions"
        ),
        "native_initial_repetitions": measurement.get("native_initial_repetitions"),
        "native_measured_repetitions": measurement.get("native_measured_repetitions"),
        "native_maximum_repetitions": measurement.get("native_maximum_repetitions"),
        "native_spread_threshold": measurement.get("native_spread_threshold"),
        "engine_relative_spread": spread,
        "preserved_vector_speedup": speedup,
        "determinism_met": determinism_met,
        "memory_limit_bytes": memory_limit,
        "observed_peak_rss_bytes": observed_peak,
        "swap_limit_bytes": swap_limit,
        "native_observed_peak_swap_bytes": native_swap,
        "official_observed_peak_swap_bytes": official_swap,
    }


def _validate_wall_summary(summary: dict[str, Any], values: list[float], *, label: str) -> None:
    wall = summary.get("wall_time_seconds")
    expected = {
        "minimum": min(values),
        "median": statistics.median(values),
        "maximum": max(values),
    }
    if not isinstance(wall, dict) or set(wall) != set(expected):
        raise SpecValidationError(f"Full X7 performance {label} wall summary differs")
    actual = {
        field: _positive_number(wall.get(field), f"{label} wall summary {field}")
        for field in expected
    }
    if actual != expected:
        raise SpecValidationError(f"Full X7 performance {label} wall summary differs")


def _summary_peak(summary: dict[str, Any], field: str, *, label: str) -> int:
    value = summary.get(field)
    if not isinstance(value, dict):
        raise SpecValidationError(f"Full X7 performance {label} summary is incomplete")
    return _nonnegative_int(value.get("maximum"), label)


def _summary_swap_peak(
    summary: dict[str, Any], runs: list[dict[str, Any]], *, label: str
) -> int:
    value = summary.get("peak_swap_bytes")
    peaks = [_nonnegative_int(run.get("peak_swap_bytes"), label) for run in runs]
    if not isinstance(value, dict) or value.get("measurements_complete") is not True:
        raise SpecValidationError(f"Full X7 performance {label} summary differs")
    maximum = _nonnegative_int(value.get("maximum"), label)
    if maximum != max(peaks):
        raise SpecValidationError(f"Full X7 performance {label} summary differs")
    return maximum


def _positive_number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SpecValidationError(f"Full X7 performance {label} is malformed")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise SpecValidationError(f"Full X7 performance {label} is malformed")
    return number


def _nonnegative_number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SpecValidationError(f"Full X7 performance {label} is malformed")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise SpecValidationError(f"Full X7 performance {label} is malformed")
    return number


def _nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SpecValidationError(f"Full X7 performance {label} is malformed")
    return value


def _positive_int(value: Any, label: str) -> int:
    number = _nonnegative_int(value, label)
    if number == 0:
        raise SpecValidationError(f"Full X7 performance {label} is malformed")
    return number


def _validate_certificate_source_and_lock(
    document: dict[str, Any],
    *,
    mode_contract: str,
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bind a Full X7 certificate to the packaged source and its own sealed input lock."""

    source = _portfolio_source_identity()
    inputs = document.get("inputs")
    claim_scope = document.get("claim_scope")
    gates = document.get("gates")
    if not all(isinstance(item, dict) for item in (inputs, claim_scope, gates)):
        raise SpecValidationError("Full X7 certificate source identity differs")
    inputs = cast(dict[str, Any], inputs)
    claim_scope = cast(dict[str, Any], claim_scope)
    gates = cast(dict[str, Any], gates)
    if (
        (
            record is not None
            and source["source_closure_sha256"] != record.get("source_identity_sha256")
        )
        or claim_scope.get("strategy") != source["strategy_class"]
        or claim_scope.get("upstream_commit") != source["upstream_commit"]
        or claim_scope.get("mode_contract") != mode_contract
        or inputs.get("strategy_sha256") != source["strategy_sha256"]
        or inputs.get("mode_contract") != mode_contract
    ):
        raise SpecValidationError("Full X7 certificate source identity differs")
    input_lock = gates.get("input_lock")
    release_lock = inputs.get("release_lock")
    if (
        not isinstance(input_lock, dict)
        or not isinstance(release_lock, dict)
        or not all(
            isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None
            for value in (
                input_lock.get("identity_sha256"),
                release_lock.get("identity_sha256"),
            )
        )
        or input_lock.get("identity_sha256") != release_lock.get("identity_sha256")
    ):
        raise SpecValidationError("Full X7 certificate sealed input identity differs")
    if "native_score_identity" in inputs and (
        record is None or inputs["native_score_identity"] != _context(record)
    ):
        raise SpecValidationError("Full X7 certificate score identity differs")
    return gates


def _validate_portfolio_document(
    document: Any,
    *,
    record: dict[str, Any],
    field: str,
) -> None:
    if field == "certificate_sha256":
        validate_schema(document, FULL_X7_CERTIFICATION_V2_SCHEMA)
        if not isinstance(document, dict):
            raise SpecValidationError("Full X7 certificate must be an object")
        gates = document.get("gates")
        if (
            document.get("status") != "certified"
            or document.get("release_certified") is not True
            or document.get("claim_scope", {}).get("pair_count") != 80
            or not isinstance(gates, dict)
            or not gates
            or any(
                not isinstance(gate, dict) or gate.get("met") is not True for gate in gates.values()
            )
        ):
            raise SpecValidationError("Full X7 certificate is failed, incomplete, or cross-mode")
        gates = _validate_certificate_source_and_lock(
            document,
            mode_contract=record["mode_contract"],
            record=record,
        )
        installed = gates.get("installed_wheel")
        engine_build = document["environment"].get("engine_build")
        payload = record["payload"]
        native_sha256 = payload.get("native_extension_sha256")
        if (
            not isinstance(installed, dict)
            or not isinstance(engine_build, dict)
            or payload.get("candidate_sha256") != record["candidate_identity_sha256"]
            or payload.get("replay_candidate_sha256") != record["candidate_identity_sha256"]
            or installed.get("sha256") != payload.get("wheel_sha256")
            or installed.get("native_member_sha256") != native_sha256
            or installed.get("installed_extension_sha256") != native_sha256
            or installed.get("installed_extension_equal") is not True
            or engine_build.get("binary_sha256") != native_sha256
        ):
            raise SpecValidationError("Full X7 certificate candidate package identity differs")
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


@cache
def _portfolio_source_identity() -> dict[str, str]:
    """Open the immutable strategy closure authorized by portfolio score evidence."""

    registry = load_immutable_packaged_semantic_registry_for_offline_audit()
    return {
        "source_closure_sha256": str(registry["source_closure"]["merkle_root"]),
        "strategy_class": str(registry["strategy"]["selected_class"]),
        "strategy_sha256": str(registry["strategy"]["source"]["sha256"]),
        "upstream_commit": str(registry["strategy"]["upstream"]["observed_commit"]),
    }


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
