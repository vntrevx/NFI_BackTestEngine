"""Fail-closed evaluation of the versioned Native ten-point scorecard."""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Any, Final

from .canonical import read_json, write_json
from .errors import SpecValidationError
from .fixture import sha256_file
from .native_score_certification_domains import PERFORMANCE_CERTIFICATE_RECORD_TYPE
from .native_score_domain_identity import (
    VerificationClockPolicy,
    native_score_producer_role,
)
from .native_score_domains import validate_domain_input
from .platform_benchmark import REQUIRED_PLATFORM_SYSTEMS
from .product_support_contract import load_product_support_contract
from .release_contract import FUTURES_RELEASE_CONTRACT_ID, SPOT_RELEASE_CONTRACT_ID
from .release_provenance import (
    DEFAULT_PROVENANCE_POLICY,
    ProvenancePolicy,
    canonical_sha256,
    verify_embedded_platform_evidence,
)
from .semantic_registry import (
    CurrentRefAuthorization,
    begin_packaged_semantic_registry_authorization,
    finalize_packaged_semantic_registry_authorization,
    load_immutable_packaged_semantic_registry_for_offline_audit,
    packaged_semantic_obligation_registry_identity,
)
from .specs import (
    NATIVE_SCORE_DOMAIN_EVIDENCE_SCHEMA,
    NATIVE_SCORE_MACHINE_RECORD_SCHEMA,
    NATIVE_SCORE_MACHINE_RECORD_V2_SCHEMA,
    NATIVE_SCORE_RAW_EVIDENCE_SCHEMA,
    NATIVE_SCORE_RAW_EVIDENCE_V2_SCHEMA,
    NATIVE_SCORECARD_SCHEMA,
    validate_schema,
)

NATIVE_SCORECARD_VERSION = "1.0.0"
NATIVE_SCORE_EVIDENCE_VERSION = "1.0.0"
NATIVE_SCORE_RAW_RECORD_VERSION = "1.0.0"
NATIVE_SCORE_RECORD_ID_VERSION = "native-score-record-id-v1"
NATIVE_SCORE_EVALUATOR_VERSION = "native-score-evaluator-v3"
NATIVE_SCORE_PERFORMANCE_EVIDENCE_VERSION = "2.0.0"
NATIVE_SCORE_PERFORMANCE_EVALUATOR_VERSION = "native-score-performance-evaluator-v2"
NATIVE_SCORE_PERFORMANCE_RECORD_TYPE = PERFORMANCE_CERTIFICATE_RECORD_TYPE
NATIVE_SCORE_EVALUATION_OPERATION: Final = "native-score-evaluation"
PRODUCT_CANDIDATE_CREATE_OPERATION: Final = "product-candidate-create"
PRODUCT_STABLE_CREATE_OPERATION: Final = "product-stable-create"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_FIELDS = (
    "source_closure_sha256",
    "engine_artifact_sha256",
    "oracle_sha256",
    "scope_sha256",
)
_AUTHORITY_FIELDS = ("bundle_id", "challenge")
_TRUSTED_SCORE_SCHEMA_IDENTITIES = {
    NATIVE_SCORE_DOMAIN_EVIDENCE_SCHEMA: (
        7_579,
        "dc7efaeb493709dec7ba6b289a983ceceaf32ac91be77f99f800612bb9b485e4",
    ),
    NATIVE_SCORE_RAW_EVIDENCE_SCHEMA: (
        16_264,
        "97850b7123bd84d3b8a9d9948af3bb4dcacb38893c53a1a8e18fffa6d250803e",
    ),
    NATIVE_SCORE_MACHINE_RECORD_SCHEMA: (
        3_207,
        "e0793fae08949e181aebd328eafa0750446cc50aece1251dd01ebb2a7997c6da",
    ),
    NATIVE_SCORE_RAW_EVIDENCE_V2_SCHEMA: (
        4_365,
        "a7e6bb5a2e3d71ba248a5337226fecec1aea1997a108723a129154462e03177c",
    ),
    NATIVE_SCORE_MACHINE_RECORD_V2_SCHEMA: (
        2_241,
        "a4de042922b709a95b2fc45837e3d9666023e4932d6db25559509557f6a4cf33",
    ),
}
_REQUIRED_MODE_CONTRACTS = frozenset({SPOT_RELEASE_CONTRACT_ID, FUTURES_RELEASE_CONTRACT_ID})
RecordEvaluator = Callable[[list[dict[str, Any]], dict[str, str]], list[str]]


@dataclass(frozen=True, slots=True)
class NativeScoreGate:
    """One binary point recomputed from typed raw machine records."""

    gate_id: str
    record_types: tuple[str, ...]
    evaluate_records: RecordEvaluator


def native_score_record_preimage(record: dict[str, Any]) -> dict[str, Any]:
    """Return the complete canonical semantic preimage for one raw record ID."""
    fields = {key: value for key, value in record.items() if key != "record_id"}
    source = fields.get("source_artifact")
    if isinstance(source, dict):
        fields["source_artifact"] = {"sha256": source.get("sha256")}
    evaluator_version = (
        NATIVE_SCORE_PERFORMANCE_EVALUATOR_VERSION
        if record.get("schema_version") == NATIVE_SCORE_PERFORMANCE_EVIDENCE_VERSION
        else NATIVE_SCORE_EVALUATOR_VERSION
    )
    return {
        "record_id_version": NATIVE_SCORE_RECORD_ID_VERSION,
        "evaluator_version": evaluator_version,
        **fields,
    }


def native_score_record_id(record: dict[str, Any]) -> str:
    """Derive a raw record ID from its complete canonical preimage."""
    return canonical_sha256(native_score_record_preimage(record))


def _semantic_identity(record: dict[str, Any]) -> str:
    payload = record["payload"]
    record_type = record["record_type"]
    if record_type == "identity_component":
        return f"identity:{payload.get('component')}"
    if record_type == "provenance_identity":
        return "provenance:release"
    for field in (
        "obligation_id",
        "target_id",
        "comparison_id",
        "event_id",
        "case_id",
        "mutant_id",
    ):
        if field in payload:
            return f"{record_type}:{payload[field]}"
    if record_type == "performance_process_sample":
        return (
            f"performance:{payload.get('runtime')}:{payload.get('population')}:"
            f"{payload.get('sample_index')}"
        )
    if record_type == NATIVE_SCORE_PERFORMANCE_RECORD_TYPE:
        return f"performance-certificate:{record['mode_contract']}"
    return f"{record_type}:{record['mode_contract']}:{record['platform']}"


@cache
def _offline_nonpromotional_semantic_registry_identity() -> dict[str, Any]:
    """Audit immutable package obligations without caching any live-current claim."""
    manifest = packaged_semantic_obligation_registry_identity()
    registry = load_immutable_packaged_semantic_registry_for_offline_audit()
    seen: set[str] = set()
    failures: list[str] = []
    count = 0
    for group in registry["obligation_groups"]:
        mapping = group["mapping"]
        if mapping in {"unknown", "unclassified", "official-only-blocker"}:
            failures.append("semantic_obligation_not_closed")
        for obligation in group["obligations"]:
            obligation_id = obligation["obligation_id"]
            if obligation_id in seen:
                raise SpecValidationError("packaged semantic obligation registry has duplicate IDs")
            seen.add(obligation_id)
            count += 1
    if count != registry["summary"]["total_obligations"]:
        raise SpecValidationError("packaged semantic obligation registry count differs")
    return {
        "compressed_sha256": manifest["compressed_sha256"],
        "uncompressed_bytes": manifest["uncompressed_bytes"],
        "uncompressed_sha256": manifest["uncompressed_sha256"],
        "registry_fingerprint": manifest["registry_fingerprint"],
        "total_obligations": count,
        "failures": _unique(failures),
        "native_promotion": False,
    }


def _semantic_registry_fingerprint() -> str:
    return str(
        _offline_nonpromotional_semantic_registry_identity()["registry_fingerprint"]
    )


