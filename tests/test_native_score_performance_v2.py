from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine import native_score_certification_domains
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.native_score_certification_domains import (
    validate_certification_input,
)
from nfi_backtest_engine.native_score_domain_identity import VerificationClockPolicy
from nfi_backtest_engine.native_scorecard import (
    NATIVE_SCORE_GATES,
    NATIVE_SCORE_PERFORMANCE_EVALUATOR_VERSION,
    NATIVE_SCORE_PERFORMANCE_EVIDENCE_VERSION,
    _full_x7_performance_resources,
    _performance_resources,
    _validate_raw_artifact,
    native_score_record_id,
)
from nfi_backtest_engine.platform_benchmark import REQUIRED_PLATFORM_SYSTEMS
from nfi_backtest_engine.release_contract import (
    FUTURES_RELEASE_CONTRACT_ID,
    SPOT_RELEASE_CONTRACT_ID,
)

_SOURCE_AUTHORITY = {
    "source_closure_sha256": "a" * 64,
    "strategy_class": "NostalgiaForInfinityX7",
    "strategy_sha256": "5" * 64,
    "upstream_commit": "4" * 40,
}


@pytest.fixture(autouse=True)
def _fixed_source_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        native_score_certification_domains,
        "_portfolio_source_identity",
        lambda: _SOURCE_AUTHORITY,
    )


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "certificate_sha256": "1" * 64,
        "wheel_sha256": "2" * 64,
        "native_extension_sha256": "3" * 64,
        "official_reference_repetitions": 1,
        "native_initial_repetitions": 3,
        "native_measured_repetitions": 3,
        "native_maximum_repetitions": 5,
        "native_spread_threshold": 0.05,
        "engine_relative_spread": 0.0,
        "preserved_vector_speedup": 10.0,
        "determinism_met": True,
        "memory_limit_bytes": 2_000,
        "observed_peak_rss_bytes": 1_000,
        "swap_limit_bytes": 1_000,
        "native_observed_peak_swap_bytes": 200,
        "official_observed_peak_swap_bytes": 300,
    }
    payload.update(overrides)
    return payload


def _record(mode: str, **overrides: object) -> dict[str, object]:
    return {
        "record_type": "full_x7_performance_certificate",
        "source_identity_sha256": _SOURCE_AUTHORITY["source_closure_sha256"],
        "mode_contract": mode,
        "payload": _payload(**overrides),
    }


