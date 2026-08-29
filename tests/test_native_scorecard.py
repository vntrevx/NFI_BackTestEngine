from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine import cli, native_scorecard
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.commands.release import execute_release
from nfi_backtest_engine.errors import PackagedRegistryCurrentRefError, SpecValidationError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.native_score_domain_identity import (
    VerificationClockPolicy,
    native_score_producer_role,
)
from nfi_backtest_engine.native_score_domains import (
    DOMAIN_EVIDENCE_VERSION,
    PROCESS_EVIDENCE_VERSION,
    REPLAY_EVIDENCE_VERSION,
    producer_identity,
)
from nfi_backtest_engine.native_scorecard import (
    NATIVE_SCORE_GATE_IDS,
    native_score_record_id,
)
from nfi_backtest_engine.native_scorecard import (
    evaluate_native_scorecard as _evaluate_native_scorecard,
)
from nfi_backtest_engine.platform_benchmark import (
    EXACT_FIXTURE_LANE,
    REQUIRED_PLATFORM_MACHINES,
    REQUIRED_PLATFORM_SYSTEMS,
    seal_platform_evidence,
)
from nfi_backtest_engine.release_contract import (
    FUTURES_RELEASE_CONTRACT,
    FUTURES_RELEASE_CONTRACT_ID,
    SPOT_RELEASE_CONTRACT,
    SPOT_RELEASE_CONTRACT_ID,
    release_contract_for_scope,
)
from nfi_backtest_engine.release_provenance import canonical_sha256, workload_identity
from nfi_backtest_engine.semantic_registry import (
    CurrentRefAuthorization,
    PackagedRegistryCurrentRefProof,
    finalize_packaged_semantic_registry_authorization,
    packaged_semantic_obligation_registry_identity,
)
from nfi_backtest_engine.specs import NATIVE_SCORE_RAW_EVIDENCE_SCHEMA, validate_schema
from provenance_support import (
    TEST_CHALLENGE,
    TEST_COMMIT,
    TEST_POLICY,
    sign_report,
)


def evaluate_native_scorecard(*args, **kwargs):
    kwargs.setdefault("provenance_policy", TEST_POLICY)
    return _evaluate_native_scorecard(*args, **kwargs)


IDENTITY = {
    "source_closure_sha256": "1" * 64,
    "engine_artifact_sha256": "2" * 64,
    "oracle_sha256": "3" * 64,
    "scope_sha256": "4" * 64,
}
MODES = tuple(sorted({SPOT_RELEASE_CONTRACT_ID, FUTURES_RELEASE_CONTRACT_ID}))
SYSTEMS = tuple(sorted(REQUIRED_PLATFORM_SYSTEMS))
_EVIDENCE_ISSUED = datetime.now(UTC).replace(microsecond=0)
_EVIDENCE_EXPIRES = _EVIDENCE_ISSUED + timedelta(hours=23)


def _current_ref_proof(
    authorization: CurrentRefAuthorization,
) -> PackagedRegistryCurrentRefProof:
    return PackagedRegistryCurrentRefProof(
        authorization=authorization,
        authorization_digest=authorization.digest,
        packaged_commit=TEST_COMMIT,
        packaged_source_closure_sha256=IDENTITY["source_closure_sha256"],
        initial_observed_commit=TEST_COMMIT,
        document={"source_closure": {"merkle_root": IDENTITY["source_closure_sha256"]}},
    )


@pytest.fixture(autouse=True)
def _stable_current_ref_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        native_scorecard,
        "begin_packaged_semantic_registry_authorization",
        _current_ref_proof,
    )
    monkeypatch.setattr(
        native_scorecard,
        "finalize_packaged_semantic_registry_authorization",
        lambda _proof: None,
    )


def test_scorecard_baseline_release_scope_identity_is_derived() -> None:
    for contract in (SPOT_RELEASE_CONTRACT, FUTURES_RELEASE_CONTRACT):
        scope = contract.scope_fields()
        assert release_contract_for_scope(scope) is contract
        assert scope == {
            "mode_contract": contract.contract_id,
            "trading_mode": contract.trading_mode,
            "margin_mode": contract.margin_mode,
            "exchange": contract.exchange,
            "settlement_currency": contract.settlement_currency,
            "required_data_roles": list(contract.required_data_roles),
        }


def _bundle_id() -> str:
    return hashlib.sha256(
        f"{IDENTITY['engine_artifact_sha256']}:{TEST_CHALLENGE}".encode()
    ).hexdigest()


def _nonce(mode: str, system: str) -> str:
    return hashlib.sha256(f"{mode}:{system}".encode()).hexdigest()


def _workload(mode: str) -> dict[str, Any]:
    workload = {
        "lane": EXACT_FIXTURE_LANE,
        "mode_contract": mode,
        "fixture_id": "scorecard-x7",
        "manifest_sha256": "f" * 64,
        "strategy_sha256": "1" * 64,
        "base_strategy_sha256": "1" * 64,
        "verification_level": "full",
        "identity_sha256": "0" * 64,
    }
    workload["identity_sha256"] = workload_identity(workload)
    return workload


def _package_digests(system: str) -> tuple[str, str]:
    digit = f"{SYSTEMS.index(system) + 10:x}"
    return digit * 64, digit * 64