def _expected_semantic_identities(gate: NativeScoreGate, mode: str, system: str) -> set[str]:
    if gate.gate_id == "immutable_identity_scope":
        return {*(f"identity:{field}" for field in _IDENTITY_FIELDS), "provenance:release"}
    if gate.gate_id == "semantic_closure":
        return {f"semantic_obligation:registry:{_semantic_registry_fingerprint()}"}
    if gate.gate_id == "changed_path_coverage_completeness":
        return {f"{kind}:{kind}:{mode}" for kind in gate.record_types}
    if gate.gate_id == "vector_callback_exactness":
        return {f"{kind}:{kind}:{mode}" for kind in gate.record_types}
    if gate.gate_id == "execution_complete_state_exactness":
        return {f"execution_state:event:{mode}"}
    if gate.gate_id == "generative_metamorphic_mutation_proof":
        return {
            f"generative_case:generative_case:{mode}",
            f"metamorphic_case:metamorphic_case:{mode}",
            f"mutant_outcome:mutant:{mode}",
        }
    if gate.gate_id == "deterministic_performance_resource_proof":
        return {
            f"performance:{runtime}:{population}:{index}"
            for runtime in ("official", "native")
            for population in ("cold", "reuse")
            for index in range(3)
        }
    return {f"{gate.record_types[0]}:{mode}:{system}"}


_MACHINE_INPUT_FIELDS_BY_TYPE = {
    "producer_run": (
        "observer_baseline_sha256",
        "observer_trace_sha256",
    ),
    "execution_trace": ("trace_sha256",),
    "semantic_obligation": ("witness_sha256",),
    "obligation_coverage": ("required_witness_sha256", "observed_witness_sha256"),
    "changed_target": ("required_witness_sha256", "observed_witness_sha256"),
    "mcdc_term": ("required_witness_sha256", "observed_witness_sha256"),
    "transition": ("required_witness_sha256", "observed_witness_sha256"),
    "vector": ("expected_sha256", "actual_sha256"),
    "decision": ("expected_sha256", "actual_sha256"),
    "callback": ("expected_sha256", "actual_sha256"),
    "state_delta": ("expected_sha256", "actual_sha256"),
    "execution_state": (
        "expected_execution_sha256",
        "actual_execution_sha256",
        "expected_state_sha256",
        "actual_state_sha256",
    ),
    "generative_case": ("input_sha256", "expected_sha256", "actual_sha256"),
    "metamorphic_case": ("input_sha256", "expected_sha256", "actual_sha256"),
    "mutant_outcome": (
        "run_sha256",
        "baseline_result_sha256",
        "mutant_result_sha256",
    ),
    "portfolio_certificate": ("certificate_sha256", "replay_result_sha256"),
    "performance_process_sample": ("output_sha256",),
    NATIVE_SCORE_PERFORMANCE_RECORD_TYPE: ("certificate_sha256",),
}