def _certificate(record: dict[str, object]) -> dict[str, object]:
    mode = record["mode_contract"]
    futures = mode == FUTURES_RELEASE_CONTRACT_ID
    payload = record["payload"]
    assert isinstance(payload, dict)
    native_sha256 = payload["native_extension_sha256"]
    surface_sha256 = "f" * 64
    engine_runs = [
        {
            "wall_time_seconds": 10.0,
            "peak_rss_bytes": 900,
            "peak_swap_bytes": payload["native_observed_peak_swap_bytes"],
            "result_sha256": surface_sha256,
        }
        for _ in range(3)
    ]
    cold_run = {
        "wall_time_seconds": 20.0,
        "peak_rss_bytes": payload["observed_peak_rss_bytes"],
        "peak_swap_bytes": 100,
        "result_sha256": surface_sha256,
    }
    official_run = {
        "wall_time_seconds": 100.0,
        "peak_rss_bytes": 800,
        "peak_swap_bytes": payload["official_observed_peak_swap_bytes"],
        "result_sha256": surface_sha256,
    }

    def summary(runs: list[dict[str, object]]) -> dict[str, object]:
        walls = [float(run["wall_time_seconds"]) for run in runs]
        rss = [int(run["peak_rss_bytes"]) for run in runs]
        swap = [int(run["peak_swap_bytes"]) for run in runs]
        return {
            "wall_time_seconds": {
                "minimum": min(walls),
                "median": sorted(walls)[len(walls) // 2],
                "maximum": max(walls),
            },
            "peak_rss_bytes": {"minimum": min(rss), "maximum": max(rss)},
            "peak_swap_bytes": {"maximum": max(swap), "measurements_complete": True},
        }

    gates = {
        "input_lock": {"met": True, "identity_sha256": "e" * 64},
        "installed_wheel": {
            "met": True,
            "sha256": payload["wheel_sha256"],
            "native_member_sha256": native_sha256,
            "installed_extension_sha256": native_sha256,
            "installed_extension_equal": True,
        },
        "native_pipeline": {"met": True},
        "official_parity": {"met": True},
        "determinism": {"met": payload["determinism_met"]},
        "speed": {
            "met": True,
            "observed_speedup": payload["preserved_vector_speedup"],
        },
        "memory": {
            "met": True,
            "limit_bytes": payload["memory_limit_bytes"],
            "observed_peak_bytes": payload["observed_peak_rss_bytes"],
        },
        "swap": {
            "met": True,
            "limit_bytes": payload["swap_limit_bytes"],
            "native_observed_peak_bytes": payload["native_observed_peak_swap_bytes"],
            "official_observed_peak_bytes": payload["official_observed_peak_swap_bytes"],
        },
        "state_probes": {"met": True},
    }
    return {
        "schema_version": "2.0.0",
        "created_at": "2026-09-06T00:00:00Z",
        "status": "certified",
        "release_certified": True,
        "claim_scope": {
            "strategy": "NostalgiaForInfinityX7",
            "upstream_commit": "4" * 40,
            "mode_contract": mode,
            "trading_mode": "futures" if futures else "spot",
            "margin_mode": "isolated" if futures else None,
            "exchange": "binance",
            "settlement_currency": "USDT",
            "required_data_roles": (
                ["candles", "funding_rate", "mark"] if futures else ["candles"]
            ),
            "timerange": "20210101-20260101",
            "pair_count": 80,
            "timeframes": ["5m", "15m", "1h", "4h", "1d"],
            "continuous_timerange": True,
            "history_coverage_policy": "listing-aware" if futures else "strict",
            "evidence": "continuous-oracle-plus-official-full-state-probes",
        },
        "inputs": {
            "release_lock": {"identity_sha256": "e" * 64},
            "mode_contract": mode,
            "reference": {},
            "strategy_sha256": "5" * 64,
            "config_sha256": "6" * 64,
            "data_aggregate_sha256": "7" * 64,
            "engine_market_snapshot_sha256": "8" * 64,
            "reference_market_snapshot_sha256": "8" * 64,
        },
        "environment": {
            "hardware": {},
            "execution_profile": {
                "working_memory_bytes": payload["memory_limit_bytes"],
                "swap_cap_bytes": payload["swap_limit_bytes"],
            },
            "package_version": "1.15.0",
            "engine_build": {"binary_sha256": native_sha256},
        },
        "measurement": {
            "native_warmups_excluded": 1,
            "native_lane": "preserved-vector-reuse",
            "native_initial_repetitions": payload["native_initial_repetitions"],
            "native_measured_repetitions": payload["native_measured_repetitions"],
            "native_maximum_repetitions": payload["native_maximum_repetitions"],
            "native_spread_threshold": payload["native_spread_threshold"],
            "engine_relative_spread": payload["engine_relative_spread"],
            "cold_seed_repetitions": 1,
            "official_reference_repetitions": payload["official_reference_repetitions"],
            "official_reference_role": "single-continuous-exact-parity-oracle",
            "resumed": False,
        },
        "runs": {
            "engine": engine_runs,
            "cold_seed": cold_run,
            "official_reference": official_run,
            "engine_summary": summary(engine_runs),
            "cold_seed_summary": summary([cold_run]),
            "official_reference_summary": summary([official_run]),
        },
        "state_probes": [{}, {}, {}],
        "gates": gates,
    }


def test_full_x7_performance_v2_matches_product_contract() -> None:
    records = [_record(SPOT_RELEASE_CONTRACT_ID), _record(FUTURES_RELEASE_CONTRACT_ID)]
    assert _full_x7_performance_resources(records) == []

    records[0]["payload"] = _payload(
        engine_relative_spread=0.051,
        native_measured_repetitions=3,
    )
    assert _full_x7_performance_resources(records) == [
        "adaptive_native_repetition_policy_not_met"
    ]
    records[0]["payload"] = _payload(
        engine_relative_spread=0.051,
        native_measured_repetitions=5,
    )
    assert _full_x7_performance_resources(records) == []


@pytest.mark.parametrize(
    ("override", "failure"),
    [
        ({"official_reference_repetitions": 2}, "official_oracle_repetition_policy_not_met"),
        ({"preserved_vector_speedup": 9.99}, "reuse_speed_target_not_met"),
        ({"determinism_met": False}, "nondeterministic_outputs"),
        ({"observed_peak_rss_bytes": 2_001}, "memory_limit_not_met"),
        ({"native_observed_peak_swap_bytes": 1_001}, "swap_limit_not_met"),
        ({"official_observed_peak_swap_bytes": 1_001}, "swap_limit_not_met"),
    ],
)
def test_full_x7_performance_v2_recomputes_each_policy(
    override: dict[str, object], failure: str
) -> None:
    assert _full_x7_performance_resources([_record(SPOT_RELEASE_CONTRACT_ID, **override)]) == [
        failure
    ]


def test_full_x7_performance_certificate_is_opened_and_recomputed() -> None:
    record = _record(SPOT_RELEASE_CONTRACT_ID)
    record.update(
        {
            "source_artifact": {"path": "machine.json", "sha256": "9" * 64},
            "source_identity_sha256": "a" * 64,
            "candidate_identity_sha256": "b" * 64,
            "platform": "linux",
            "workload_sha256": "c" * 64,
            "run_id": "run-1",
            "nonce": "d" * 64,
        }
    )
    certificate = _certificate(record)
    validate_certification_input(
        certificate,
        record=record,
        field="certificate_sha256",
        verification_clock=VerificationClockPolicy(now=datetime.now(UTC)),
    )

    mismatched = copy.deepcopy(record)
    assert isinstance(mismatched["payload"], dict)
    mismatched["payload"]["preserved_vector_speedup"] = 11.0
    with pytest.raises(SpecValidationError, match="observation differs"):
        validate_certification_input(
            certificate,
            record=mismatched,
            field="certificate_sha256",
            verification_clock=VerificationClockPolicy(now=datetime.now(UTC)),
        )

    package_drift = copy.deepcopy(certificate)
    package_drift["environment"]["engine_build"]["binary_sha256"] = "e" * 64
    with pytest.raises(SpecValidationError, match="package identity differs"):
        validate_certification_input(
            package_drift,
            record=record,
            field="certificate_sha256",
            verification_clock=VerificationClockPolicy(now=datetime.now(UTC)),
        )

    summary_drift = copy.deepcopy(certificate)
    summary_drift["runs"]["engine"][0]["wall_time_seconds"] = 11.0
    with pytest.raises(SpecValidationError, match="wall summary differs"):
        validate_certification_input(
            summary_drift,
            record=record,
            field="certificate_sha256",
            verification_clock=VerificationClockPolicy(now=datetime.now(UTC)),
        )

    cap_drift = copy.deepcopy(certificate)
    cap_drift["environment"]["execution_profile"]["working_memory_bytes"] = 3_000
    with pytest.raises(SpecValidationError, match="memory gate differs"):
        validate_certification_input(
            cap_drift,
            record=record,
            field="certificate_sha256",
            verification_clock=VerificationClockPolicy(now=datetime.now(UTC)),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda certificate, _record: certificate["claim_scope"].__setitem__(
            "upstream_commit", "0" * 40
        ),
        lambda certificate, _record: certificate["inputs"].__setitem__(
            "strategy_sha256", "0" * 64
        ),
        lambda certificate, _record: certificate["inputs"].__setitem__(
            "mode_contract", FUTURES_RELEASE_CONTRACT_ID
        ),
        lambda _certificate, record: record.__setitem__("source_identity_sha256", "0" * 64),
    ],
)
def test_performance_certificate_binds_packaged_source_and_mode(mutation) -> None:
    record = _record(SPOT_RELEASE_CONTRACT_ID)
    record["source_identity_sha256"] = _SOURCE_AUTHORITY["source_closure_sha256"]
    certificate = _certificate(record)
    mutation(certificate, record)
    with pytest.raises(SpecValidationError, match="source identity differs"):
        validate_certification_input(
            certificate,
            record=record,
            field="certificate_sha256",
            verification_clock=VerificationClockPolicy(now=datetime.now(UTC)),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda certificate: certificate["gates"]["input_lock"].__setitem__(
            "identity_sha256", "0" * 64
        ),
        lambda certificate: certificate["inputs"]["release_lock"].__setitem__(
            "identity_sha256", None
        ),
    ],
)
def test_performance_certificate_requires_its_own_valid_sealed_lock(mutation) -> None:
    record = _record(SPOT_RELEASE_CONTRACT_ID)
    record["source_identity_sha256"] = _SOURCE_AUTHORITY["source_closure_sha256"]
    certificate = _certificate(record)
    mutation(certificate)
    with pytest.raises(SpecValidationError, match="sealed input identity differs"):
        validate_certification_input(
            certificate,
            record=record,
            field="certificate_sha256",
            verification_clock=VerificationClockPolicy(now=datetime.now(UTC)),
        )


