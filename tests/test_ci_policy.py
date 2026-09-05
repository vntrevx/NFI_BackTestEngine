from __future__ import annotations

import copy
import hashlib
import importlib.util
import math
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]


def _load_script(name: str) -> ModuleType:
    path = ROOT / ".github" / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"ci_policy_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"CI script is not loadable: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _complete_reports(
    timing: ModuleType,
    contract: dict[str, Any],
    tmp_path: Path,
    *,
    run_id: str = "9001",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    lock_sha = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    reports: list[dict[str, Any]] = []
    pytest_reports: list[dict[str, Any]] = []
    pytest_timing = _load_script("ci_pytest.py")
    for index, report_policy in enumerate(contract["timing"]["reports"]):
        output = tmp_path / f"timing-{index}.json"
        report = timing.initialize_report(
            output=output,
            contract=contract,
            job=report_policy["job"],
            os_name=report_policy["os"],
            python_version=report_policy["python"],
            suite=report_policy["suite"],
            workflow=contract["timing"]["workflow_name"],
            repository=contract["timing"]["repository"],
            run_id=run_id,
            run_attempt="1",
            commit_sha="a" * 40,
            cache_file=ROOT / "uv.lock",
            source_dirty=False,
        )
        for step in report["steps"]:
            step.update(
                status="completed",
                duration_seconds=0.25,
                timeout_seconds=min(step["timeout_seconds"], 30),
                exit_code=0,
            )
        reports.append(report)
        if report_policy["job"] == "python" and report_policy["suite"] == "full":
            records = [
                pytest_timing.build_test_record(
                    nodeid="tests/test_ci_policy.py::test_actionable",
                    outcome="passed",
                    wall_seconds=0.25,
                    cpu_seconds=0.125,
                    peak_rss_bytes=4096,
                ),
                pytest_timing.build_test_record(
                    nodeid="tests/parity/test_ci_policy.py::test_parity",
                    outcome="passed",
                    wall_seconds=0.5,
                    cpu_seconds=0.25,
                    peak_rss_bytes=8192,
                ),
            ]
            pytest_reports.append(
                pytest_timing.build_pytest_report(
                    records,
                    slowest_count=contract["timing"]["pytest_slowest_count"],
                    identity={
                        field: report["identity"][field]
                        for field in (
                            "report_id",
                            "workflow",
                            "workflow_ref",
                            "repository",
                            "run_id",
                            "run_attempt",
                            "commit_sha",
                            "os",
                            "python",
                            "suite",
                            "cache",
                            "build",
                        )
                    },
                )
            )
    return reports, pytest_reports, lock_sha


def _trusted_compare_inputs(
    timing: ModuleType,
    contract: dict[str, Any],
    cache_sha: str,
) -> dict[str, str]:
    policy = contract["timing"]
    candidate = timing.trusted_candidate_identity(
        contract,
        repository=policy["repository"],
        workflow=policy["workflow_name"],
        workflow_ref=policy["workflow_ref"],
        baseline_id=policy["baseline_id"],
        candidate_commit="a" * 40,
        cache_lock_sha256=cache_sha,
    )
    return {
        "expected_repository": policy["repository"],
        "expected_workflow": policy["workflow_name"],
        "expected_workflow_ref": policy["workflow_ref"],
        "expected_run_attempt": "1",
        "expected_commit_sha": "a" * 40,
        "expected_cache_sha256": cache_sha,
        "expected_candidate_commit": "a" * 40,
        "expected_candidate_artifact_root": candidate["artifact_identity_root"],
        "expected_build_identity_root": candidate["build_identity_root"],
        "expected_baseline_id": policy["baseline_id"],
    }


def test_machine_timing_policy_covers_retention_jobs_os_and_steps() -> None:
    contract_module = _load_script("ci_contract.py")
    contract = contract_module.load_contract(ROOT / ".github/ci-contract.json")
    policy = contract["timing"]
    workflow = (ROOT / contract["workflow"]).read_text(encoding="utf-8")

    assert re.fullmatch(r"\d+\.\d+\.\d+", policy["schema_version"])
    assert policy["artifact_retention_days"] > 0
    retention_values = {
        int(value) for value in re.findall(r"retention-days: (\d+)", workflow)
    }
    assert policy["artifact_retention_days"] in retention_values
    reports = policy["reports"]
    full_python_os = {
        report["os"]
        for report in reports
        if report["job"] == "python" and report["suite"] == "full"
    }
    assert full_python_os == {"ubuntu-latest", "macos-14"}
    assert "windows-latest" not in workflow
    assert "windows_process_lifecycle" not in policy
    assert "windows_cleanup_event" not in policy
    ast_python_lanes = {
        (report["os"], report["python"])
        for report in reports
        if report["job"] == "python" and report["suite"] == "ast-compat"
    }
    assert ast_python_lanes == {
        ("ubuntu-latest", "3.13"),
        ("ubuntu-latest", "3.14"),
    }
    assert {report["job"] for report in reports} == {
        "python",
        "python-quality",
        "rust-quality",
        "parity",
    }
    observed_steps = {
        step for report in reports for step in report["required_steps"]
    }
    assert observed_steps == set(policy["step_categories"])
    assert all(report["required_steps"] for report in reports)
    assert policy["rust_compiler_cache"] == "sccache-gha-v0.10.0"


def test_actionable_pytest_resources_and_slowest_are_machine_validated(
    tmp_path: Path,
) -> None:
    timing = _load_script("ci_timing.py")
    contract = _load_script("ci_contract.py").load_contract(
        ROOT / ".github/ci-contract.json"
    )
    reports, pytest_reports, lock_sha = _complete_reports(timing, contract, tmp_path)

    aggregate = timing.validate_timing_reports(
        reports,
        contract=contract,
        expected_run_id="9001",
        expected_run_attempt="1",
        expected_commit_sha="a" * 40,
        expected_lock_sha256=lock_sha,
    )
    aggregate["pytest"] = timing.validate_pytest_reports(
        pytest_reports,
        timing_reports=reports,
        contract=contract,
    )

    assert aggregate["pytest"]["report_count"] == 2
    assert aggregate["pytest"]["test_count"] == 4
    for record in aggregate["pytest"]["tests"]:
        assert re.fullmatch(r"[0-9a-f]{64}", record["test_id"])
        assert record["owner"] in contract["timing"]["pytest_ownership_groups"]
        assert math.isfinite(record["duration_seconds"])
        assert math.isfinite(record["resources"]["cpu_seconds"])
        assert record["resources"]["peak_rss_bytes"] > 0
    assert aggregate["pytest"]["slowest_tests"]
    assert aggregate["pytest"]["slowest_tests"][0]["duration_seconds"] == 0.5


def test_retry_validation_accepts_prior_attempt_artifacts_from_same_run(
    tmp_path: Path,
) -> None:
    timing = _load_script("ci_timing.py")
    contract = _load_script("ci_contract.py").load_contract(
        ROOT / ".github/ci-contract.json"
    )
    reports, _, lock_sha = _complete_reports(timing, contract, tmp_path)
    reports[0]["identity"]["run_attempt"] = "2"

    aggregate = timing.validate_timing_reports(
        reports,
        contract=contract,
        expected_run_id="9001",
        expected_run_attempt="3",
        expected_commit_sha="a" * 40,
        expected_lock_sha256=lock_sha,
    )

    assert aggregate["identity"]["run_attempt"] == "3"


def test_compare_uses_trusted_inputs_not_mutual_report_identity(tmp_path: Path) -> None:
    timing = _load_script("ci_timing.py")
    contract = _load_script("ci_contract.py").load_contract(
        ROOT / ".github/ci-contract.json"
    )
    aggregates = []
    cache_sha = ""
    for run_id in ("9001", "9002", "9003"):
        reports, _, cache_sha = _complete_reports(
            timing, contract, tmp_path / run_id, run_id=run_id
        )
        aggregates.append(
            timing.validate_timing_reports(
                reports,
                contract=contract,
                expected_run_id=run_id,
                expected_run_attempt="1",
                expected_commit_sha="a" * 40,
                expected_lock_sha256=cache_sha,
            )
        )
    trusted = _trusted_compare_inputs(timing, contract, cache_sha)

    comparison = timing.compare_three_runs(
        aggregates,
        contract=contract,
        **trusted,
    )
    assert comparison["run_ids"] == ["9001", "9002", "9003"]
    forged = copy.deepcopy(aggregates)
    for aggregate in forged:
        aggregate["identity"]["repository"] = "attacker/repository"
    with pytest.raises(ValueError, match="trusted comparison identity mismatch"):
        timing.compare_three_runs(forged, contract=contract, **trusted)


def test_missing_os_or_required_job_is_rejected(tmp_path: Path) -> None:
    timing = _load_script("ci_timing.py")
    contract = _load_script("ci_contract.py").load_contract(
        ROOT / ".github/ci-contract.json"
    )
    reports, _, lock_sha = _complete_reports(timing, contract, tmp_path)
    for missing_job, missing_os in (
        ("python", "ubuntu-latest"),
        ("python", "macos-14"),
        ("python-quality", None),
        ("rust-quality", None),
    ):
        incomplete = [
            report
            for report in reports
            if not (
                report["identity"]["job"] == missing_job
                and (missing_os is None or report["identity"]["os"] == missing_os)
            )
        ]
        with pytest.raises(ValueError, match="missing timing reports"):
            timing.validate_timing_reports(
                incomplete,
                contract=contract,
                expected_run_id="9001",
                expected_run_attempt="1",
                expected_commit_sha="a" * 40,
                expected_lock_sha256=lock_sha,
            )