def _payload(record: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    payload = record["payload"]
    _require_exact_fields(payload, required=fields, label=f"{record['record_type']} payload")
    assert isinstance(payload, dict)
    return payload


def _identity_scope(records: list[dict[str, Any]], identity: dict[str, str]) -> list[str]:
    failures: list[str] = []
    for record in records:
        if record["record_type"] == "identity_component":
            payload = _payload(record, {"component", "digest"})
            component = payload["component"]
            if component not in _IDENTITY_FIELDS:
                raise SpecValidationError("identity raw record has unknown component")
            digest = _sha256(payload["digest"], label="identity component digest")
            if digest != identity[component]:
                failures.append("identity_component_mismatch")
        else:
            payload = _payload(
                record,
                {
                    "repository",
                    "repository_ref",
                    "commit",
                    "workflow",
                    "workflow_ref",
                    "job",
                    "bundle_id",
                    "challenge",
                    "candidate_id",
                },
            )
            if not all(
                isinstance(payload[field], str) and payload[field]
                for field in (
                    "repository",
                    "repository_ref",
                    "commit",
                    "workflow",
                    "workflow_ref",
                    "job",
                )
            ):
                raise SpecValidationError("provenance identity raw record is malformed")
            for field in ("bundle_id", "challenge", "candidate_id"):
                _sha256(payload[field], label=f"provenance {field}")
    return _unique(failures)


def _evidence_independence(records: list[dict[str, Any]], _identity: dict[str, str]) -> list[str]:
    failures: list[str] = []
    for record in records:
        payload = _payload(
            record,
            {
                "oracle_producer",
                "oracle_run_id",
                "native_producer",
                "native_run_id",
                "verifier_producer",
                "verifier_run_id",
                "observer_baseline_sha256",
                "observer_trace_sha256",
            },
        )
        producers = [payload[f"{name}_producer"] for name in ("oracle", "native", "verifier")]
        runs = [payload[f"{name}_run_id"] for name in ("oracle", "native", "verifier")]
        if not all(isinstance(value, str) and value for value in (*producers, *runs)):
            raise SpecValidationError("producer/run raw record contains an invalid identity")
        if len(set(producers)) != 3 or len(set(runs)) != 3:
            failures.append("producers_not_independent")
        baseline = _sha256(payload["observer_baseline_sha256"], label="observer baseline")
        trace = _sha256(payload["observer_trace_sha256"], label="observer trace")
        if baseline != trace:
            failures.append("observer_interference")
    return _unique(failures)


def _native_purity(records: list[dict[str, Any]], _identity: dict[str, str]) -> list[str]:
    failures: list[str] = []
    forbidden = {"strategy_python", "fallback", "identity_dispatch"}
    for record in records:
        payload = _payload(record, {"events", "trace_sha256"})
        _sha256(payload["trace_sha256"], label="native execution trace")
        events = payload["events"]
        if not isinstance(events, list) or not events:
            raise SpecValidationError("execution trace raw record requires events")
        for index, event in enumerate(events):
            _require_exact_fields(
                event,
                required={"sequence", "kind", "module", "route"},
                label="execution trace event",
            )
            assert isinstance(event, dict)
            if event["sequence"] != index or not isinstance(event["module"], str):
                raise SpecValidationError("execution trace sequence is malformed")
            if event["kind"] in forbidden:
                failures.append(f"forbidden_execution:{event['kind']}")
            if event["route"] != "native":
                failures.append(f"non_native_route:{event['route']}")
    return _unique(failures)


def _semantic_closure(records: list[dict[str, Any]], _identity: dict[str, str]) -> list[str]:
    authority = _offline_nonpromotional_semantic_registry_identity()
    failures = list(authority["failures"])
    for record in records:
        payload = _payload(
            record,
            {"obligation_id", "registry_sha256", "mapping", "witness_sha256"},
        )
        if payload["obligation_id"] != f"registry:{authority['registry_fingerprint']}":
            raise SpecValidationError("semantic closure record is not the authoritative registry")
        registry_sha256 = _sha256(payload["registry_sha256"], label="semantic registry")
        if registry_sha256 != authority["compressed_sha256"]:
            raise SpecValidationError("semantic closure registry payload identity differs")
        _sha256(payload["witness_sha256"], label="semantic witness")
        if payload["mapping"] in {"unknown", "unclassified", "official-only"}:
            failures.append("semantic_obligation_not_closed")
    return _unique(failures)


def _coverage(records: list[dict[str, Any]], _identity: dict[str, str]) -> list[str]:
    failures: list[str] = []
    for record in records:
        payload = _payload(
            record,
            {"target_id", "required_witness_sha256", "observed_witness_sha256"},
        )
        if not isinstance(payload["target_id"], str) or not payload["target_id"]:
            raise SpecValidationError("coverage target id is malformed")
        required = _sha256(payload["required_witness_sha256"], label="required coverage witness")
        observed = _sha256(payload["observed_witness_sha256"], label="observed coverage witness")
        if observed != required:
            failures.append(f"uncovered:{record['record_type']}")
    return _unique(failures)


def _vector_callback(records: list[dict[str, Any]], _identity: dict[str, str]) -> list[str]:
    failures: list[str] = []
    for record in records:
        payload = _payload(record, {"comparison_id", "expected_sha256", "actual_sha256"})
        if not isinstance(payload["comparison_id"], str) or not payload["comparison_id"]:
            raise SpecValidationError("comparison id is malformed")
        expected = _sha256(payload["expected_sha256"], label="expected comparison")
        actual = _sha256(payload["actual_sha256"], label="actual comparison")
        if expected != actual:
            failures.append(f"mismatch:{record['record_type']}")
    return _unique(failures)


_CANONICAL_EXECUTION_STATE_FIELDS = (
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


def _execution_state(records: list[dict[str, Any]], _identity: dict[str, str]) -> list[str]:
    failures: list[str] = []
    fields = {
        "event_id",
        "expected_execution_sha256",
        "actual_execution_sha256",
        "expected_state_sha256",
        "actual_state_sha256",
        "state_fields",
    }
    for record in records:
        payload = _payload(record, fields)
        state_fields = payload["state_fields"]
        if (
            not isinstance(payload["event_id"], str)
            or not payload["event_id"]
            or not isinstance(state_fields, list)
            or not state_fields
            or not all(isinstance(field, str) and field for field in state_fields)
            or len(state_fields) != len(set(state_fields))
        ):
            raise SpecValidationError("complete-state raw record is malformed")
        if tuple(state_fields) != _CANONICAL_EXECUTION_STATE_FIELDS:
            raise SpecValidationError("complete-state raw record omits canonical contract fields")
        for expected_field, actual_field, failure in (
            ("expected_execution_sha256", "actual_execution_sha256", "execution_mismatch"),
            ("expected_state_sha256", "actual_state_sha256", "complete_state_mismatch"),
        ):
            expected = _sha256(payload[expected_field], label=expected_field)
            actual = _sha256(payload[actual_field], label=actual_field)
            if expected != actual:
                failures.append(failure)
    return _unique(failures)


def _generative_mutation(records: list[dict[str, Any]], _identity: dict[str, str]) -> list[str]:
    failures: list[str] = []
    for record in records:
        kind = record["record_type"]
        if kind in {"generative_case", "metamorphic_case"}:
            fields = {"case_id", "seed", "input_sha256", "expected_sha256", "actual_sha256"}
            if kind == "metamorphic_case":
                fields.add("relation")
            payload = _payload(record, fields)
            if not isinstance(payload["case_id"], str) or not isinstance(payload["seed"], int):
                raise SpecValidationError("generated-case raw record is malformed")
            _sha256(payload["input_sha256"], label="generated input")
            expected = _sha256(payload["expected_sha256"], label="generated expected")
            actual = _sha256(payload["actual_sha256"], label="generated actual")
            if kind == "metamorphic_case" and not isinstance(payload["relation"], str):
                raise SpecValidationError("metamorphic relation is malformed")
            if expected != actual:
                failures.append(f"failed:{kind}")
        else:
            payload = _payload(
                record,
                {
                    "mutant_id",
                    "operator",
                    "run_sha256",
                    "baseline_result_sha256",
                    "mutant_result_sha256",
                },
            )
            if not all(
                isinstance(payload[field], str) and payload[field]
                for field in ("mutant_id", "operator")
            ):
                raise SpecValidationError("mutant raw record is malformed")
            _sha256(payload["run_sha256"], label="mutation run")
            baseline = _sha256(payload["baseline_result_sha256"], label="mutation baseline result")
            mutant = _sha256(payload["mutant_result_sha256"], label="mutant result")
            if mutant == baseline:
                failures.append("mutant_not_killed")
    return _unique(failures)


def _portfolio_platform(records: list[dict[str, Any]], identity: dict[str, str]) -> list[str]:
    failures: list[str] = []
    fields = {
        "certificate_sha256",
        "candidate_sha256",
        "wheel_sha256",
        "native_extension_sha256",
        "replay_result_sha256",
        "replay_candidate_sha256",
    }
    for record in records:
        payload = _payload(record, fields)
        certificate = _sha256(payload["certificate_sha256"], label="portfolio certificate")
        candidate = _sha256(payload["candidate_sha256"], label="portfolio candidate")
        replay_candidate = _sha256(payload["replay_candidate_sha256"], label="replay candidate")
        _sha256(payload["wheel_sha256"], label="certificate wheel")
        _sha256(payload["native_extension_sha256"], label="certificate native extension")
        _sha256(payload["replay_result_sha256"], label="portfolio replay")
        if candidate != identity["engine_artifact_sha256"] or replay_candidate != candidate:
            failures.append("candidate_identity_mismatch")
        if certificate == "0" * 64:
            failures.append("portfolio_certificate_missing")
    return _unique(failures)


def _performance_resources(records: list[dict[str, Any]], _identity: dict[str, str]) -> list[str]:
    if records and records[0]["record_type"] == NATIVE_SCORE_PERFORMANCE_RECORD_TYPE:
        return _full_x7_performance_resources(records)
    failures: list[str] = []
    populations: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    fields = {
        "runtime",
        "population",
        "sample_index",
        "wall_seconds",
        "peak_rss_bytes",
        "memory_limit_bytes",
        "oom",
        "swap_bytes",
        "output_sha256",
    }
    for record in records:
        payload = _payload(record, fields)
        runtime = payload["runtime"]
        population = payload["population"]
        if runtime not in {"official", "native"} or population not in {"cold", "reuse"}:
            raise SpecValidationError("performance process population is unauthorized")
        populations[
            (record["mode_contract"], record["platform"], runtime, population)
        ].append(payload)
    expected_contexts = {
        (mode, system)
        for mode in _REQUIRED_MODE_CONTRACTS
        for system in REQUIRED_PLATFORM_SYSTEMS
    }
    if {(mode, system) for mode, system, _runtime, _population in populations} != expected_contexts:
        raise SpecValidationError("performance process contexts are incomplete")
    medians: dict[tuple[str, str, str, str], float] = {}
    for key, samples in populations.items():
        if sorted(sample["sample_index"] for sample in samples) != [0, 1, 2]:
            raise SpecValidationError("performance process sample indices differ")
        times = [_number_value(sample["wall_seconds"], "process wall time") for sample in samples]
        medians[key] = sorted(times)[1]
        if (max(times) - min(times)) / medians[key] > 0.05:
            failures.append("measurement_spread")
        hashes = [
            _sha256(sample["output_sha256"], label="performance output")
            for sample in samples
        ]
        if len(set(hashes)) != 1:
            failures.append("nondeterministic_outputs")
        for sample in samples:
            peak = _nonnegative_int(sample["peak_rss_bytes"], "peak RSS")
            limit = _nonnegative_int(sample["memory_limit_bytes"], "memory limit")
            swap = _nonnegative_int(sample["swap_bytes"], "swap")
            if not isinstance(sample["oom"], bool):
                raise SpecValidationError("performance process flags are malformed")
            if limit == 0 or peak > limit or sample["oom"]:
                failures.append("memory_limit_not_met")
            if swap != 0:
                failures.append("swap_used")
    for mode, system in expected_contexts:
        if any(
            (mode, system, runtime, population) not in medians
            for runtime in ("official", "native")
            for population in ("cold", "reuse")
        ):
            raise SpecValidationError("performance process populations are incomplete")
        if (
            medians[(mode, system, "official", "cold")]
            / medians[(mode, system, "native", "cold")]
            < 5.0
        ):
            failures.append("cold_start_speed_target_not_met")
        if (
            medians[(mode, system, "official", "reuse")]
            / medians[(mode, system, "native", "reuse")]
            < 10.0
        ):
            failures.append("reuse_speed_target_not_met")
    return _unique(failures)


def _full_x7_performance_resources(records: list[dict[str, Any]]) -> list[str]:
    """Evaluate v2 records derived from the two real Full X7 certificates."""

    contract = load_product_support_contract()["certification"]
    failures: list[str] = []
    for record in records:
        payload = _payload(
            record,
            {
                "certificate_sha256",
                "wheel_sha256",
                "native_extension_sha256",
                "official_reference_repetitions",
                "native_initial_repetitions",
                "native_measured_repetitions",
                "native_maximum_repetitions",
                "native_spread_threshold",
                "engine_relative_spread",
                "preserved_vector_speedup",
                "determinism_met",
                "memory_limit_bytes",
                "observed_peak_rss_bytes",
                "swap_limit_bytes",
                "native_observed_peak_swap_bytes",
                "official_observed_peak_swap_bytes",
            },
        )
        for field in ("certificate_sha256", "wheel_sha256", "native_extension_sha256"):
            _sha256(payload[field], label=field)
        initial = _nonnegative_int(
            payload["native_initial_repetitions"], "initial Native repetitions"
        )
        measured = _nonnegative_int(
            payload["native_measured_repetitions"], "measured Native repetitions"
        )
        maximum = _nonnegative_int(
            payload["native_maximum_repetitions"], "maximum Native repetitions"
        )
        official = _nonnegative_int(
            payload["official_reference_repetitions"], "Official repetitions"
        )
        threshold = _nonnegative_number_value(
            payload["native_spread_threshold"], "spread threshold"
        )
        spread = _nonnegative_number_value(payload["engine_relative_spread"], "Native spread")
        speedup = _number_value(payload["preserved_vector_speedup"], "Native speedup")
        memory_limit = _nonnegative_int(payload["memory_limit_bytes"], "memory limit")
        memory_peak = _nonnegative_int(payload["observed_peak_rss_bytes"], "peak RSS")
        swap_limit = _nonnegative_int(payload["swap_limit_bytes"], "swap limit")
        native_swap = _nonnegative_int(
            payload["native_observed_peak_swap_bytes"], "Native swap peak"
        )
        official_swap = _nonnegative_int(
            payload["official_observed_peak_swap_bytes"], "Official swap peak"
        )
        if official != 1:
            failures.append("official_oracle_repetition_policy_not_met")
        if (
            initial != contract["minimum_native_repetitions"]
            or maximum != contract["maximum_native_repetitions"]
            or measured not in {initial, maximum}
            or threshold != contract["adaptive_spread_threshold"]
            or (spread > threshold and measured != maximum)
        ):
            failures.append("adaptive_native_repetition_policy_not_met")
        if speedup < contract["minimum_native_speedup"]:
            failures.append("reuse_speed_target_not_met")
        if payload["determinism_met"] is not True:
            failures.append("nondeterministic_outputs")
        if (
            contract["memory_cap_required"] is not True
            or memory_limit == 0
            or memory_peak > memory_limit
        ):
            failures.append("memory_limit_not_met")
        if (
            contract["swap_cap_required"] is not True
            or swap_limit == 0
            or native_swap > swap_limit
            or official_swap > swap_limit
        ):
            failures.append("swap_limit_not_met")
    return _unique(failures)


NATIVE_SCORE_GATES = (
    NativeScoreGate(
        "immutable_identity_scope",
        ("identity_component",) * 4 + ("provenance_identity",),
        _identity_scope,
    ),
    NativeScoreGate("evidence_independence", ("producer_run",), _evidence_independence),
    NativeScoreGate("native_purity", ("execution_trace",), _native_purity),
    NativeScoreGate("semantic_closure", ("semantic_obligation",), _semantic_closure),
    NativeScoreGate(
        "changed_path_coverage_completeness",
        ("obligation_coverage", "changed_target", "mcdc_term", "transition"),
        _coverage,
    ),
    NativeScoreGate(
        "vector_callback_exactness",
        ("vector", "decision", "callback", "state_delta"),
        _vector_callback,
    ),
    NativeScoreGate("execution_complete_state_exactness", ("execution_state",), _execution_state),
    NativeScoreGate(
        "generative_metamorphic_mutation_proof",
        ("generative_case", "metamorphic_case", "mutant_outcome"),
        _generative_mutation,
    ),
    NativeScoreGate(
        "same_candidate_portfolio_platform_certification",
        ("portfolio_certificate",),
        _portfolio_platform,
    ),
    NativeScoreGate(
        "deterministic_performance_resource_proof",
        ("performance_process_sample",) * 12,
        _performance_resources,
    ),
)
NATIVE_SCORE_GATE_IDS = tuple(gate.gate_id for gate in NATIVE_SCORE_GATES)


def evaluate_native_scorecard(
    evidence_manifest_path: str | Path,
    *,
    expected_identity_path: str | Path,
    output_path: str | Path | None = None,
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
    authorization_operation: str = NATIVE_SCORE_EVALUATION_OPERATION,
) -> dict[str, Any]:
    """Authenticate raw leaves and recompute the exact binary ten-point verdict."""

    verification_clock = VerificationClockPolicy.capture()
    _verify_score_schema_identities()
    destination = Path(output_path).absolute() if output_path is not None else None
    if destination is not None and destination.exists():
        raise SpecValidationError(f"scorecard output must not already exist: {destination}")
    manifest_path = _plain_file(evidence_manifest_path, label="score evidence manifest")
    manifest = read_json(manifest_path)
    _validate_manifest(manifest)
    assert isinstance(manifest, dict)
    expected_document = read_json(
        _plain_file(expected_identity_path, label="expected score identity")
    )
    expected_identity, expected_authority = _validate_expected_identity(expected_document)
    manifest_identity = _validate_identity(manifest["identity"], label="manifest identity")
    stale_fields = [
        field for field in _IDENTITY_FIELDS if manifest_identity[field] != expected_identity[field]
    ]
    blockers = [f"stale_identity:{field}" for field in stale_fields]

    artifacts, subjects = _load_raw_artifacts(
        manifest,
        root=manifest_path.parent,
        manifest_identity=manifest_identity,
        verification_clock=verification_clock,
    )
    authorization = _verify_signed_subject_graphs(
        manifest,
        root=manifest_path.parent,
        manifest_identity=manifest_identity,
        expected_authority=expected_authority,
        expected_subjects=subjects,
        provenance_policy=provenance_policy,
    )
    _verify_leaf_authorization(artifacts, authorization, manifest_identity)
    _verify_closed_evidence_tree(
        manifest_path,
        expected_identity_path=_plain_file(
            expected_identity_path, label="expected score identity"
        ),
        manifest=manifest,
        artifacts=artifacts,
    )
    workload_run_nonce_contexts = sorted(
        {
            (
                str(record["workload_sha256"]),
                str(record["run_id"]),
                str(record["nonce"]),
            )
            for artifact, _artifact_sha in artifacts.values()
            for record in artifact["records"]
        }
    )
    requested_current_ref_authorization = CurrentRefAuthorization(
        operation=authorization_operation,
        candidate_commit=expected_authority["candidate_commit"],
        candidate_identity_sha256=expected_identity["engine_artifact_sha256"],
        source_closure_sha256=expected_identity["source_closure_sha256"],
        workload_run_nonce_sha256=canonical_sha256(
            {"contexts": workload_run_nonce_contexts}
        ),
    )
    current_ref_proof = begin_packaged_semantic_registry_authorization(
        requested_current_ref_authorization
    )
    if (
        current_ref_proof.authorization != requested_current_ref_authorization
        or current_ref_proof.authorization_digest != requested_current_ref_authorization.digest
    ):
        raise SpecValidationError("packaged current-ref proof requested authorization differs")

    gate_reports: list[dict[str, Any]] = []
    for gate in NATIVE_SCORE_GATES:
        artifact, artifact_sha = artifacts[gate.gate_id]
        failures = ["stale_identity"] if stale_fields else []
        if _validate_identity(artifact["identity"], label="artifact identity") != manifest_identity:
            failures.append("artifact_identity_mismatch")
        records = artifact["records"]
        assert isinstance(records, list)
        failures.extend(gate.evaluate_records(records, manifest_identity))
        gate_reports.append(_gate_report(gate.gate_id, artifact_sha, _unique(failures)))
    points_awarded = sum(int(gate["points_awarded"]) for gate in gate_reports)
    report = {
        "schema_version": NATIVE_SCORECARD_VERSION,
        "identity": manifest_identity,
        "identity_match": not stale_fields,
        "points_awarded": points_awarded,
        "points_available": len(NATIVE_SCORE_GATES),
        "perfect_native": points_awarded == len(NATIVE_SCORE_GATES) and not blockers,
        "gates": gate_reports,
        "blockers": blockers,
    }
    validate_schema(report, NATIVE_SCORECARD_SCHEMA)
    _validate_derived_report(report)
    finalize_packaged_semantic_registry_authorization(current_ref_proof)
    if destination is not None and report["perfect_native"]:
        _publish_report_atomic(destination, report)
    return report


def require_native_scorecard_for_promotion(
    evidence_manifest_path: str | Path | None,
    *,
    expected_identity_path: str | Path | None = None,
    expected_candidate_commit: str | None = None,
    expected_candidate_identity: str | None = None,
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
    authorization_operation: str = "native-score-promotion",
) -> dict[str, Any]:
    """Require a fresh perfect scorecard evaluation at a promotion boundary."""
    if evidence_manifest_path is None or expected_identity_path is None:
        raise SpecValidationError("Native scorecard evidence and identity are required")
    identity_document = read_json(
        _plain_file(expected_identity_path, label="expected score identity")
    )
    score_identity, score_authority = _validate_expected_identity(identity_document)
    if (
        expected_candidate_commit is not None
        and score_authority["candidate_commit"] != expected_candidate_commit
    ):
        raise SpecValidationError("Native scorecard candidate commit differs")
    if (
        expected_candidate_identity is not None
        and score_identity["engine_artifact_sha256"] != expected_candidate_identity
    ):
        raise SpecValidationError("Native scorecard candidate package identity differs")
    report = evaluate_native_scorecard(
        evidence_manifest_path,
        expected_identity_path=expected_identity_path,
        provenance_policy=provenance_policy,
        authorization_operation=authorization_operation,
    )
    if report["perfect_native"] is not True or report["points_awarded"] != 10:
        raise SpecValidationError("Native scorecard is not a current perfect proof")
    return report


def require_fresh_current_ref_for_authorization(
    evidence_manifest_path: str | Path | None,
    expected_identity_path: str | Path | None,
    operation: str,
) -> None:
    """Reauthorize one imminent output or reservation against the exact score closure."""
    if evidence_manifest_path is None or expected_identity_path is None:
        raise SpecValidationError("Native scorecard evidence and identity are required")
    manifest_path = _plain_file(evidence_manifest_path, label="score evidence manifest")
    identity_document = read_json(
        _plain_file(expected_identity_path, label="expected score identity")
    )
    identity, authority = _validate_expected_identity(identity_document)
    requested_current_ref_authorization = CurrentRefAuthorization(
        operation=operation,
        candidate_commit=authority["candidate_commit"],
        candidate_identity_sha256=identity["engine_artifact_sha256"],
        source_closure_sha256=identity["source_closure_sha256"],
        workload_run_nonce_sha256=sha256_file(manifest_path),
    )
    proof = begin_packaged_semantic_registry_authorization(requested_current_ref_authorization)
    if (
        proof.authorization != requested_current_ref_authorization
        or proof.authorization_digest != requested_current_ref_authorization.digest
    ):
        raise SpecValidationError("packaged current-ref proof requested authorization differs")
    finalize_packaged_semantic_registry_authorization(proof)


def require_native_scorecard_candidate_binding(
    expected_identity_path: str | Path | None,
    *,
    expected_candidate_commit: str,
    expected_candidate_identity: str,
) -> None:
    """Bind an already validated score input to an opened release candidate."""
    if expected_identity_path is None:
        raise SpecValidationError("Native scorecard identity is required")
    document = read_json(_plain_file(expected_identity_path, label="expected score identity"))
    identity, authority = _validate_expected_identity(document)
    if authority["candidate_commit"] != expected_candidate_commit:
        raise SpecValidationError("Native scorecard candidate commit differs")
    if identity["engine_artifact_sha256"] != expected_candidate_identity:
        raise SpecValidationError("Native scorecard candidate package identity differs")


def _load_raw_artifacts(
    manifest: dict[str, Any],
    *,
    root: Path,
    manifest_identity: dict[str, str],
    verification_clock: VerificationClockPolicy,
) -> tuple[dict[str, tuple[dict[str, Any], str]], list[dict[str, Any]]]:
    proofs = manifest["proofs"]
    assert isinstance(proofs, dict)
    if set(proofs) != set(NATIVE_SCORE_GATE_IDS):
        raise SpecValidationError("signed scorecard requires exactly ten gate proofs")
    artifacts: dict[str, tuple[dict[str, Any], str]] = {}
    subjects: list[dict[str, Any]] = []
    for gate in NATIVE_SCORE_GATES:
        record = proofs[gate.gate_id]
        _require_exact_fields(
            record, required={"path", "sha256"}, label=f"{gate.gate_id} proof record"
        )
        assert isinstance(record, dict)
        proof_path, _proof_sha = _verified_path(record, root=root, label="proof")
        if proof_path is None:
            raise SpecValidationError(f"missing proof for gate {gate.gate_id}")
        proof = read_json(proof_path)
        _validate_proof(proof, gate)
        assert isinstance(proof, dict)
        artifact_record = proof["artifact"]
        _require_exact_fields(artifact_record, required={"path", "sha256"}, label="proof artifact")
        assert isinstance(artifact_record, dict)
        artifact_path, artifact_sha = _verified_path(artifact_record, root=root, label="evidence")
        if artifact_path is None:
            raise SpecValidationError(f"missing evidence for gate {gate.gate_id}")
        artifact = read_json(artifact_path)
        _validate_raw_artifact(
            artifact,
            gate,
            manifest_identity,
            root=root,
            verification_clock=verification_clock,
        )
        assert isinstance(artifact, dict)
        records = artifact["records"]
        assert isinstance(records, list)
        leaf_hashes = [canonical_sha256(item) for item in records]
        artifacts[gate.gate_id] = (artifact, artifact_sha)
        subjects.append(
            {
                "schema_version": NATIVE_SCORE_EVIDENCE_VERSION,
                "gate_id": gate.gate_id,
                "artifact_sha256": artifact_sha,
                "raw_record_schema_version": artifact["schema_version"],
                "evaluator_version": artifact["evaluator_version"],
                "expected_record_count": len(records),
                "record_sha256s": leaf_hashes,
            }
        )
    return artifacts, subjects


def _validate_raw_artifact(
    document: Any,
    gate: NativeScoreGate,
    manifest_identity: dict[str, str],
    *,
    root: Path,
    verification_clock: VerificationClockPolicy,
) -> None:
    if isinstance(document, dict) and "records" not in document:
        raise SpecValidationError(
            f"{gate.gate_id} raw records are required; aggregate observations are forbidden"
        )
    performance_v2 = bool(
        gate.gate_id == "deterministic_performance_resource_proof"
        and isinstance(document, dict)
        and document.get("schema_version") == NATIVE_SCORE_PERFORMANCE_EVIDENCE_VERSION
    )
    validate_schema(
        document,
        NATIVE_SCORE_RAW_EVIDENCE_V2_SCHEMA
        if performance_v2
        else NATIVE_SCORE_RAW_EVIDENCE_SCHEMA,
    )
    _require_exact_fields(
        document,
        required={"schema_version", "evaluator_version", "gate_id", "identity", "records"},
        label=f"malformed raw evidence for {gate.gate_id}",
    )
    assert isinstance(document, dict)
    expected_version = (
        NATIVE_SCORE_PERFORMANCE_EVIDENCE_VERSION
        if performance_v2
        else NATIVE_SCORE_EVIDENCE_VERSION
    )
    expected_evaluator = (
        NATIVE_SCORE_PERFORMANCE_EVALUATOR_VERSION
        if performance_v2
        else NATIVE_SCORE_EVALUATOR_VERSION
    )
    if (
        document["schema_version"] != expected_version
        or document["evaluator_version"] != expected_evaluator
        or document["gate_id"] != gate.gate_id
    ):
        raise SpecValidationError(f"malformed raw evidence for {gate.gate_id}: identity/version")
    _validate_identity(document["identity"], label="artifact identity")
    records = document["records"]
    if not isinstance(records, list) or not records:
        raise SpecValidationError(f"{gate.gate_id} raw records are required")
    record_ids: list[str] = []
    context_types: dict[tuple[str, str], list[str]] = defaultdict(list)
    context_semantics: dict[tuple[str, str], list[str]] = defaultdict(list)
    for record in records:
        _require_exact_fields(
            record,
            required={
                "schema_version",
                "gate_id",
                "record_id",
                "record_type",
                "source_identity_sha256",
                "candidate_identity_sha256",
                "mode_contract",
                "platform",
                "workload_sha256",
                "run_id",
                "nonce",
                "source_artifact",
                "payload",
            },
            label=f"{gate.gate_id} raw record",
        )
        assert isinstance(record, dict)
        if (
            record["schema_version"] != expected_version
            or record["gate_id"] != gate.gate_id
        ):
            raise SpecValidationError("raw record gate/schema identity differs")
        record_id = record["record_id"]
        if not isinstance(record_id, str) or not record_id:
            raise SpecValidationError("raw record id is malformed")
        if record_id != native_score_record_id(record):
            raise SpecValidationError("raw record canonical record id/preimage mismatch")
        record_ids.append(record_id)
        mode = record["mode_contract"]
        system = record["platform"]
        if mode not in _REQUIRED_MODE_CONTRACTS or system not in REQUIRED_PLATFORM_SYSTEMS:
            raise SpecValidationError("raw record mode/platform is unauthorized")
        _sha256(record["source_identity_sha256"], label="raw source identity")
        _sha256(record["candidate_identity_sha256"], label="raw candidate identity")
        _sha256(record["workload_sha256"], label="raw workload")
        _sha256(record["nonce"], label="raw nonce")
        if not isinstance(record["run_id"], str) or not record["run_id"]:
            raise SpecValidationError("raw record run identity is malformed")
        if not isinstance(record["payload"], dict):
            raise SpecValidationError("raw record payload must be an object")
        _verify_machine_record(
            record,
            root=root,
            verification_clock=verification_clock,
        )
        context_types[(mode, system)].append(record["record_type"])
        context_semantics[(mode, system)].append(_semantic_identity(record))
    if len(record_ids) != len(set(record_ids)):
        raise SpecValidationError("raw record duplicate semantic identity or record id")
    if record_ids != sorted(record_ids):
        raise SpecValidationError("raw records are reordered")
    if performance_v2:
        if (
            len(records) != len(_REQUIRED_MODE_CONTRACTS)
            or {record["mode_contract"] for record in records} != set(_REQUIRED_MODE_CONTRACTS)
            or any(record["platform"] != "linux" for record in records)
            or any(
                record["record_type"] != NATIVE_SCORE_PERFORMANCE_RECORD_TYPE
                for record in records
            )
        ):
            raise SpecValidationError(
                "v2 performance records must contain one certificate per mode"
            )
    else:
        expected_contexts = {
            (mode, system)
            for mode in _REQUIRED_MODE_CONTRACTS
            for system in REQUIRED_PLATFORM_SYSTEMS
        }
        if set(context_types) != expected_contexts:
            raise SpecValidationError("raw records have incomplete mode/platform cardinality")
        expected_types = Counter(gate.record_types)
        if any(Counter(types) != expected_types for types in context_types.values()):
            raise SpecValidationError("raw records have missing, extra, or cross-gate leaves")
        for context, semantic_ids in context_semantics.items():
            if len(semantic_ids) != len(set(semantic_ids)):
                raise SpecValidationError("raw record duplicate semantic identity")
            if set(semantic_ids) != _expected_semantic_identities(gate, *context):
                raise SpecValidationError("raw record differs from authoritative universe")
    if _validate_identity(document["identity"], label="artifact identity") != manifest_identity:
        return


def _verify_machine_record(
    record: dict[str, Any],
    *,
    root: Path,
    verification_clock: VerificationClockPolicy,
) -> None:
    source = record["source_artifact"]
    _require_exact_fields(source, required={"path", "sha256"}, label="machine record artifact")
    assert isinstance(source, dict)
    path, _digest = _verified_path(source, root=root, label="machine record artifact")
    if path is None:
        raise SpecValidationError("machine record artifact is missing")
    document = read_json(path)
    performance_v2 = record["record_type"] == NATIVE_SCORE_PERFORMANCE_RECORD_TYPE
    validate_schema(
        document,
        NATIVE_SCORE_MACHINE_RECORD_V2_SCHEMA
        if performance_v2
        else NATIVE_SCORE_MACHINE_RECORD_SCHEMA,
    )
    _require_exact_fields(
        document,
        required={
            "schema_version",
            "gate_id",
            "record_type",
            "semantic_identity",
            "producer",
            "context",
            "inputs",
            "observation",
        },
        label="independent machine record",
    )
    assert isinstance(document, dict)
    if (
        document["schema_version"]
        != (
            NATIVE_SCORE_PERFORMANCE_EVIDENCE_VERSION
            if performance_v2
            else NATIVE_SCORE_RAW_RECORD_VERSION
        )
        or document["gate_id"] != record["gate_id"]
        or document["record_type"] != record["record_type"]
        or document["semantic_identity"] != _semantic_identity(record)
    ):
        raise SpecValidationError("machine record semantic identity differs")
    producer = document["producer"]
    _require_exact_fields(
        producer,
        required={"role", "identity_sha256", "run_id"},
        label="machine record producer",
    )
    assert isinstance(producer, dict)
    if producer["role"] != native_score_producer_role(record["record_type"]):
        raise SpecValidationError("machine record producer role is unauthorized")
    producer_identity = _sha256(producer["identity_sha256"], label="machine producer identity")
    if producer_identity != record["candidate_identity_sha256"]:
        raise SpecValidationError("machine record producer candidate identity differs")
    if producer["run_id"] != record["run_id"]:
        raise SpecValidationError("machine producer run is stale or unauthorized")
    expected_context = {
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
    if document["context"] != expected_context:
        raise SpecValidationError("machine record context differs")
    inputs = document["inputs"]
    if not isinstance(inputs, list):
        raise SpecValidationError("machine record inputs must be an array")
    expected_input_fields = _MACHINE_INPUT_FIELDS_BY_TYPE.get(record["record_type"], ())
    if [item.get("field") for item in inputs if isinstance(item, dict)] != list(
        expected_input_fields
    ):
        raise SpecValidationError("machine record inputs differ from the proof contract")
    input_paths: set[Path] = set()
    for item in inputs:
        _require_exact_fields(
            item, required={"field", "path", "sha256"}, label="machine record input"
        )
        assert isinstance(item, dict)
        input_path, input_sha256 = _verified_path(
            item, root=root, label=f"machine input {item['field']}"
        )
        if input_path is None:
            raise SpecValidationError("machine record input is missing")
        if input_path in input_paths:
            raise SpecValidationError("machine record input path is aliased")
        input_paths.add(input_path)
        if (
            record["record_type"] != "performance_process_sample"
            and input_sha256 != record["payload"][item["field"]]
        ):
            raise SpecValidationError("machine record input digest differs from observation")
        validate_domain_input(
            read_json(input_path),
            record=record,
            field=item["field"],
            registry_identity=_offline_nonpromotional_semantic_registry_identity(),
            verification_clock=verification_clock,
        )
    if document["observation"] != record["payload"]:
        raise SpecValidationError("machine record observation/leaf aggregate disagrees")


def _verify_signed_subject_graphs(
    manifest: dict[str, Any],
    *,
    root: Path,
    manifest_identity: dict[str, str],
    expected_authority: dict[str, str],
    expected_subjects: list[dict[str, Any]],
    provenance_policy: ProvenancePolicy,
) -> dict[tuple[str, str], dict[str, Any]]:
    graph_records = manifest["subject_graphs"]
    if len(graph_records) != len(_REQUIRED_MODE_CONTRACTS):
        raise SpecValidationError("signed subject graphs have incomplete mode cardinality")
    seen_modes: set[str] = set()
    seen_attestations: set[str] = set()
    seen_nonces: set[str] = set()
    run_identities: set[tuple[str, int]] = set()
    packages_by_mode: dict[str, dict[str, tuple[Any, Any]]] = {}
    authorization: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_record in graph_records:
        _require_exact_fields(
            raw_record,
            required={"mode_contract", "path", "sha256"},
            label="signed subject graph record",
        )
        assert isinstance(raw_record, dict)
        mode = raw_record["mode_contract"]
        if not isinstance(mode, str) or mode in seen_modes:
            raise SpecValidationError("signed subject graph mode was duplicated")
        seen_modes.add(mode)
        graph_path, _graph_sha = _verified_path(raw_record, root=root, label="signed subject graph")
        if graph_path is None:
            raise SpecValidationError("signed subject graph is missing")
        graph_document = read_json(graph_path)
        if not isinstance(graph_document, dict):
            raise SpecValidationError("signed subject graph must be an object")
        graph = verify_embedded_platform_evidence(
            graph_document,
            policy=provenance_policy,
            expected_commit=expected_authority["candidate_commit"],
            expected_candidate_id=manifest_identity["engine_artifact_sha256"],
            expected_bundle_id=expected_authority["bundle_id"],
            expected_challenge=expected_authority["challenge"],
            required_platform_systems=REQUIRED_PLATFORM_SYSTEMS,
        )
        reports = graph["reports"]
        if graph_document.get("mode_contract") != mode or {
            item["workload"]["mode_contract"] for item in reports
        } != {mode}:
            raise SpecValidationError("signed subject graph mode authorization differs")
        if {item["platform"]["system"] for item in reports} != REQUIRED_PLATFORM_SYSTEMS:
            raise SpecValidationError("signed subject graph platform authorization differs")
        packages_by_mode[mode] = {}
        for report, statement in zip(reports, graph["statements"], strict=True):
            if report.get("native_scorecard_subjects") != expected_subjects:
                raise SpecValidationError(
                    "signed scorecard subjects or raw leaves are missing or contradictory"
                )
            system = report["platform"]["system"]
            package = report["package"]
            packages_by_mode[mode][system] = (
                package.get("wheel_sha256"),
                package.get("native_extension_sha256"),
            )
            bundle = statement["bundle"]
            if bundle["attestation_id"] in seen_attestations or bundle["nonce"] in seen_nonces:
                raise SpecValidationError("signed scorecard subject was replayed")
            seen_attestations.add(bundle["attestation_id"])
            seen_nonces.add(bundle["nonce"])
            producer = statement["producer"]
            run_identities.add((producer["run_id"], producer["run_attempt"]))
            authorization[(mode, system)] = {
                "workload_sha256": statement["workload_identity_sha256"],
                "run_id": producer["run_id"],
                "nonce": bundle["nonce"],
                "wheel_sha256": package.get("wheel_sha256"),
                "native_extension_sha256": package.get("native_extension_sha256"),
                "producer": producer,
                "bundle": bundle,
                "candidate_id": graph["candidate_id"],
            }
    if seen_modes != set(_REQUIRED_MODE_CONTRACTS):
        raise SpecValidationError("signed subject graphs do not authorize exact required modes")
    if len(run_identities) != 1:
        raise SpecValidationError("signed subject graphs differ in run authorization")
    if len({tuple(sorted(packages.items())) for packages in packages_by_mode.values()}) != 1:
        raise SpecValidationError("signed subject graphs differ in candidate package identity")
    return authorization


def _verify_leaf_authorization(
    artifacts: dict[str, tuple[dict[str, Any], str]],
    authorization: dict[tuple[str, str], dict[str, Any]],
    identity: dict[str, str],
) -> None:
    portfolio_artifact, _ = artifacts["same_candidate_portfolio_platform_certification"]
    portfolio_certificates = {
        (record["mode_contract"], record["platform"]): record["payload"]["certificate_sha256"]
        for record in portfolio_artifact["records"]
    }
    for gate_id, (artifact, _artifact_sha) in artifacts.items():
        for record in artifact["records"]:
            context = authorization[(record["mode_contract"], record["platform"])]
            if (
                record["source_identity_sha256"] != identity["source_closure_sha256"]
                or record["candidate_identity_sha256"] != identity["engine_artifact_sha256"]
                or record["workload_sha256"] != context["workload_sha256"]
                or record["run_id"] != context["run_id"]
                or record["nonce"] != context["nonce"]
            ):
                raise SpecValidationError("raw record source/candidate/run context differs")
            payload = record["payload"]
            if (
                gate_id == "immutable_identity_scope"
                and record["record_type"] == "provenance_identity"
            ):
                producer = context["producer"]
                bundle = context["bundle"]
                expected = {
                    **{
                        field: producer[field]
                        for field in (
                            "repository",
                            "repository_ref",
                            "commit",
                            "workflow",
                            "workflow_ref",
                            "job",
                        )
                    },
                    "bundle_id": bundle["bundle_id"],
                    "challenge": bundle["challenge"],
                    "candidate_id": context["candidate_id"],
                }
                if payload != expected:
                    raise SpecValidationError("provenance identity raw record differs")
            if gate_id == "same_candidate_portfolio_platform_certification" and (
                payload.get("wheel_sha256") != context["wheel_sha256"]
                or payload.get("native_extension_sha256") != context["native_extension_sha256"]
            ):
                raise SpecValidationError("portfolio raw record package identity differs")
            if gate_id == "deterministic_performance_resource_proof" and (
                record["record_type"] == NATIVE_SCORE_PERFORMANCE_RECORD_TYPE
                and (
                    payload.get("wheel_sha256") != context["wheel_sha256"]
                    or payload.get("native_extension_sha256")
                    != context["native_extension_sha256"]
                )
            ):
                raise SpecValidationError("performance raw record package identity differs")
            if (
                record["record_type"] == NATIVE_SCORE_PERFORMANCE_RECORD_TYPE
                and payload["certificate_sha256"]
                != portfolio_certificates.get((record["mode_contract"], record["platform"]))
            ):
                raise SpecValidationError("performance and portfolio certificate identities differ")


def _validate_manifest(document: Any) -> None:
    _require_exact_fields(
        document,
        required={"schema_version", "identity", "proofs", "subject_graphs"},
        label="score evidence manifest",
    )
    assert isinstance(document, dict)
    if document["schema_version"] != NATIVE_SCORE_EVIDENCE_VERSION:
        raise SpecValidationError("unsupported score evidence schema version")
    _validate_identity(document["identity"], label="manifest identity")
    if not isinstance(document["proofs"], dict) or not isinstance(document["subject_graphs"], list):
        raise SpecValidationError("score evidence proofs/subject_graphs are malformed")
    unexpected = sorted(set(document["proofs"]) - set(NATIVE_SCORE_GATE_IDS))
    if unexpected:
        raise SpecValidationError(f"score evidence proofs contain unexpected gate: {unexpected[0]}")


def _validate_expected_identity(document: Any) -> tuple[dict[str, str], dict[str, str]]:
    _require_exact_fields(
        document,
        required={"schema_version", *_IDENTITY_FIELDS, "candidate_commit", *_AUTHORITY_FIELDS},
        label="expected score identity",
    )
    assert isinstance(document, dict)
    if document["schema_version"] != NATIVE_SCORE_EVIDENCE_VERSION:
        raise SpecValidationError("unsupported expected score identity schema version")
    identity = _validate_identity(
        {field: document[field] for field in _IDENTITY_FIELDS}, label="expected score identity"
    )
    commit = document["candidate_commit"]
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise SpecValidationError("expected candidate commit must be a Git SHA")
    return identity, {
        "candidate_commit": commit,
        **{
            field: _sha256(document[field], label=f"expected {field}")
            for field in _AUTHORITY_FIELDS
        },
    }


def _validate_proof(document: Any, gate: NativeScoreGate) -> None:
    _require_exact_fields(
        document,
        required={"schema_version", "gate_id", "identity", "artifact"},
        label=f"malformed proof for {gate.gate_id}",
    )
    assert isinstance(document, dict)
    if (
        document["schema_version"] != NATIVE_SCORE_EVIDENCE_VERSION
        or document["gate_id"] != gate.gate_id
    ):
        raise SpecValidationError(f"malformed proof for {gate.gate_id}: gate/version")
    _validate_identity(document["identity"], label="proof identity")


def _publication_checkpoint(_name: str) -> None:
    """Deterministic interruption hook for transactional publication tests."""


def _publish_report_atomic(destination: Path, report: dict[str, Any]) -> None:
    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise SpecValidationError("scorecard output parent must be an existing directory")
    if os.name == "posix" and parent.stat().st_uid != os.geteuid():
        raise SpecValidationError("scorecard output parent is not owned by this process")
    stage = parent / f".{destination.name}.stage-{secrets.token_hex(16)}"
    published = False
    try:
        write_json(stage, report)
        stage.chmod(0o600)
        with stage.open("r+b") as handle:
            os.fsync(handle.fileno())
        _publication_checkpoint("after-stage-fsync")
        try:
            os.link(stage, destination)
        except FileExistsError as exc:
            raise SpecValidationError(
                f"scorecard output must not already exist: {destination}"
            ) from exc
        published = True
        _fsync_directory(parent)
        _publication_checkpoint("after-atomic-publication")
    except BaseException:
        if published:
            try:
                if destination.stat().st_ino == stage.stat().st_ino:
                    destination.unlink()
                    _fsync_directory(parent)
            except FileNotFoundError:
                pass
        raise
    finally:
        stage.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_closed_evidence_tree(
    manifest_path: Path,
    *,
    expected_identity_path: Path,
    manifest: dict[str, Any],
    artifacts: dict[str, tuple[dict[str, Any], str]],
) -> None:
    """Require one closed, non-aliased fixed-layer dependency DAG."""
    root = manifest_path.parent
    used = {manifest_path.resolve()}
    if expected_identity_path.parent != root:
        raise SpecValidationError("score identity must belong to the evidence bundle")
    used.add(expected_identity_path.resolve())
    referenced: list[Path] = []
    for gate in NATIVE_SCORE_GATES:
        proof_record = manifest["proofs"][gate.gate_id]
        proof_path = (
            root / _safe_relative_path(proof_record["path"], label="proof path")
        ).resolve()
        referenced.append(proof_path)
        proof = read_json(proof_path)
        artifact_record = proof["artifact"]
        artifact_path = (
            root / _safe_relative_path(artifact_record["path"], label="evidence path")
        ).resolve()
        referenced.append(artifact_path)
        artifact = artifacts[gate.gate_id][0]
        for record in artifact["records"]:
            source = record["source_artifact"]
            machine_path = (
                root / _safe_relative_path(source["path"], label="machine record path")
            ).resolve()
            referenced.append(machine_path)
            machine = read_json(machine_path)
            if isinstance(machine, dict) and isinstance(machine.get("inputs"), list):
                for item in machine["inputs"]:
                    if isinstance(item, dict):
                        referenced.append(
                            (
                                root
                                / _safe_relative_path(
                                    item.get("path"), label="machine input path"
                                )
                            ).resolve()
                        )
    for graph in manifest["subject_graphs"]:
        referenced.append(
            (root / _safe_relative_path(graph["path"], label="signed graph path")).resolve()
        )
    if len(referenced) != len(set(referenced)):
        raise SpecValidationError("score dependency DAG contains an aliased artifact")
    used.update(referenced)
    actual: set[Path] = set()
    inode_owners: dict[tuple[int, int], Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SpecValidationError("score dependency DAG contains a symlink")
        if not path.is_file() and not path.is_dir():
            raise SpecValidationError("score dependency DAG contains a non-regular input")
        if path.is_file():
            identity = (path.stat().st_dev, path.stat().st_ino)
            if identity in inode_owners:
                raise SpecValidationError("score dependency DAG contains a hard-link alias")
            inode_owners[identity] = path
            actual.add(path.resolve())
    if actual != used:
        missing = used - actual
        if missing:
            raise SpecValidationError("score dependency DAG contains a dangling artifact")
        raise SpecValidationError("score dependency DAG contains an unused artifact")


def _verify_score_schema_identities() -> None:
    """Reject substituted or duplicated score schemas before parsing evidence."""
    try:
        package_locations = tuple(
            Path(location).absolute()
            for location in import_module("nfi_backtest_engine.schemas").__path__
        )
    except (ImportError, ModuleNotFoundError, OSError) as exc:
        raise SpecValidationError("NATIVE_SCORE_SCHEMA_IDENTITY: package unavailable") from exc
    if len(package_locations) != 1:
        raise SpecValidationError("NATIVE_SCORE_SCHEMA_IDENTITY: package location duplicated")
    package = package_locations[0]
    if package.is_symlink() or not package.is_dir():
        raise SpecValidationError("NATIVE_SCORE_SCHEMA_IDENTITY: package is not a directory")
    for schema_name, (expected_bytes, expected_sha256) in _TRUSTED_SCORE_SCHEMA_IDENTITIES.items():
        stem = schema_name.removesuffix(".json")
        matches = sorted(entry.name for entry in package.iterdir() if stem in entry.name)
        if matches != [schema_name]:
            raise SpecValidationError(
                f"NATIVE_SCORE_SCHEMA_IDENTITY: {schema_name} is absent or duplicated"
            )
        resource = package / schema_name
        if resource.is_symlink() or not resource.is_file():
            raise SpecValidationError(
                f"NATIVE_SCORE_SCHEMA_IDENTITY: {schema_name} is not a regular file"
            )
        try:
            payload = resource.read_bytes()
        except (FileNotFoundError, IsADirectoryError, OSError) as exc:
            raise SpecValidationError(
                f"NATIVE_SCORE_SCHEMA_IDENTITY: {schema_name} is unavailable"
            ) from exc
        if len(payload) != expected_bytes or hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise SpecValidationError(
                f"NATIVE_SCORE_SCHEMA_IDENTITY: {schema_name} differs from compiled identity"
            )


def _verified_path(record: dict[str, Any], *, root: Path, label: str) -> tuple[Path | None, str]:
    relative = _safe_relative_path(record["path"], label=f"{label} path")
    expected_sha = _sha256(record["sha256"], label=f"{label} sha256")
    path = root / relative
    if not path.is_file() or path.is_symlink():
        return None, expected_sha
    if sha256_file(path) != expected_sha:
        raise SpecValidationError(f"{label} checksum mismatch")
    return path, expected_sha


def _gate_report(gate_id: str, evidence_sha256: str | None, failures: list[str]) -> dict[str, Any]:
    met = not failures
    return {
        "id": gate_id,
        "points_available": 1,
        "points_awarded": int(met),
        "met": met,
        "failures": failures,
        "evidence_sha256": evidence_sha256,
    }


def _validate_identity(value: Any, *, label: str) -> dict[str, str]:
    _require_exact_fields(value, required=set(_IDENTITY_FIELDS), label=label)
    assert isinstance(value, dict)
    return {field: _sha256(value[field], label=f"{label} {field}") for field in _IDENTITY_FIELDS}


def _validate_derived_report(report: dict[str, Any]) -> None:
    gates = report["gates"]
    if tuple(gate["id"] for gate in gates) != NATIVE_SCORE_GATE_IDS:
        raise SpecValidationError("native scorecard gate order differs from the ten-point contract")
    derived = sum(int(gate["met"]) for gate in gates)
    if report["points_awarded"] != derived or report["perfect_native"] != (derived == 10):
        raise SpecValidationError("native scorecard contains a caller-supplied verdict")


def _require_exact_fields(value: Any, *, required: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise SpecValidationError(f"{label}: expected an object")
    missing = sorted(required - set(value))
    if missing:
        raise SpecValidationError(f"{label}: missing field {missing[0]}")
    unexpected = sorted(set(value) - required)
    if unexpected:
        raise SpecValidationError(f"{label}: unexpected field {unexpected[0]}")


def _safe_relative_path(value: Any, *, label: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise SpecValidationError(f"{label}: expected a relative path")
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SpecValidationError(f"{label}: expected a safe relative path")
    return path


def _sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise SpecValidationError(f"{label}: expected a lowercase SHA-256")
    return value


def _number_value(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SpecValidationError(f"{label}: expected a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise SpecValidationError(f"{label}: outside allowed range")
    return number


def _nonnegative_number_value(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SpecValidationError(f"{label}: expected a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise SpecValidationError(f"{label}: outside allowed range")
    return number


def _nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SpecValidationError(f"{label}: expected a nonnegative integer")
    return value


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _plain_file(source: str | Path, *, label: str) -> Path:
    raw = Path(source).absolute()
    if raw.is_symlink():
        raise SpecValidationError(f"{label} must not be a symlink")
    resolved = raw.resolve()
    if not resolved.is_file():
        raise SpecValidationError(f"{label} does not exist: {resolved}")
    return resolved