def test_performance_certificate_legacy_score_identity_is_strict_when_present() -> None:
    record = _record(SPOT_RELEASE_CONTRACT_ID)
    record.update(
        {
            "source_identity_sha256": _SOURCE_AUTHORITY["source_closure_sha256"],
            "candidate_identity_sha256": "b" * 64,
            "platform": "linux",
            "workload_sha256": "c" * 64,
            "run_id": "run-1",
            "nonce": "d" * 64,
        }
    )
    certificate = _certificate(record)
    certificate["inputs"]["native_score_identity"] = None
    with pytest.raises(SpecValidationError, match="score identity differs"):
        validate_certification_input(
            certificate,
            record=record,
            field="certificate_sha256",
            verification_clock=VerificationClockPolicy(now=datetime.now(UTC)),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda certificate: certificate["measurement"].pop("native_lane"),
        lambda certificate: certificate["measurement"].pop("cold_seed_repetitions"),
        lambda certificate: certificate["measurement"].__setitem__(
            "cold_seed_repetitions", True
        ),
    ],
)
def test_performance_certificate_requires_writer_lane_and_cold_seed(mutation) -> None:
    record = _record(SPOT_RELEASE_CONTRACT_ID)
    certificate = _certificate(record)
    mutation(certificate)
    with pytest.raises(SpecValidationError, match="repetition|cold-seed repetitions"):
        validate_certification_input(
            certificate,
            record=record,
            field="certificate_sha256",
            verification_clock=VerificationClockPolicy(now=datetime.now(UTC)),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("limit_bytes", True), ("native_observed_peak_bytes", False)],
)
def test_performance_certificate_rejects_boolean_swap_numbers(field: str, value: bool) -> None:
    record = _record(
        SPOT_RELEASE_CONTRACT_ID,
        swap_limit_bytes=1,
        native_observed_peak_swap_bytes=0,
        official_observed_peak_swap_bytes=0,
    )
    certificate = _certificate(record)
    certificate["gates"]["swap"][field] = value
    with pytest.raises(SpecValidationError, match="swap gate .*malformed"):
        validate_certification_input(
            certificate,
            record=record,
            field="certificate_sha256",
            verification_clock=VerificationClockPolicy(now=datetime.now(UTC)),
        )


