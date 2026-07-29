"""Promotion gate for independent legacy and generic state-machine executions."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import SpecValidationError
from .fixture import sha256_file
from .parity import ParityDifference, first_difference
from .specs import validate_trade_surface
from .state_trace import TraceDifference, first_trace_difference

STATE_MACHINE_SHADOW_GATE_VERSION = "1.0.0"


def evaluate_state_machine_shadow_gate(
    legacy_run_directory: str | Path,
    candidate_run_directory: str | Path,
    *,
    legacy_trace: str | Path,
    candidate_trace: str | Path,
    branch_proof: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Require exact trade and full-state parity from two separate runs."""

    legacy_root = Path(legacy_run_directory).resolve()
    candidate_root = Path(candidate_run_directory).resolve()
    if legacy_root == candidate_root:
        raise SpecValidationError("shadow gate requires two separate run directories")
    legacy = _complete_run(legacy_root, "legacy")
    candidate = _complete_run(candidate_root, "candidate")
    if _adapter_lane(candidate) != "generic-state-machine":
        raise SpecValidationError(
            "candidate run did not execute the generic-state-machine adapter lane"
        )
    if _adapter_lane(legacy) == "generic-state-machine":
        raise SpecValidationError("legacy run must use an independent non-state-machine lane")
    if _run_binding(legacy) != _run_binding(candidate):
        raise SpecValidationError("shadow runs do not bind the same sealed workload")

    legacy_surface_path, legacy_surface = _surface(legacy_root, legacy)
    candidate_surface_path, candidate_surface = _surface(candidate_root, candidate)
    if legacy_surface_path == candidate_surface_path:
        raise SpecValidationError("shadow runs cannot share a trade-surface artifact")
    trade_difference = first_difference(legacy_surface, candidate_surface)

    legacy_trace_path = Path(legacy_trace).resolve()
    candidate_trace_path = Path(candidate_trace).resolve()
    if legacy_trace_path == candidate_trace_path:
        raise SpecValidationError("shadow runs cannot share a full-state trace")
    state_difference = first_trace_difference(
        legacy_trace_path,
        candidate_trace_path,
        compare_input_identity=False,
    )
    proof_path = Path(branch_proof).resolve()
    proof = read_json(proof_path)
    branch_reached = _validated_branch_proof(proof)
    trade_exact = trade_difference is None
    state_exact = state_difference is None
    report = {
        "schema_version": STATE_MACHINE_SHADOW_GATE_VERSION,
        "complete": True,
        "separate_executions": True,
        "changed_branch_reached": branch_reached,
        "trade_surface_exact": trade_exact,
        "full_state_exact": state_exact,
        "promoted": branch_reached and trade_exact and state_exact,
        "workload": _run_binding(legacy),
        "runs": {
            "legacy": _run_record(legacy_root, legacy),
            "candidate": _run_record(candidate_root, candidate),
        },
        "artifacts": {
            "legacy_trade_surface": _file_record(legacy_surface_path),
            "candidate_trade_surface": _file_record(candidate_surface_path),
            "legacy_state_trace": _file_record(legacy_trace_path),
            "candidate_state_trace": _file_record(candidate_trace_path),
            "branch_proof": _file_record(proof_path),
        },
        "differences": {
            "trade_surface": _parity_difference(trade_difference),
            "full_state": _trace_difference(state_difference),
        },
    }
    if output_path is not None:
        write_json(output_path, report)
    return report


def _complete_run(root: Path, label: str) -> dict[str, Any]:
    path = root / "run.json"
    value = read_json(path)
    if (
        not isinstance(value, dict)
        or value.get("complete") is not True
        or value.get("status") != "complete"
        or not isinstance(value.get("result"), dict)
        or not isinstance(value.get("inputs"), dict)
    ):
        raise SpecValidationError(f"{label} shadow run is not a complete Native run")
    return value


def _adapter_lane(run: Mapping[str, Any]) -> str | None:
    capability = run.get("capability")
    return (
        capability.get("adapter_lane")
        if isinstance(capability, Mapping)
        and isinstance(capability.get("adapter_lane"), str)
        else None
    )


def _run_binding(run: Mapping[str, Any]) -> dict[str, Any]:
    inputs = _mapping(run, "inputs")
    strategy = _mapping(inputs, "strategy")
    config = _mapping(inputs, "config")
    data = _mapping(run, "data")
    binding = {
        "strategy_sha256": strategy.get("file_sha256"),
        "config_sha256": config.get("run_effective_sha256"),
        "pairlist_sha256": inputs.get("pairlist_sha256"),
        "data_sha256": data.get("aggregate_sha256"),
        "timerange": inputs.get("timerange"),
    }
    if not all(isinstance(value, str) and value for value in binding.values()):
        raise SpecValidationError("shadow run has an incomplete workload binding")
    return binding


def _surface(root: Path, run: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    result = _mapping(run, "result")
    record = _mapping(result, "trade_surface")
    path_value = record.get("path")
    if not isinstance(path_value, str):
        raise SpecValidationError("shadow run trade-surface path is invalid")
    path = Path(path_value)
    path = path.resolve() if path.is_absolute() else (root / path).resolve()
    if path != root / "trade-surface.json":
        raise SpecValidationError("shadow run trade surface is outside its canonical path")
    _validate_file_record(path, record)
    surface = read_json(path)
    validate_trade_surface(surface)
    return path, surface


def _validated_branch_proof(value: Any) -> bool:
    if not isinstance(value, Mapping):
        raise SpecValidationError("branch proof must be an object")
    parity = value.get("parity")
    coverage = value.get("branch_coverage")
    valid = (
        value.get("schema_version") == "1.1.0"
        and value.get("verification_level") == "full"
        and value.get("complete") is True
        and isinstance(parity, Mapping)
        and isinstance(parity.get("trade_surface"), Mapping)
        and parity["trade_surface"].get("equal") is True
        and isinstance(parity.get("state_trace"), Mapping)
        and parity["state_trace"].get("equal") is True
        and isinstance(coverage, Mapping)
        and coverage.get("met") is True
    )
    if not valid:
        raise SpecValidationError(
            "branch proof must be a complete full fixture with exact official "
            "trade/full-state parity and required coverage"
        )
    return True


def _run_record(root: Path, run: Mapping[str, Any]) -> dict[str, Any]:
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise SpecValidationError("shadow run_id is invalid")
    return {
        "run_id": run_id,
        "adapter_lane": _adapter_lane(run),
        "report": _file_record(root / "run.json"),
    }


def _file_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _validate_file_record(path: Path, record: Mapping[str, Any]) -> None:
    if (
        not path.is_file()
        or record.get("bytes") != path.stat().st_size
        or record.get("sha256") != sha256_file(path)
    ):
        raise SpecValidationError(f"shadow artifact record differs from disk: {path}")


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    candidate = value.get(key)
    if not isinstance(candidate, Mapping):
        raise SpecValidationError(f"shadow run field {key!r} must be an object")
    return candidate


def _parity_difference(
    difference: ParityDifference | None,
) -> dict[str, Any] | None:
    if difference is None:
        return None
    return {
        "path": difference.path,
        "expected": difference.expected,
        "actual": difference.actual,
        "reason": difference.reason,
    }


def _trace_difference(
    difference: TraceDifference | None,
) -> dict[str, Any] | None:
    if difference is None:
        return None
    return {
        "sequence": difference.sequence,
        "path": difference.path,
        "expected": difference.expected,
        "actual": difference.actual,
        "reason": difference.reason,
        "event_key": difference.event_key,
    }
