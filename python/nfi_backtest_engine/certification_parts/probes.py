"""Full X7 branch-probe validation and execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..branch_coverage import validate_fixture_coverage
from ..canonical import read_json
from ..errors import BenchmarkError, SpecValidationError
from ..evidence_bundle import artifact_record
from ..fixture import sha256_file, validate_fixture
from ..full_x7_resume import require_stage_available
from ..performance_gate import run_performance_gate
from ..release_contract import (
    ReleaseModeContract,
    release_contract_for_config,
)


def _validate_probe_matrix(
    manifests: list[str | Path],
    *,
    contract: ReleaseModeContract,
    expected_upstream_commit: str | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    probes: list[tuple[Path, dict[str, Any]]] = []
    kinds: set[str] = set()
    coverage_by_kind: dict[str, list[dict[str, Any]]] = {}
    all_coverage: list[dict[str, Any]] = []
    for value in manifests:
        path = Path(value).resolve()
        manifest = validate_fixture(path)
        if manifest["schema_version"] != "3.0.0":
            raise SpecValidationError("Full X7 probes must use fixture manifest v3")
        config_inputs = [
            item
            for item in manifest["inputs"]
            if isinstance(item, dict) and item.get("role") == "config"
        ]
        if len(config_inputs) != 1:
            raise SpecValidationError("Full X7 probe must seal exactly one config")
        probe_config = read_json(path.parent / config_inputs[0]["path"])
        if not isinstance(probe_config, dict):
            raise SpecValidationError("Full X7 probe config must be an object")
        probe_contract = release_contract_for_config(probe_config)
        if probe_contract.contract_id != contract.contract_id:
            raise SpecValidationError("Full X7 probe mode differs from the release input lock")
        provenance = manifest.get("strategy_provenance")
        if expected_upstream_commit is not None and (
            not isinstance(provenance, dict)
            or provenance.get("upstream_commit") != expected_upstream_commit
        ):
            raise SpecValidationError(
                "Full X7 probe upstream commit differs from the release input lock"
            )
        coverage = validate_fixture_coverage(path, manifest)
        observed = coverage["observed"]
        kind = manifest["probe_kind"]
        kinds.add(kind)
        coverage_by_kind.setdefault(kind, []).append(observed)
        all_coverage.append(observed)
        probes.append((path, manifest))
    missing = sorted(contract.required_probe_kinds - kinds)
    if missing:
        raise SpecValidationError("Full X7 probe matrix is incomplete: " + ", ".join(missing))
    for requirement in contract.probe_evidence:
        observed = _merge_probe_coverage(coverage_by_kind.get(requirement.probe_kind, []))
        missing_evidence = requirement.missing_from(observed)
        if missing_evidence:
            raise SpecValidationError(
                f"Full X7 {requirement.probe_kind} probe evidence is incomplete: "
                + ", ".join(missing_evidence)
            )
    aggregate = _merge_probe_coverage(all_coverage)
    missing_protections = sorted(
        contract.required_protection_methods - set(aggregate["protection_methods"])
    )
    if missing_protections:
        raise SpecValidationError(
            "Full X7 protection probe matrix is incomplete: " + ", ".join(missing_protections)
        )
    if contract.require_rejected_locked_entry and not aggregate["rejected_locked_entry"]:
        raise SpecValidationError("Full X7 protection probe matrix did not reject a locked entry")
    return probes


def _merge_probe_coverage(
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Combine independent official probes without weakening any observation."""

    list_fields = (
        "callbacks",
        "entry_tags",
        "compound_tags",
        "protection_methods",
        "exit_reasons",
        "sides",
        "leverages",
    )
    return {
        **{
            field: sorted({value for observed in observations for value in observed.get(field, [])})
            for field in list_fields
        },
        "lock_count": sum(int(observed.get("lock_count", 0)) for observed in observations),
        "funded_trades": sum(int(observed.get("funded_trades", 0)) for observed in observations),
        "rejected_locked_entry": any(
            observed.get("rejected_locked_entry") is True for observed in observations
        ),
    }


def _run_probes(
    probes: list[tuple[Path, dict[str, Any]]],
    output: Path,
    *,
    execution_profile_path: str | Path,
    timeout_seconds: int,
    resume: bool,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for index, (manifest_path, manifest) in enumerate(probes, start=1):
        probe_output = output / f"{index:02d}-{manifest['probe_kind']}"
        performance_path = probe_output / "performance.json"
        if resume and performance_path.is_file():
            performance = read_json(performance_path)
            if performance.get("fixture_id") != manifest["fixture_id"]:
                raise BenchmarkError(
                    f"resumed state probe differs from its manifest: {probe_output}"
                )
        else:
            require_stage_available(
                probe_output,
                stage=f"state probe {manifest['fixture_id']}",
            )
            performance = run_performance_gate(
                manifest_path,
                probe_output,
                profile_path=execution_profile_path,
                verification_level="full",
                repetitions=1,
                timeout_seconds=timeout_seconds,
            )
        reports.append(
            {
                "fixture_id": manifest["fixture_id"],
                "probe_kind": manifest["probe_kind"],
                "manifest_sha256": sha256_file(manifest_path),
                "complete": performance["complete"],
                "trade_surface_equal": performance["gates"]["parity"]["met"],
                "full_state_equal": _performance_full_state_equal(performance),
                "coverage_met": True,
                "performance_report": artifact_record(
                    probe_output / "performance.json",
                    relative_to=output.parent,
                ),
            }
        )
    return reports


def _performance_full_state_equal(performance: dict[str, Any]) -> bool:
    for lane in ("engine", "reference"):
        runs = performance.get(lane, {}).get("runs", [])
        if not runs:
            return False
        for run in runs:
            report = run.get("report")
            state = (
                report.get("parity", {}).get("state_trace") if isinstance(report, dict) else None
            )
            if not isinstance(state, dict) or state.get("equal") is not True:
                return False
    return True