def _record(
    gate_id: str,
    record_type: str,
    mode: str,
    system: str,
    suffix: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    record = {
        "schema_version": "1.0.0",
        "gate_id": gate_id,
        "record_id": f"{mode}|{system}|{record_type}|{suffix}",
        "record_type": record_type,
        "source_identity_sha256": IDENTITY["source_closure_sha256"],
        "candidate_identity_sha256": IDENTITY["engine_artifact_sha256"],
        "mode_contract": mode,
        "platform": system,
        "workload_sha256": _workload(mode)["identity_sha256"],
        "run_id": "1",
        "nonce": _nonce(mode, system),
        "payload": payload,
    }
    record["record_id"] = native_score_record_id(record)
    return record


def _raw_records(*, candidate_commit: str = TEST_COMMIT) -> dict[str, list[dict[str, Any]]]:
    records = {gate_id: [] for gate_id in NATIVE_SCORE_GATE_IDS}
    registry_identity = packaged_semantic_obligation_registry_identity()
    registry_fingerprint = registry_identity["registry_fingerprint"]
    for mode in MODES:
        for system in SYSTEMS:
            for index, component in enumerate(IDENTITY):
                records["immutable_identity_scope"].append(
                    _record(
                        "immutable_identity_scope",
                        "identity_component",
                        mode,
                        system,
                        str(index),
                        {"component": component, "digest": IDENTITY[component]},
                    )
                )
            records["immutable_identity_scope"].append(
                _record(
                    "immutable_identity_scope",
                    "provenance_identity",
                    mode,
                    system,
                    "4",
                    {
                        "repository": TEST_POLICY.repository,
                        "repository_ref": TEST_POLICY.repository_ref,
                        "commit": candidate_commit,
                        "workflow": TEST_POLICY.workflow,
                        "workflow_ref": TEST_POLICY.workflow_ref,
                        "job": TEST_POLICY.job,
                        "bundle_id": _bundle_id(),
                        "challenge": TEST_CHALLENGE,
                        "candidate_id": IDENTITY["engine_artifact_sha256"],
                    },
                )
            )
            records["evidence_independence"].append(
                _record(
                    "evidence_independence",
                    "producer_run",
                    mode,
                    system,
                    "0",
                    {
                        "oracle_producer": "oracle-capture",
                        "oracle_run_id": "oracle-1",
                        "native_producer": "native-candidate",
                        "native_run_id": "native-1",
                        "verifier_producer": "independent-verifier",
                        "verifier_run_id": "verifier-1",
                        "observer_baseline_sha256": "5" * 64,
                        "observer_trace_sha256": "5" * 64,
                    },
                )
            )
            records["native_purity"].append(
                _record(
                    "native_purity",
                    "execution_trace",
                    mode,
                    system,
                    "0",
                    {
                        "trace_sha256": "6" * 64,
                        "events": [
                            {
                                "sequence": 0,
                                "kind": "native_extension",
                                "module": "nfi_backtest_engine._rust",
                                "route": "native",
                            }
                        ],
                    },
                )
            )
            records["semantic_closure"].append(
                _record(
                    "semantic_closure",
                    "semantic_obligation",
                    mode,
                    system,
                    "0",
                    {
                        "obligation_id": f"registry:{registry_fingerprint}",
                        "registry_sha256": registry_identity["compressed_sha256"],
                        "mapping": "compiled-program",
                        "witness_sha256": "7" * 64,
                    },
                )
            )
            for index, kind in enumerate(
                ("obligation_coverage", "changed_target", "mcdc_term", "transition")
            ):
                records["changed_path_coverage_completeness"].append(
                    _record(
                        "changed_path_coverage_completeness",
                        kind,
                        mode,
                        system,
                        str(index),
                        {
                            "target_id": f"{kind}:{mode}",
                            "required_witness_sha256": "8" * 64,
                            "observed_witness_sha256": "8" * 64,
                        },
                    )
                )
            for index, kind in enumerate(("vector", "decision", "callback", "state_delta")):
                records["vector_callback_exactness"].append(
                    _record(
                        "vector_callback_exactness",
                        kind,
                        mode,
                        system,
                        str(index),
                        {
                            "comparison_id": f"{kind}:{mode}",
                            "expected_sha256": "9" * 64,
                            "actual_sha256": "9" * 64,
                        },
                    )
                )
            records["execution_complete_state_exactness"].append(
                _record(
                    "execution_complete_state_exactness",
                    "execution_state",
                    mode,
                    system,
                    "0",
                    {
                        "event_id": f"event:{mode}",
                        "expected_execution_sha256": "a" * 64,
                        "actual_execution_sha256": "a" * 64,
                        "expected_state_sha256": "b" * 64,
                        "actual_state_sha256": "b" * 64,
                        "state_fields": [
                            "quote_free",
                            "base_balances",
                            "open_trade_count",
                            "realized_profit",
                            "closed_trade_count",
                            "rejected_signals",
                            "trade_id_counter",
                            "order_id_counter",
                            "locks",
                        ],
                    },
                )
            )
            for index, kind in enumerate(("generative_case", "metamorphic_case")):
                payload = {
                    "case_id": f"{kind}:{mode}",
                    "seed": index + 1,
                    "input_sha256": "c" * 64,
                    "expected_sha256": "d" * 64,
                    "actual_sha256": "d" * 64,
                }
                if kind == "metamorphic_case":
                    payload["relation"] = "scale-invariant"
                records["generative_metamorphic_mutation_proof"].append(
                    _record(
                        "generative_metamorphic_mutation_proof",
                        kind,
                        mode,
                        system,
                        str(index),
                        payload,
                    )
                )
            records["generative_metamorphic_mutation_proof"].append(
                _record(
                    "generative_metamorphic_mutation_proof",
                    "mutant_outcome",
                    mode,
                    system,
                    "2",
                    {
                        "mutant_id": f"mutant:{mode}",
                        "operator": "invert-branch",
                        "run_sha256": "e" * 64,
                        "baseline_result_sha256": "1" * 64,
                        "mutant_result_sha256": "2" * 64,
                    },
                )
            )
            wheel, native = _package_digests(system)
            records["same_candidate_portfolio_platform_certification"].append(
                _record(
                    "same_candidate_portfolio_platform_certification",
                    "portfolio_certificate",
                    mode,
                    system,
                    "0",
                    {
                        "certificate_sha256": "f" * 64,
                        "candidate_sha256": IDENTITY["engine_artifact_sha256"],
                        "wheel_sha256": wheel,
                        "native_extension_sha256": native,
                        "replay_result_sha256": "1" * 64,
                        "replay_candidate_sha256": IDENTITY["engine_artifact_sha256"],
                    },
                )
            )
            for runtime, wall_seconds in (("official", 120.0), ("native", 10.0)):
                for population in ("cold", "reuse"):
                    for index, spread in enumerate((0.99, 1.0, 1.01)):
                        records["deterministic_performance_resource_proof"].append(
                            _record(
                                "deterministic_performance_resource_proof",
                                "performance_process_sample",
                                mode,
                                system,
                                f"{runtime}-{population}-{index}",
                                {
                                    "runtime": runtime,
                                    "population": population,
                                    "sample_index": index,
                                    "wall_seconds": wall_seconds * spread,
                                    "peak_rss_bytes": 1000,
                                    "memory_limit_bytes": 2000,
                                    "oom": False,
                                    "swap_bytes": 0,
                                    "output_sha256": "2" * 64,
                                },
                            )
                        )
    for gate_records in records.values():
        gate_records.sort(key=lambda item: item["record_id"])
    return records


RAW_FAILURES: dict[str, tuple[Callable[[list[dict[str, Any]]], None], str]] = {
    "immutable_identity_scope": (
        lambda records: next(
            item for item in records if item["record_type"] == "identity_component"
        )["payload"].__setitem__("digest", "f" * 64),
        "identity_component_mismatch",
    ),
    "evidence_independence": (
        lambda records: records[0]["payload"].__setitem__("observer_trace_sha256", "f" * 64),
        "observer_interference",
    ),
    "native_purity": (
        lambda records: records[0]["payload"]["events"][0].__setitem__("route", "official"),
        "non_native_route:official",
    ),
    "semantic_closure": (
        lambda records: records[0]["payload"].__setitem__("mapping", "unknown"),
        "semantic_obligation_not_closed",
    ),
    "changed_path_coverage_completeness": (
        lambda records: next(item for item in records if item["record_type"] == "changed_target")[
            "payload"
        ].__setitem__("observed_witness_sha256", "f" * 64),
        "uncovered:changed_target",
    ),
    "vector_callback_exactness": (
        lambda records: next(item for item in records if item["record_type"] == "callback")[
            "payload"
        ].__setitem__("actual_sha256", "f" * 64),
        "mismatch:callback",
    ),
    "execution_complete_state_exactness": (
        lambda records: records[0]["payload"].__setitem__("actual_state_sha256", "f" * 64),
        "complete_state_mismatch",
    ),
    "generative_metamorphic_mutation_proof": (
        lambda records: next(item for item in records if item["record_type"] == "mutant_outcome")[
            "payload"
        ].__setitem__("mutant_result_sha256", "1" * 64),
        "mutant_not_killed",
    ),
    "same_candidate_portfolio_platform_certification": (
        lambda records: records[0]["payload"].__setitem__("replay_candidate_sha256", "f" * 64),
        "candidate_identity_mismatch",
    ),
    "deterministic_performance_resource_proof": (
        lambda records: records[0]["payload"].__setitem__("output_sha256", "f" * 64),
        "nondeterministic_outputs",
    ),
}


def _scorecard_platform_evidence(
    tmp_path, mode: str, subjects: list[dict[str, Any]], *, candidate_commit: str
):
    reports = tmp_path / f"score-platform-reports-{mode}"
    paths = []
    for system in SYSTEMS:
        machine = sorted(REQUIRED_PLATFORM_MACHINES[system])[0]
        wheel, native = _package_digests(system)
        path = reports / f"{system}.json"
        write_json(
            path,
            {
                "schema_version": "1.2.0",
                "complete": True,
                "lane": EXACT_FIXTURE_LANE,
                "native_scorecard_subjects": subjects,
                "platform": {"system": system, "machine": machine, "wsl": False},
                "package": {
                    "version": "1.6.1",
                    "wheel_sha256": wheel,
                    "native_extension_sha256": native,
                    "installed_extension_sha256": native,
                    "installed_extension_equal": True,
                    "portable_package_sha256": "e" * 64,
                },
                "workload": _workload(mode),
                "measurement": {
                    "result_sha256": ["d" * 64],
                    "wall_time_seconds": {"median": 1.0},
                    "peak_rss_bytes": {"maximum": 1000},
                    "measured_repetitions": 3,
                },
            },
        )
        sign_report(
            path,
            run_id=1,
            candidate_id=IDENTITY["engine_artifact_sha256"],
            commit=candidate_commit,
            nonce=_nonce(mode, system),
        )
        paths.append(path)
    sealed = tmp_path / f"score-platform-{mode}"
    seal_platform_evidence(paths, sealed, provenance_policy=TEST_POLICY)
    shutil.rmtree(reports)
    for extra in sealed.iterdir():
        if extra.name != "platform-evidence.json":
            extra.unlink()
    return sealed / "platform-evidence.json"


def _domain_producer(record: dict[str, Any]) -> dict[str, Any]:
    issued_at = _EVIDENCE_ISSUED.isoformat().replace("+00:00", "Z")
    expires_at = _EVIDENCE_EXPIRES.isoformat().replace("+00:00", "Z")
    role = native_score_producer_role(record["record_type"])
    return {
        "role": role,
        "identity_sha256": producer_identity(
            role=role,
            candidate_identity_sha256=record["candidate_identity_sha256"],
            workload_sha256=record["workload_sha256"],
            run_id=record["run_id"],
            nonce=record["nonce"],
            issued_at=issued_at,
            expires_at=expires_at,
        ),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "run_id": record["run_id"],
        "nonce": record["nonce"],
    }


def _domain_context(record: dict[str, Any]) -> dict[str, Any]:
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


def _full_x7_certificate(record: dict[str, Any]) -> dict[str, Any]:
    mode = record["mode_contract"]
    futures = mode == FUTURES_RELEASE_CONTRACT_ID
    return {
        "schema_version": "2.0.0",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "certified",
        "release_certified": True,
        "claim_scope": {
            "strategy": "NostalgiaForInfinityX7",
            "upstream_commit": TEST_COMMIT,
            "mode_contract": mode,
            "trading_mode": "futures" if futures else "spot",
            "margin_mode": "isolated" if futures else None,
            "exchange": "binance",
            "settlement_currency": "USDT",
            "required_data_roles": ["candles", "funding_rate", "mark"] if futures else ["candles"],
            "timerange": "20210101-20260101",
            "pair_count": 80,
            "timeframes": ["5m", "15m", "1h", "4h", "1d"],
            "continuous_timerange": True,
            "history_coverage_policy": "listing-aware" if futures else "strict",
            "evidence": "continuous-oracle-plus-official-full-state-probes",
        },
        "inputs": {
            "release_lock": {},
            "mode_contract": {},
            "reference": {},
            "strategy_sha256": record["source_identity_sha256"],
            "config_sha256": "3" * 64,
            "data_aggregate_sha256": "4" * 64,
            "engine_market_snapshot_sha256": "5" * 64,
            "reference_market_snapshot_sha256": "5" * 64,
            "native_score_identity": _domain_context(record),
        },
        "environment": {
            "hardware": {},
            "execution_profile": {},
            "package_version": "1.6.1",
            "engine_build": {"source_fingerprint": record["candidate_identity_sha256"]},
        },
        "measurement": {
            "native_warmups_excluded": 1,
            "native_initial_repetitions": 3,
            "native_measured_repetitions": 3,
            "native_maximum_repetitions": 5,
            "native_spread_threshold": 0.05,
            "engine_relative_spread": 0.01,
            "official_reference_repetitions": 1,
            "official_reference_role": "single-continuous-exact-parity-oracle",
            "resumed": False,
        },
        "runs": {
            "engine": [{}, {}, {}],
            "official_reference": {},
            "engine_summary": {},
            "official_reference_summary": {},
        },
        "state_probes": [{}, {}, {}],
        "gates": {
            "input_lock": {"met": True},
            "installed_wheel": {"met": True},
            "native_pipeline": {"met": True},
            "official_parity": {"met": True},
            "determinism": {"met": True},
            "speed": {"met": True},
            "memory": {"met": True},
            "state_probes": {"met": True},
        },
    }


def _domain_input_document(
    record: dict[str, Any], field: str, original_identity: str
) -> dict[str, Any]:
    record_type = record["record_type"]
    if record_type == "portfolio_certificate":
        if field == "certificate_sha256":
            return _full_x7_certificate(record)
        return {
            "schema_version": REPLAY_EVIDENCE_VERSION,
            "certificate_sha256": record["payload"]["certificate_sha256"],
            "candidate_identity_sha256": record["candidate_identity_sha256"],
            "context": _domain_context(record),
            "exact": True,
            "complete": True,
        }
    if record_type == "performance_process_sample":
        payload = record["payload"]
        return {
            "schema_version": PROCESS_EVIDENCE_VERSION,
            "producer": _domain_producer(record),
            "context": _domain_context(record),
            "runtime": payload["runtime"],
            "population": payload["population"],
            "sample_index": payload["sample_index"],
            "process": {
                "exit_code": 0,
                "wall_seconds": payload["wall_seconds"],
                "cpu_seconds": payload["wall_seconds"] / 2,
                "peak_rss_bytes": payload["peak_rss_bytes"],
                "memory_limit_bytes": payload["memory_limit_bytes"],
                "oom": payload["oom"],
                "swap_bytes": payload["swap_bytes"],
            },
            "output": {"sha256": original_identity, "bytes": 1, "bounded": True},
        }
    document_types = {
        "producer_run": "observer-trace",
        "execution_trace": "native-execution-trace",
        "semantic_obligation": "semantic-obligation-coverage",
        "obligation_coverage": "coverage-witness",
        "changed_target": "coverage-witness",
        "mcdc_term": "coverage-witness",
        "transition": "coverage-witness",
        "vector": "exact-comparison",
        "decision": "exact-comparison",
        "callback": "exact-comparison",
        "state_delta": "exact-comparison",
        "execution_state": "complete-state-trace",
        "generative_case": "generated-corpus",
        "metamorphic_case": "generated-corpus",
        "mutant_outcome": "mutation-execution",
    }
    payload = record["payload"]
    if record_type == "semantic_obligation":
        registry_identity = (
            native_scorecard._offline_nonpromotional_semantic_registry_identity()
        )
        count = registry_identity["total_obligations"]
        coverage = bytearray([0xFF]) * ((count + 7) // 8)
        if count % 8:
            coverage[-1] = (1 << (count % 8)) - 1
        observation = {
            "registry_fingerprint": registry_identity["registry_fingerprint"],
            "obligation_count": count,
            "ordered_coverage_encoding": "registry-order-bitset-v1",
            "coverage_bits_base64": base64.b64encode(coverage).decode(),
        }
    elif record_type == "producer_run":
        observation = {
            "events": [{"sequence": 0, "event_sha256": original_identity}],
            "complete": True,
        }
    elif record_type == "execution_trace":
        observation = {"events": payload["events"], "complete": True}
    elif record_type in {"obligation_coverage", "changed_target", "mcdc_term", "transition"}:
        observation = {
            "semantic_id": payload["target_id"],
            "observations": [{"sequence": 0, "witness_sha256": original_identity, "reached": True}],
        }
    elif record_type in {"vector", "decision", "callback", "state_delta"}:
        observation = {
            "semantic_id": payload["comparison_id"],
            "records": [{"sequence": 0, "value_sha256": original_identity}],
        }
    elif record_type == "execution_state":
        observation = {
            "semantic_id": payload["event_id"],
            "state_fields": payload["state_fields"],
            "events": [{"sequence": 0, "value_sha256": original_identity}],
        }
    elif record_type in {"generative_case", "metamorphic_case"}:
        observation = {
            "domain": "callback-state-machine",
            "case_id": payload["case_id"],
            "seed": payload["seed"],
            "records": [{"sequence": 0, "value_sha256": original_identity}],
        }
        if record_type == "metamorphic_case":
            observation["relation"] = payload["relation"]
    else:
        observation = {
            "mutant_id": payload["mutant_id"],
            "operator": payload["operator"],
            "executions": [{"sequence": 0, "result_sha256": original_identity}],
        }
    return {
        "schema_version": DOMAIN_EVIDENCE_VERSION,
        "document_type": document_types[record_type],
        "producer": _domain_producer(record),
        "context": _domain_context(record),
        "observation": observation,
    }


def _materialize_machine_records(
    tmp_path,
    records_by_gate: dict[str, list[dict[str, Any]]],
    *,
    opaque_machine_inputs: bool = False,
) -> None:
    for gate_id, records in records_by_gate.items():
        for index, record in enumerate(records):
            source = tmp_path / "machine-records" / gate_id / f"{index:04d}.json"
            producer_role = native_score_producer_role(record["record_type"])
            inputs = []
            for field in native_scorecard._MACHINE_INPUT_FIELDS_BY_TYPE.get(
                record["record_type"], ()
            ):
                if field not in record["payload"]:
                    continue
                original_identity = record["payload"][field]
                input_path = (
                    tmp_path / "machine-inputs" / gate_id / f"{index:04d}" / f"{field}.json"
                )
                write_json(
                    input_path,
                    {"machine_value": original_identity}
                    if opaque_machine_inputs
                    else _domain_input_document(record, field, original_identity),
                )
                input_sha256 = sha256_file(input_path)
                if record["record_type"] != "performance_process_sample":
                    record["payload"][field] = input_sha256
                inputs.append(
                    {
                        "field": field,
                        "path": input_path.relative_to(tmp_path).as_posix(),
                        "sha256": input_sha256,
                    }
                )
            write_json(
                source,
                {
                    "schema_version": "1.0.0",
                    "gate_id": gate_id,
                    "record_type": record["record_type"],
                    "semantic_identity": native_scorecard._semantic_identity(record),
                    "producer": {
                        "role": producer_role,
                        "identity_sha256": record["candidate_identity_sha256"],
                        "run_id": record["run_id"],
                    },
                    "context": {
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
                    },
                    "inputs": inputs,
                    "observation": record["payload"],
                },
            )
            record["source_artifact"] = {
                "path": source.relative_to(tmp_path).as_posix(),
                "sha256": sha256_file(source),
            }


def _scorecard_inputs(
    tmp_path,
    *,
    mutations: dict[str, Callable[[list[dict[str, Any]]], None]] | None = None,
    mode_subject_mutations=None,
    scalar_only: bool = False,
    recompute_record_ids: bool = True,
    sort_record_ids: bool = True,
    engine_artifact_sha256: str | None = None,
    candidate_commit: str = TEST_COMMIT,
    opaque_machine_inputs: bool = False,
):
    saved_engine_identity = IDENTITY["engine_artifact_sha256"]
    if engine_artifact_sha256 is not None:
        IDENTITY["engine_artifact_sha256"] = engine_artifact_sha256
    expected_identity = tmp_path / "identity.json"
    write_json(
        expected_identity,
        {
            "schema_version": "1.0.0",
            **IDENTITY,
            "candidate_commit": candidate_commit,
            "bundle_id": _bundle_id(),
            "challenge": TEST_CHALLENGE,
        },
    )
    raw_by_gate = _raw_records(candidate_commit=candidate_commit)
    if mutations:
        for gate_id, mutate in mutations.items():
            mutate(raw_by_gate[gate_id])
    _materialize_machine_records(
        tmp_path,
        raw_by_gate,
        opaque_machine_inputs=opaque_machine_inputs,
    )
    proof_records = {}
    subjects = []
    for gate_id in NATIVE_SCORE_GATE_IDS:
        artifact_path = tmp_path / "artifacts" / f"{gate_id}.json"
        records = raw_by_gate[gate_id]
        if recompute_record_ids:
            for record in records:
                record["record_id"] = native_score_record_id(record)
            if sort_record_ids:
                records.sort(key=lambda item: item["record_id"])
        artifact = (
            {
                "schema_version": "1.0.0",
                "gate_id": gate_id,
                "identity": copy.deepcopy(IDENTITY),
                "observation": {"authenticated_scalar": 1},
                "status": "passed",
                "exact": True,
                "complete": True,
                "official_only": False,
                "execution_route": "native",
            }
            if scalar_only
            else {
                "schema_version": "1.0.0",
                "evaluator_version": "native-score-evaluator-v3",
                "gate_id": gate_id,
                "identity": copy.deepcopy(IDENTITY),
                "records": records,
            }
        )
        write_json(artifact_path, artifact)
        artifact_sha = sha256_file(artifact_path)
        subject: dict[str, Any] = {
            "schema_version": "1.0.0",
            "gate_id": gate_id,
            "artifact_sha256": artifact_sha,
        }
        if not scalar_only:
            subject.update(
                raw_record_schema_version="1.0.0",
                evaluator_version="native-score-evaluator-v3",
                expected_record_count=len(records),
                record_sha256s=[canonical_sha256(item) for item in records],
            )
        subjects.append(subject)
        proof_path = tmp_path / "proofs" / f"{gate_id}.json"
        write_json(
            proof_path,
            {
                "schema_version": "1.0.0",
                "gate_id": gate_id,
                "identity": copy.deepcopy(IDENTITY),
                "artifact": {
                    "path": artifact_path.relative_to(tmp_path).as_posix(),
                    "sha256": artifact_sha,
                },
            },
        )
        proof_records[gate_id] = {
            "path": proof_path.relative_to(tmp_path).as_posix(),
            "sha256": sha256_file(proof_path),
        }
    subject_graphs = []
    for mode in MODES:
        mode_subjects = copy.deepcopy(subjects)
        if mode_subject_mutations and mode in mode_subject_mutations:
            mode_subject_mutations[mode](mode_subjects)
        graph = _scorecard_platform_evidence(
            tmp_path, mode, mode_subjects, candidate_commit=candidate_commit
        )
        subject_graphs.append(
            {
                "mode_contract": mode,
                "path": graph.relative_to(tmp_path).as_posix(),
                "sha256": sha256_file(graph),
            }
        )
    manifest = tmp_path / "score-evidence.json"
    write_json(
        manifest,
        {
            "schema_version": "1.0.0",
            "identity": IDENTITY,
            "proofs": proof_records,
            "subject_graphs": subject_graphs,
        },
    )
    IDENTITY["engine_artifact_sha256"] = saved_engine_identity
    return manifest, expected_identity


def _rewrite_artifact(tmp_path, manifest, gate_id, mutate):
    document = json.loads(manifest.read_text(encoding="utf-8"))
    proof_path = tmp_path / document["proofs"][gate_id]["path"]
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    artifact_path = tmp_path / proof["artifact"]["path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    mutate(artifact)
    write_json(artifact_path, artifact)
    proof["artifact"]["sha256"] = sha256_file(artifact_path)
    write_json(proof_path, proof)
    document["proofs"][gate_id]["sha256"] = sha256_file(proof_path)
    write_json(manifest, document)


def test_opaque_machine_value_files_cannot_stand_in_for_domain_evidence(tmp_path) -> None:
    manifest, identity = _scorecard_inputs(tmp_path, opaque_machine_inputs=True)
    output = tmp_path / "opaque-domain-report.json"

    with pytest.raises(SpecValidationError, match="native-score-domain-evidence"):
        evaluate_native_scorecard(
            manifest,
            expected_identity_path=identity,
            output_path=output,
        )

    assert not output.exists()


def test_future_issued_typed_domain_graph_is_rejected_before_publication(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    future_issued = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)
    monkeypatch.setattr(sys.modules[__name__], "_EVIDENCE_ISSUED", future_issued)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_EVIDENCE_EXPIRES",
        future_issued + timedelta(hours=23),
    )
    manifest, identity = _scorecard_inputs(tmp_path)
    output = tmp_path / "future-domain-report.json"

    with pytest.raises(SpecValidationError, match="future"):
        evaluate_native_scorecard(
            manifest,
            expected_identity_path=identity,
            output_path=output,
        )

    assert not output.exists()


def test_unauthorized_typed_domain_producer_graph_is_rejected_before_publication(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _domain_producer

    def unauthorized(record: dict[str, Any]) -> dict[str, Any]:
        producer = original(record)
        role = "wrong-independent-producer-role"
        producer["role"] = role
        producer["identity_sha256"] = producer_identity(
            role=role,
            candidate_identity_sha256=record["candidate_identity_sha256"],
            workload_sha256=record["workload_sha256"],
            run_id=record["run_id"],
            nonce=record["nonce"],
            issued_at=producer["issued_at"],
            expires_at=producer["expires_at"],
        )
        return producer

    monkeypatch.setattr(sys.modules[__name__], "_domain_producer", unauthorized)
    manifest, identity = _scorecard_inputs(tmp_path)
    output = tmp_path / "unauthorized-domain-report.json"

    with pytest.raises(SpecValidationError, match="producer role is unauthorized"):
        evaluate_native_scorecard(
            manifest,
            expected_identity_path=identity,
            output_path=output,
        )

    assert not output.exists()


def test_signed_scalar_only_fixture_is_rejected_before_staging(tmp_path) -> None:
    manifest, identity = _scorecard_inputs(tmp_path, scalar_only=True)
    output = tmp_path / "scalar-only-report.json"
    with pytest.raises(SpecValidationError, match="raw records"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity, output_path=output)
    assert not output.exists()


def test_opaque_record_ids_are_rejected_even_when_signed(tmp_path) -> None:
    def opaque(records):
        for index, record in enumerate(records):
            record["record_id"] = f"opaque-self-asserted-{index:03d}"

    manifest, identity = _scorecard_inputs(
        tmp_path,
        mutations={"semantic_closure": opaque},
        recompute_record_ids=False,
    )
    with pytest.raises(SpecValidationError, match="canonical record id"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)


def test_duplicate_identity_component_semantics_are_rejected(tmp_path) -> None:
    def duplicate(records):
        for record in records:
            if record["record_type"] == "identity_component":
                record["payload"] = {
                    "component": "source_closure_sha256",
                    "digest": IDENTITY["source_closure_sha256"],
                }

    manifest, identity = _scorecard_inputs(
        tmp_path, mutations={"immutable_identity_scope": duplicate}
    )
    with pytest.raises(SpecValidationError, match="semantic identity"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)


def test_score_registry_identity_is_derived_from_authoritative_packaged_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_identity = {
        "compressed_sha256": "a" * 64,
        "uncompressed_bytes": 123,
        "uncompressed_sha256": "b" * 64,
        "registry_fingerprint": "c" * 64,
    }
    registry = {
        "summary": {"total_obligations": 1},
        "obligation_groups": [
            {
                "mapping": "compiled-program",
                "obligations": [{"obligation_id": "obligation-1"}],
            }
        ]
    }
    monkeypatch.setattr(
        native_scorecard,
        "packaged_semantic_obligation_registry_identity",
        lambda: manifest_identity,
    )
    monkeypatch.setattr(
        native_scorecard,
        "load_immutable_packaged_semantic_registry_for_offline_audit",
        lambda: registry,
    )
    native_scorecard._offline_nonpromotional_semantic_registry_identity.cache_clear()

    try:
        derived = native_scorecard._offline_nonpromotional_semantic_registry_identity()
    finally:
        native_scorecard._offline_nonpromotional_semantic_registry_identity.cache_clear()

    assert derived["registry_fingerprint"] == "c" * 64
    assert derived["total_obligations"] == 1
    assert derived["native_promotion"] is False


def test_release_score_cli_forwards_publication_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def evaluate(_evidence, **kwargs):
        observed.append(kwargs["authorization_operation"])
        return {"perfect_native": True}

    monkeypatch.setattr(native_scorecard, "evaluate_native_scorecard", evaluate)
    args = cli.build_parser().parse_args(
        [
            "release",
            "score",
            "--evidence",
            "score-evidence.json",
            "--identity",
            "identity.json",
            "--operation",
            "product-candidate-create:v1.0.0-rc.1:" + TEST_COMMIT,
        ]
    )

    assert execute_release(args) == 0
    assert observed == ["product-candidate-create:v1.0.0-rc.1:" + TEST_COMMIT]


def test_product_publication_authorizations_bind_distinct_complete_preimages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    captured: list[CurrentRefAuthorization] = []
    monkeypatch.setattr(
        native_scorecard,
        "begin_packaged_semantic_registry_authorization",
        lambda authorization: captured.append(authorization)
        or _current_ref_proof(authorization),
    )
    candidate_operation = "product-candidate-create:v1.0.0-rc.1:" + TEST_COMMIT
    stable_operation = "product-stable-create:v1.0.0:" + TEST_COMMIT

    evaluate_native_scorecard(
        manifest,
        expected_identity_path=identity,
        authorization_operation=candidate_operation,
    )
    evaluate_native_scorecard(
        manifest,
        expected_identity_path=identity,
        authorization_operation=stable_operation,
    )

    records = _raw_records()
    expected_common = {
        "candidate_commit": TEST_COMMIT,
        "candidate_identity_sha256": IDENTITY["engine_artifact_sha256"],
        "source_closure_sha256": IDENTITY["source_closure_sha256"],
        "workload_run_nonce_sha256": canonical_sha256(
            {
                "contexts": sorted(
                    {
                        (
                            str(record["workload_sha256"]),
                            str(record["run_id"]),
                            str(record["nonce"]),
                        )
                        for gate_records in records.values()
                        for record in gate_records
                    }
                )
            }
        ),
    }
    assert [authorization.operation for authorization in captured] == [
        candidate_operation,
        stable_operation,
    ]
    assert [
        {
            "candidate_commit": authorization.candidate_commit,
            "candidate_identity_sha256": authorization.candidate_identity_sha256,
            "source_closure_sha256": authorization.source_closure_sha256,
            "workload_run_nonce_sha256": authorization.workload_run_nonce_sha256,
        }
        for authorization in captured
    ] == [expected_common, expected_common]
    assert captured[0].digest != captured[1].digest


@pytest.mark.parametrize(
    ("requested_operation", "proof_operation"),
    [
        (
            "product-stable-create:v1.0.0:" + TEST_COMMIT,
            "product-candidate-create:v1.0.0-rc.1:" + TEST_COMMIT,
        ),
        (
            "product-candidate-create:v1.0.0-rc.1:" + TEST_COMMIT,
            "product-stable-create:v1.0.0:" + TEST_COMMIT,
        ),
    ],
)
def test_score_evaluator_rejects_intact_cross_operation_proof_before_consequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_operation: str,
    proof_operation: str,
) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    output = tmp_path / "score.json"
    reservation = tmp_path / "reservation.json"
    external_command = tmp_path / "external-command-invoked"
    private_residue = tmp_path / ".score-private"
    final_observations: list[str] = []
    publications: list[Path] = []

    def swapped_proof(requested: CurrentRefAuthorization) -> PackagedRegistryCurrentRefProof:
        assert requested.operation == requested_operation
        swapped = replace(requested, operation=proof_operation)
        return PackagedRegistryCurrentRefProof(
            authorization=swapped,
            authorization_digest=swapped.digest,
            packaged_commit="eebaf97c1434bd8f208b7cd9c417606646e1e478",
            packaged_source_closure_sha256=IDENTITY["source_closure_sha256"],
            initial_observed_commit="eebaf97c1434bd8f208b7cd9c417606646e1e478",
            document={"source_closure": {"merkle_root": IDENTITY["source_closure_sha256"]}},
        )

    monkeypatch.setattr(
        native_scorecard,
        "begin_packaged_semantic_registry_authorization",
        swapped_proof,
    )
    monkeypatch.setattr(
        native_scorecard,
        "finalize_packaged_semantic_registry_authorization",
        finalize_packaged_semantic_registry_authorization,
    )
    monkeypatch.setattr(
        "nfi_backtest_engine.semantic_registry._observe_packaged_registry_current_ref",
        lambda *_args, **_kwargs: final_observations.append("observed") or TEST_COMMIT,
    )
    monkeypatch.setattr(
        native_scorecard,
        "_publish_report_atomic",
        lambda destination, _report: publications.append(destination),
    )

    with pytest.raises(SpecValidationError, match="requested authorization differs"):
        evaluate_native_scorecard(
            manifest,
            expected_identity_path=identity,
            authorization_operation=requested_operation,
            output_path=output,
        )

    assert final_observations == []
    assert publications == []
    assert not output.exists()
    assert not reservation.exists()
    assert not external_command.exists()
    assert not private_residue.exists()


def test_current_ref_proof_rejects_cross_operation_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = CurrentRefAuthorization(
        operation="product-candidate-create:v1.0.0-rc.1:" + TEST_COMMIT,
        candidate_commit=TEST_COMMIT,
        candidate_identity_sha256=IDENTITY["engine_artifact_sha256"],
        source_closure_sha256=IDENTITY["source_closure_sha256"],
        workload_run_nonce_sha256="5" * 64,
    )
    stable = replace(candidate, operation="product-stable-create:v1.0.0:" + TEST_COMMIT)
    proof = PackagedRegistryCurrentRefProof(
        authorization=candidate,
        authorization_digest=candidate.digest,
        packaged_commit=TEST_COMMIT,
        packaged_source_closure_sha256=IDENTITY["source_closure_sha256"],
        initial_observed_commit=TEST_COMMIT,
        document={"source_closure": {"merkle_root": IDENTITY["source_closure_sha256"]}},
    )
    swapped = replace(proof, authorization=stable)
    monkeypatch.setattr(
        "nfi_backtest_engine.semantic_registry.packaged_semantic_obligation_registry_identity",
        lambda: {"upstream_commit": TEST_COMMIT},
    )

    with pytest.raises(SpecValidationError, match="authorization digest differs"):
        finalize_packaged_semantic_registry_authorization(swapped)


def test_release_authorize_current_uses_operation_bound_fresh_proof(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[tuple[Path, Path, str]] = []
    monkeypatch.setattr(
        native_scorecard,
        "require_fresh_current_ref_for_authorization",
        lambda evidence, identity, operation: observed.append(
            (Path(evidence), Path(identity), operation)
        ),
    )
    args = cli.build_parser().parse_args(
        [
            "release",
            "authorize-current",
            "--evidence",
            "score-evidence.json",
            "--identity",
            "identity.json",
            "--operation",
            "combined-draft-upload:rc:commit",
        ]
    )

    exit_code = execute_release(args)

    assert exit_code == 0
    assert observed == [
        (
            Path("score-evidence.json"),
            Path("identity.json"),
            "combined-draft-upload:rc:commit",
        )
    ]
    assert capsys.readouterr().out == "current-ref authorization valid\n"


def test_two_score_authorizations_reobserve_current_ref_after_movement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    authorizations = []

    def begin(authorization):
        authorizations.append(authorization)
        if len(authorizations) == 2:
            raise PackagedRegistryCurrentRefError(
                code="STALE_UPSTREAM_REF",
                observation_method="git-fetch-depth-1-v1",
                observation_status="stale",
                repository="https://example.invalid/nfi.git",
                ref="refs/heads/main",
                packaged_commit="a" * 40,
                observed_commit="b" * 40,
            )
        return _current_ref_proof(authorization)

    monkeypatch.setattr(
        native_scorecard,
        "begin_packaged_semantic_registry_authorization",
        begin,
    )
    evaluate_native_scorecard(manifest, expected_identity_path=identity)

    with pytest.raises(PackagedRegistryCurrentRefError) as moved:
        evaluate_native_scorecard(manifest, expected_identity_path=identity)

    assert moved.value.evidence["native_promotion"] is False
    assert len(authorizations) == 2
    assert authorizations[0].digest == authorizations[1].digest


def test_final_current_ref_movement_creates_no_score_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    output = tmp_path / "score.json"

    def moved(_proof):
        raise PackagedRegistryCurrentRefError(
            code="UPSTREAM_REF_MOVED_DURING_AUTHORIZATION",
            observation_method="git-fetch-depth-1-v1",
            observation_status="moved",
            repository="https://example.invalid/nfi.git",
            ref="refs/heads/main",
            packaged_commit="a" * 40,
            observed_commit="b" * 40,
        )

    monkeypatch.setattr(
        native_scorecard,
        "finalize_packaged_semantic_registry_authorization",
        moved,
    )

    with pytest.raises(PackagedRegistryCurrentRefError) as movement:
        evaluate_native_scorecard(
            manifest,
            expected_identity_path=identity,
            output_path=output,
        )

    assert movement.value.evidence["native_promotion"] is False
    assert not output.exists()


def test_registry_fingerprint_summary_rows_cannot_replace_all_obligations(tmp_path) -> None:
    fingerprint = packaged_semantic_obligation_registry_identity()["registry_fingerprint"]

    def summarize(records):
        for record in records:
            record["payload"]["obligation_id"] = fingerprint
            record["payload"]["registry_sha256"] = fingerprint

    manifest, identity = _scorecard_inputs(tmp_path, mutations={"semantic_closure": summarize})
    with pytest.raises(
        SpecValidationError,
        match="authoritative (registry|universe)|payload identity",
    ):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)


def test_invented_obligation_universe_is_rejected(tmp_path) -> None:
    def invent(records):
        for record in records:
            record["payload"]["obligation_id"] = "invented-one-row-universe"

    manifest, identity = _scorecard_inputs(tmp_path, mutations={"semantic_closure": invent})
    with pytest.raises(SpecValidationError, match="authoritative universe"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)


def test_packaged_schema_rejects_payload_the_evaluator_rejects() -> None:
    artifact = {
        "schema_version": "1.0.0",
        "evaluator_version": "native-score-evaluator-v3",
        "gate_id": "native_purity",
        "identity": IDENTITY,
        "records": [
            {
                "schema_version": "1.0.0",
                "gate_id": "native_purity",
                "record_id": "opaque",
                "record_type": "execution_trace",
                "source_identity_sha256": "1" * 64,
                "candidate_identity_sha256": "2" * 64,
                "mode_contract": SPOT_RELEASE_CONTRACT_ID,
                "platform": "linux",
                "workload_sha256": "3" * 64,
                "run_id": "1",
                "nonce": "4" * 64,
                "source_artifact": {"path": "machine.json", "sha256": "5" * 64},
                "payload": {"anything": "previously accepted"},
            }
        ],
    }
    with pytest.raises(SpecValidationError):
        validate_schema(artifact, NATIVE_SCORE_RAW_EVIDENCE_SCHEMA)


@pytest.mark.parametrize("gate_id", NATIVE_SCORE_GATE_IDS)
def test_schema_and_evaluator_reject_malformed_payloads_in_parity(tmp_path, gate_id) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    proof = json.loads((tmp_path / document["proofs"][gate_id]["path"]).read_text(encoding="utf-8"))
    artifact_path = tmp_path / proof["artifact"]["path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["records"][0]["payload"] = {}
    with pytest.raises(SpecValidationError):
        validate_schema(artifact, NATIVE_SCORE_RAW_EVIDENCE_SCHEMA)
    _rewrite_artifact(
        tmp_path,
        manifest,
        gate_id,
        lambda target: target["records"][0].__setitem__("payload", {}),
    )
    with pytest.raises(SpecValidationError):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)


def test_interrupted_score_publication_leaves_no_partial_or_stage(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    output = tmp_path / "score.json"

    def interrupt(name: str) -> None:
        if name == "after-stage-fsync":
            raise KeyboardInterrupt

    monkeypatch.setattr(native_scorecard, "_publication_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        evaluate_native_scorecard(manifest, expected_identity_path=identity, output_path=output)
    assert not output.exists()
    assert not list(tmp_path.glob(".score.json.stage-*"))


@pytest.mark.parametrize(
    "schema_name",
    (
        "native-score-raw-evidence-v1.schema.json",
        "native-score-machine-record-v1.schema.json",
        "native-score-domain-evidence-v1.schema.json",
    ),
)
def test_installed_score_schema_substitution_rejects_before_evidence_parsing(
    tmp_path, monkeypatch: pytest.MonkeyPatch, schema_name: str
) -> None:
    from nfi_backtest_engine import schemas

    package = tmp_path / "schemas"
    shutil.copytree(next(iter(schemas.__path__)), package)
    (package / schema_name).write_text(
        '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(schemas, "__path__", [str(package)])
    manifest = tmp_path / "untrusted.json"
    manifest.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SpecValidationError, match="NATIVE_SCORE_SCHEMA_IDENTITY"):
        evaluate_native_scorecard(manifest, expected_identity_path=manifest)


def test_stale_resealed_machine_producer_run_is_unauthorized(tmp_path) -> None:
    records = _raw_records()
    _materialize_machine_records(tmp_path, records)
    record = records["native_purity"][0]
    source = tmp_path / record["source_artifact"]["path"]
    machine = json.loads(source.read_text(encoding="utf-8"))
    machine["producer"]["run_id"] = "stale-producer-run-from-1970"
    write_json(source, machine)
    record["source_artifact"]["sha256"] = sha256_file(source)
    record["record_id"] = native_score_record_id(record)
    with pytest.raises(SpecValidationError, match="stale or unauthorized"):
        native_scorecard._verify_machine_record(
            record,
            root=tmp_path,
            verification_clock=VerificationClockPolicy.capture(),
        )


def test_score_for_another_commit_or_package_cannot_authorize_promotion(tmp_path) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    with pytest.raises(SpecValidationError, match="candidate commit differs"):
        native_scorecard.require_native_scorecard_for_promotion(
            manifest,
            expected_identity_path=identity,
            expected_candidate_commit="f" * 40,
            provenance_policy=TEST_POLICY,
        )
    with pytest.raises(SpecValidationError, match="candidate package identity differs"):
        native_scorecard.require_native_scorecard_for_promotion(
            manifest,
            expected_identity_path=identity,
            expected_candidate_identity="f" * 64,
            provenance_policy=TEST_POLICY,
        )


def test_post_link_score_publication_failure_is_recoverable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "score.json"

    def interrupt(name: str) -> None:
        if name == "after-atomic-publication":
            raise OSError("simulated directory durability failure")

    monkeypatch.setattr(native_scorecard, "_publication_checkpoint", interrupt)
    with pytest.raises(OSError, match="durability"):
        native_scorecard._publish_report_atomic(output, {"sentinel": "complete"})
    assert not output.exists()
    assert not list(tmp_path.glob(".score.json.stage-*"))
    monkeypatch.setattr(native_scorecard, "_publication_checkpoint", lambda _name: None)
    native_scorecard._publish_report_atomic(output, {"sentinel": "complete"})
    assert json.loads(output.read_text(encoding="utf-8")) == {"sentinel": "complete"}


def test_release_promotion_cannot_bypass_missing_scorecard() -> None:
    require = getattr(native_scorecard, "require_native_scorecard_for_promotion", None)
    assert callable(require)
    with pytest.raises(SpecValidationError, match="required"):
        require(None)


def test_complete_raw_scorecard_recomputes_exactly_ten_points(tmp_path) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    report = evaluate_native_scorecard(manifest, expected_identity_path=identity)
    assert tuple(item["id"] for item in report["gates"]) == NATIVE_SCORE_GATE_IDS
    assert report["points_awarded"] == report["points_available"] == 10, [
        gate["failures"] for gate in report["gates"] if not gate["met"]
    ]
    assert report["perfect_native"] is True


@pytest.mark.parametrize("gate_id", tuple(RAW_FAILURES))
def test_each_contradictory_raw_record_withholds_exactly_its_point_and_report(
    tmp_path, gate_id
) -> None:
    mutate, expected_failure = RAW_FAILURES[gate_id]
    manifest, identity = _scorecard_inputs(tmp_path, mutations={gate_id: mutate})
    output = tmp_path / "not-perfect.json"
    report = evaluate_native_scorecard(
        manifest, expected_identity_path=identity, output_path=output
    )
    assert report["points_awarded"] == 9
    assert report["perfect_native"] is False
    assert not output.exists()
    failed = next(item for item in report["gates"] if item["id"] == gate_id)
    assert failed["failures"] == [expected_failure]
    assert all(item["met"] == (item["id"] != gate_id) for item in report["gates"])


@pytest.mark.parametrize("gate_id", NATIVE_SCORE_GATE_IDS)
def test_unsigned_raw_or_aggregate_resealing_cannot_increase_score(tmp_path, gate_id) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    _rewrite_artifact(
        tmp_path,
        manifest,
        gate_id,
        lambda artifact: artifact.update(status="passed", point_total=1),
    )
    with pytest.raises(SpecValidationError, match="unexpected"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)


@pytest.mark.parametrize(
    "mutation,match,sort_ids",
    (
        (
            lambda records: records.pop(),
            "too short|incomplete mode/platform cardinality",
            True,
        ),
        (
            lambda records: records.append(copy.deepcopy(records[0])),
            "too long|duplicate semantic identity",
            True,
        ),
        (
            lambda records: records.__setitem__(slice(0, 2), [records[1], records[0]]),
            "reordered",
            False,
        ),
        (
            lambda records: records[0].__setitem__("record_type", "callback"),
            "unexpected|semantic_obligation.*expected",
            True,
        ),
    ),
    ids=("missing", "duplicate", "reordered", "cross-gate"),
)
def test_invalid_raw_leaf_sets_reject_before_staging(tmp_path, mutation, match, sort_ids) -> None:
    gate = "semantic_closure"
    manifest, identity = _scorecard_inputs(
        tmp_path, mutations={gate: mutation}, sort_record_ids=sort_ids
    )
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(SpecValidationError, match=match):
        evaluate_native_scorecard(manifest, expected_identity_path=identity, output_path=output)
    assert not output.exists()


def test_combined_scalar_performance_rows_are_rejected(tmp_path) -> None:
    def combine_scalar(records):
        for record in records:
            record["record_type"] = "performance_sample"
            record["payload"] = {
                "sample_index": 0,
                "cold": True,
                "official_wall_seconds": 120.0,
                "native_wall_seconds": 10.0,
                "peak_rss_bytes": 1000,
                "memory_limit_bytes": 2000,
                "oom": False,
                "swap_bytes": 0,
                "output_sha256": "2" * 64,
            }

    with pytest.raises(SpecValidationError, match="unknown native score raw record type"):
        _scorecard_inputs(
            tmp_path,
            mutations={"deterministic_performance_resource_proof": combine_scalar},
        )


def test_unused_dependency_artifact_rejects_closed_dag(tmp_path) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    (tmp_path / "unused-machine-claim.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SpecValidationError, match="unused artifact"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)


def test_leaf_context_and_subject_digest_are_signed(tmp_path) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    _rewrite_artifact(
        tmp_path,
        manifest,
        "native_purity",
        lambda artifact: artifact["records"][0].__setitem__("nonce", "f" * 64),
    )
    with pytest.raises(SpecValidationError, match="canonical record id|subjects or raw leaves"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)


def test_futures_only_and_cross_mode_contradictory_graphs_reject(tmp_path) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["subject_graphs"] = [
        item
        for item in document["subject_graphs"]
        if item["mode_contract"] == FUTURES_RELEASE_CONTRACT_ID
    ]
    write_json(manifest, document)
    with pytest.raises(SpecValidationError, match="incomplete mode cardinality"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)

    def contradict(subjects):
        subjects[0]["record_sha256s"][0] = "f" * 64

    manifest, identity = _scorecard_inputs(
        tmp_path / "contradictory",
        mode_subject_mutations={SPOT_RELEASE_CONTRACT_ID: contradict},
    )
    with pytest.raises(SpecValidationError, match="subjects or raw leaves"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)


def test_missing_proof_replay_and_cross_bundle_reject(tmp_path) -> None:
    manifest, identity = _scorecard_inputs(tmp_path / "missing")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    del document["proofs"][NATIVE_SCORE_GATE_IDS[0]]
    write_json(manifest, document)
    with pytest.raises(SpecValidationError, match="exactly ten"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)

    manifest, identity = _scorecard_inputs(tmp_path / "replay")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    graph_record = document["subject_graphs"][0]
    graph_path = manifest.parent / graph_record["path"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["provenance"]["attestations"][1] = copy.deepcopy(graph["provenance"]["attestations"][0])
    write_json(graph_path, graph)
    graph_record["sha256"] = sha256_file(graph_path)
    write_json(manifest, document)
    with pytest.raises(SpecValidationError, match="replayed"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)

    manifest, identity = _scorecard_inputs(tmp_path / "bundle")
    expected = json.loads(identity.read_text(encoding="utf-8"))
    expected["bundle_id"] = "f" * 64
    write_json(identity, expected)
    with pytest.raises(SpecValidationError, match="bundle identity differs"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)


def test_stale_identity_zeroes_all_points(tmp_path) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    expected = json.loads(identity.read_text(encoding="utf-8"))
    expected["scope_sha256"] = "f" * 64
    write_json(identity, expected)
    report = evaluate_native_scorecard(manifest, expected_identity_path=identity)
    assert report["points_awarded"] == 0
    assert report["blockers"] == ["stale_identity:scope_sha256"]


def test_release_score_cli_publishes_only_complete_raw_fixture(
    tmp_path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    output = tmp_path / "score.json"

    def evaluate_with_test_policy(*args, **kwargs):
        kwargs["provenance_policy"] = TEST_POLICY
        return _evaluate_native_scorecard(*args, **kwargs)

    monkeypatch.setattr(
        "nfi_backtest_engine.native_scorecard.evaluate_native_scorecard",
        evaluate_with_test_policy,
    )
    exit_code = cli.main(
        [
            "release",
            "score",
            "--evidence",
            str(manifest),
            "--identity",
            str(identity),
            "--output",
            str(output),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0 and output.exists()
    assert report["points_awarded"] == 10


def test_rejected_cli_raw_mutation_does_not_publish(
    tmp_path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mutate, _failure = RAW_FAILURES["native_purity"]
    manifest, identity = _scorecard_inputs(tmp_path, mutations={"native_purity": mutate})
    output = tmp_path / "rejected.json"

    def evaluate_with_test_policy(*args, **kwargs):
        kwargs["provenance_policy"] = TEST_POLICY
        return _evaluate_native_scorecard(*args, **kwargs)

    monkeypatch.setattr(
        "nfi_backtest_engine.native_scorecard.evaluate_native_scorecard",
        evaluate_with_test_policy,
    )
    exit_code = cli.main(
        [
            "release",
            "score",
            "--evidence",
            str(manifest),
            "--identity",
            str(identity),
            "--output",
            str(output),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert report["points_awarded"] == 9
    assert not output.exists()


def test_malformed_or_hash_mismatched_evidence_fails_closed(tmp_path) -> None:
    manifest, identity = _scorecard_inputs(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    proof_path = tmp_path / document["proofs"]["evidence_independence"]["path"]
    proof_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SpecValidationError, match="checksum mismatch"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)
    document["proofs"]["evidence_independence"]["sha256"] = sha256_file(proof_path)
    write_json(manifest, document)
    with pytest.raises(SpecValidationError, match="malformed proof"):
        evaluate_native_scorecard(manifest, expected_identity_path=identity)