def _materialize_v2_record(tmp_path: Path, mode: str, index: int) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": NATIVE_SCORE_PERFORMANCE_EVIDENCE_VERSION,
        "gate_id": "deterministic_performance_resource_proof",
        "record_id": "pending",
        "record_type": "full_x7_performance_certificate",
        "source_identity_sha256": "a" * 64,
        "candidate_identity_sha256": "b" * 64,
        "mode_contract": mode,
        "platform": "linux",
        "workload_sha256": f"{index + 1:x}" * 64,
        "run_id": "run-1",
        "nonce": f"{index + 3:x}" * 64,
        "source_artifact": {"path": "pending", "sha256": "e" * 64},
        "payload": _payload(),
    }
    certificate_path = tmp_path / f"certificate-{index}.json"
    write_json(certificate_path, _certificate(record))
    certificate_sha256 = sha256_file(certificate_path)
    record["payload"]["certificate_sha256"] = certificate_sha256
    machine_path = tmp_path / f"machine-{index}.json"
    write_json(
        machine_path,
        {
            "schema_version": NATIVE_SCORE_PERFORMANCE_EVIDENCE_VERSION,
            "gate_id": record["gate_id"],
            "record_type": record["record_type"],
            "semantic_identity": f"performance-certificate:{mode}",
            "producer": {
                "role": "full-x7-performance-certificate-verifier",
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
            "inputs": [
                {
                    "field": "certificate_sha256",
                    "path": certificate_path.relative_to(tmp_path).as_posix(),
                    "sha256": certificate_sha256,
                }
            ],
            "observation": record["payload"],
        },
    )
    record["source_artifact"] = {
        "path": machine_path.relative_to(tmp_path).as_posix(),
        "sha256": sha256_file(machine_path),
    }
    record["record_id"] = native_score_record_id(record)
    return record


def test_v2_raw_artifact_opens_one_real_certificate_per_mode(tmp_path: Path) -> None:
    identity = {
        "source_closure_sha256": "a" * 64,
        "engine_artifact_sha256": "b" * 64,
        "oracle_sha256": "c" * 64,
        "scope_sha256": "d" * 64,
    }
    records = [
        _materialize_v2_record(tmp_path, mode, index)
        for index, mode in enumerate(
            sorted((SPOT_RELEASE_CONTRACT_ID, FUTURES_RELEASE_CONTRACT_ID))
        )
    ]
    records.sort(key=lambda item: item["record_id"])
    document = {
        "schema_version": NATIVE_SCORE_PERFORMANCE_EVIDENCE_VERSION,
        "evaluator_version": NATIVE_SCORE_PERFORMANCE_EVALUATOR_VERSION,
        "gate_id": "deterministic_performance_resource_proof",
        "identity": identity,
        "records": records,
    }
    gate = next(
        item
        for item in NATIVE_SCORE_GATES
        if item.gate_id == "deterministic_performance_resource_proof"
    )
    _validate_raw_artifact(
        document,
        gate,
        identity,
        root=tmp_path,
        verification_clock=VerificationClockPolicy(now=datetime.now(UTC)),
    )
    assert gate.evaluate_records(records, identity) == []


def test_legacy_v1_process_samples_keep_zero_swap_policy() -> None:
    records = []
    for mode in (SPOT_RELEASE_CONTRACT_ID, FUTURES_RELEASE_CONTRACT_ID):
        for platform in REQUIRED_PLATFORM_SYSTEMS:
            for runtime, wall_seconds in (("official", 120.0), ("native", 10.0)):
                for population in ("cold", "reuse"):
                    for sample_index in range(3):
                        records.append(
                            {
                                "record_type": "performance_process_sample",
                                "mode_contract": mode,
                                "platform": platform,
                                "payload": {
                                    "runtime": runtime,
                                    "population": population,
                                    "sample_index": sample_index,
                                    "wall_seconds": wall_seconds,
                                    "peak_rss_bytes": 1_000,
                                    "memory_limit_bytes": 2_000,
                                    "oom": False,
                                    "swap_bytes": 1 if sample_index == 0 else 0,
                                    "output_sha256": "f" * 64,
                                },
                            }
                        )
    assert _performance_resources(records, {}) == ["swap_used"]
