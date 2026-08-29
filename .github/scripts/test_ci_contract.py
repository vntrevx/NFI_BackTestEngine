#!/usr/bin/env python3
"""Dependency-free tests for the required CI path policy."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).parents[2]


def _load_module() -> ModuleType:
    source = Path(__file__).with_name("ci_contract.py")
    spec = importlib.util.spec_from_file_location("nfi_ci_contract", source)
    if spec is None or spec.loader is None:
        raise AssertionError("CI contract module is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_timing_module() -> ModuleType:
    source = Path(__file__).with_name("ci_timing.py")
    spec = importlib.util.spec_from_file_location("nfi_ci_timing", source)
    if spec is None or spec.loader is None:
        raise AssertionError("CI timing module is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_pytest_timing_module() -> ModuleType:
    source = Path(__file__).with_name("ci_pytest.py")
    spec = importlib.util.spec_from_file_location("nfi_ci_pytest", source)
    if spec is None or spec.loader is None:
        raise AssertionError("CI pytest timing module is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _timing_report(
    *,
    job: str,
    os_name: str,
    python: str,
    suite: str,
    steps: list[str],
    run_id: str = "9001",
    commit_sha: str = "a" * 40,
) -> dict[str, Any]:
    categories = {
        "dependency-sync": "dependency_sync",
        "native-build": "native_build",
        "python-tests": "python_test",
        "python-ast-identity": "python_test",
        "python-lint": "static_check",
        "python-type-check": "static_check",
        "rust-format": "rust_check",
        "rust-tests": "rust_check",
        "rust-lint": "rust_check",
        "parity-normal-routing": "parity",
        "parity-stops-only": "parity",
    }
    return {
        "schema_version": "1.0.0",
        "baseline_id": "ci-timing-v1",
        "identity": {
            "report_id": f"{job}-{os_name}-{python}-{suite}",
            "workflow": "CI",
            "workflow_ref": ".github/workflows/ci.yml",
            "repository": "vntrevx/NFI_BackTestEngine",
            "run_id": run_id,
            "run_attempt": "1",
            "commit_sha": commit_sha,
            "job": job,
            "os": os_name,
            "python": python,
            "suite": suite,
            "cache": {
                "key": "uv-lock-" + "b" * 64,
                "lock_sha256": "b" * 64,
                "rust_compiler_cache": "sccache-gha-v0.10.0",
            },
            "build": {
                "commit_sha": commit_sha,
                "target": f"{os_name}-{python}",
            },
            "source_dirty": False,
        },
        "steps": [
            {
                "name": step,
                "category": categories[step],
                "status": "completed",
                "duration_seconds": 1.0,
                "timeout_seconds": 900,
                "exit_code": 0,
            }
            for step in steps
        ],
    }


def _complete_timing_reports(
    *, run_id: str = "9001", commit_sha: str = "a" * 40
) -> list[dict[str, Any]]:
    contract = json.loads((ROOT / ".github/ci-contract.json").read_text(encoding="utf-8"))
    return [
        _timing_report(
            job=spec["job"],
            os_name=spec["os"],
            python=spec["python"],
            suite=spec["suite"],
            steps=spec["required_steps"],
            run_id=run_id,
            commit_sha=commit_sha,
        )
        for spec in contract["timing"]["reports"]
    ]


def _complete_pytest_reports(
    timing_reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pytest_timing = _load_pytest_timing_module()
    reports: list[dict[str, Any]] = []
    for timing_report in timing_reports:
        identity = timing_report["identity"]
        if not isinstance(identity, dict):
            raise AssertionError("synthetic timing identity must be an object")
        if identity["job"] != "python" or identity["suite"] != "full":
            continue
        records = [
            pytest_timing.build_test_record(
                nodeid="tests/parity/test_example.py::test_exact",
                outcome="passed",
                wall_seconds=0.25,
                cpu_seconds=0.20,
                peak_rss_bytes=1024,
            ),
            pytest_timing.build_test_record(
                nodeid="tests/test_release_contract.py::test_policy",
                outcome="passed",
                wall_seconds=0.5,
                cpu_seconds=0.4,
                peak_rss_bytes=2048,
            ),
        ]
        reports.append(
            pytest_timing.build_pytest_report(
                records,
                slowest_count=20,
                identity={
                    field: identity[field]
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
    return reports


def _trusted_compare_kwargs(
    timing: ModuleType, contract: dict[str, Any]
) -> dict[str, str]:
    candidate = timing.trusted_candidate_identity(
        contract,
        repository="vntrevx/NFI_BackTestEngine",
        workflow="CI",
        workflow_ref=".github/workflows/ci.yml",
        baseline_id="ci-timing-v1",
        candidate_commit="a" * 40,
        cache_lock_sha256="b" * 64,
    )
    return {
        "expected_repository": "vntrevx/NFI_BackTestEngine",
        "expected_workflow": "CI",
        "expected_workflow_ref": ".github/workflows/ci.yml",
        "expected_run_attempt": "1",
        "expected_commit_sha": "a" * 40,
        "expected_cache_sha256": "b" * 64,
        "expected_candidate_commit": "a" * 40,
        "expected_candidate_artifact_root": candidate["artifact_identity_root"],
        "expected_build_identity_root": candidate["build_identity_root"],
        "expected_baseline_id": "ci-timing-v1",
    }


class CiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()
        cls.contract = cls.module.load_contract(ROOT / ".github/ci-contract.json")

    def test_documentation_and_planning_use_fast_lane(self) -> None:
        paths = ["README.md", "docs/ci-policy.md", "planning/roadmap-state.json"]

        self.assertEqual(
            self.module.classify_paths(paths, self.contract),
            self.module.DOCS_CLASSIFICATION,
        )

    def test_policy_and_documentation_use_policy_lane(self) -> None:
        paths = ["README.md", ".github/workflows/ci.yml"]

        self.assertEqual(
            self.module.classify_paths(paths, self.contract),
            self.module.POLICY_CLASSIFICATION,
        )

    def test_compatibility_automation_uses_focused_lane(self) -> None:
        paths = [
            ".github/workflows/nfi-compatibility.yml",
            "python/nfi_backtest_engine/compatibility_automation.py",
            "scripts/compatibility_issue.py",
            "scripts/compatibility_review_pr.py",
            "tests/test_compatibility_automation.py",
            "tests/test_compatibility_issue.py",
            "tests/test_compatibility_review_pr.py",
            "tests/test_nfi_compatibility_workflow.py",
            ".github/workflows/ci.yml",
        ]

        self.assertEqual(
            self.module.classify_paths(paths, self.contract),
            self.module.AUTOMATION_CLASSIFICATION,
        )

    def test_runtime_or_unknown_paths_fail_closed_to_code(self) -> None:
        for paths in (
            ["python/nfi_backtest_engine/cli.py"],
            ["docs/ci-policy.md", "rust/crates/nfi-sim-core/Cargo.toml"],
            ["planning/futures-discovery-policy.json"],
            [".github/workflows/release.yml"],
            [
                "scripts/compatibility_issue.py",
                "rust/crates/nfi-sim-core/Cargo.toml",
            ],
            [],
        ):
            with self.subTest(paths=paths):
                self.assertEqual(
                    self.module.classify_paths(paths, self.contract),
                    self.module.CODE_CLASSIFICATION,
                )

    def test_affected_validation_plans_match_path_capabilities(self) -> None:
        base = self.contract["affected_validation"]["python_matrix"]["base"]
        ast = self.contract["affected_validation"]["python_matrix"]["ast"]
        platform = self.contract["affected_validation"]["python_matrix"]["platform"]
        self.assertEqual(
            base,
            [{"os": "ubuntu-latest", "python-version": "3.12", "suite": "full"}],
        )
        self.assertEqual(
            platform,
            [{"os": "macos-14", "python-version": "3.12", "suite": "full"}],
        )
        self.assertEqual(
            ast,
            [
                {
                    "os": "ubuntu-latest",
                    "python-version": "3.13",
                    "suite": "ast-compat",
                },
                {
                    "os": "ubuntu-latest",
                    "python-version": "3.14",
                    "suite": "ast-compat",
                },
            ],
        )
        cases = (
            (
                "python/nfi_backtest_engine/cli.py",
                "affected",
                ["parity", "python"],
                base,
                ["python", "python-quality", "parity", "timing"],
            ),
            (
                "rust/crates/nfi-sim-core/src/exit.rs",
                "affected",
                ["parity", "python", "rust"],
                base,
                ["python", "python-quality", "rust-quality", "parity", "timing"],
            ),
            (
                "python/nfi_backtest_engine/indicator_program.py",
                "affected",
                ["ast", "parity", "python"],
                base + ast,
                ["python", "python-quality", "parity", "timing"],
            ),
            (
                "python/nfi_backtest_engine/windows_job.py",
                "affected",
                ["parity", "platform", "python"],
                base + platform,
                ["python", "python-quality", "parity", "timing"],
            ),
            (
                "pyproject.toml",
                "full",
                ["ast", "parity", "platform", "python", "rust"],
                base + platform + ast,
                ["python", "python-quality", "rust-quality", "parity", "timing"],
            ),
            (
                "unclassified/input.bin",
                "full",
                ["ast", "parity", "platform", "python", "rust"],
                base + platform + ast,
                ["python", "python-quality", "rust-quality", "parity", "timing"],
            ),
        )
        for path, mode, capabilities, matrix, jobs in cases:
            with self.subTest(path=path):
                plan = self.module.plan_affected_validation(
                    [path],
                    self.contract,
                    event_name="pull_request",
                )
                self.assertEqual(plan["mode"], mode)
                self.assertEqual(plan["capabilities"], capabilities)
                self.assertEqual(plan["python_matrix"], matrix)
                self.assertEqual(plan["selected_jobs"], jobs)

    def test_manual_and_empty_diff_validation_fail_closed_to_full(self) -> None:
        for paths, event_name in (([], "pull_request"), (["README.md"], "push")):
            with self.subTest(paths=paths, event_name=event_name):
                plan = self.module.plan_affected_validation(
                    paths,
                    self.contract,
                    event_name=event_name,
                )
                self.assertEqual(plan["mode"], "full")
                self.assertEqual(plan["classification"], self.module.CODE_CLASSIFICATION)
                self.assertIn("rust-quality", plan["selected_jobs"])


    def test_required_result_matrix_matches_selected_lane(self) -> None:
        paths_by_classification = {
            self.module.DOCS_CLASSIFICATION: ["README.md"],
            self.module.POLICY_CLASSIFICATION: [".github/workflows/ci.yml"],
            self.module.AUTOMATION_CLASSIFICATION: [
                ".github/workflows/nfi-compatibility.yml"
            ],
            self.module.CODE_CLASSIFICATION: ["pyproject.toml"],
        }
        conditional = self.contract["conditional_job_ids"]
        for classification, paths in paths_by_classification.items():
            plan = self.module.plan_affected_validation(
                paths,
                self.contract,
                event_name="pull_request",
            )
            selected = set(plan["selected_jobs"])
            results = {
                job: "success" if job in selected else "skipped"
                for job in conditional
            }
            with self.subTest(classification=classification):
                self.assertTrue(
                    self.module.required_results_pass(
                        classification,
                        validation_plan=plan,
                        changes_result="success",
                        documentation_result="success",
                        job_results=results,
                        contract=self.contract,
                    )
                )
                wrong = dict(results)
                wrong[conditional[0]] = (
                    "skipped" if wrong[conditional[0]] == "success" else "success"
                )
                self.assertFalse(
                    self.module.required_results_pass(
                        classification,
                        validation_plan=plan,
                        changes_result="success",
                        documentation_result="success",
                        job_results=wrong,
                        contract=self.contract,
                    )
                )

    def test_current_workflow_uses_planned_job_and_python_matrices(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        for job_id in (
            "changes",
            "documentation",
            "policy",
            "python",
            "python-quality",
            "rust-quality",
            "parity",
            "timing",
            "required",
        ):
            self.assertIn(f"  {job_id}:\n", workflow)
        self.assertIn(
            "matrix: ${{ fromJSON(needs.changes.outputs.python_matrix_json) }}",
            workflow,
        )
        self.assertIn("name: Python quality checks", workflow)
        self.assertIn("name: Rust quality checks", workflow)
        self.assertIn("name: Native full-parity fixtures", workflow)
        self.assertNotIn("  quality:\n", workflow)
        self.assertNotIn("windows-latest", workflow)

    def test_workflow_exposes_stable_aggregate_and_lane_conditions(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("name: Required CI", workflow)
        self.assertIn("needs.changes.outputs.policy_changes == 'true'", workflow)
        self.assertIn("needs.changes.outputs.automation_changes == 'true'", workflow)
        self.assertIn("needs.changes.outputs.selected_jobs_json", workflow)
        self.assertIn("needs.changes.outputs.python_matrix_json", workflow)
        self.assertIn("Run focused compatibility automation tests", workflow)
        self.assertNotIn("pull_request_target:", workflow)
        self.assertNotIn('paths-ignore:', workflow)
        for path in self.contract["push"]["release_paths"]:
            self.assertIn(f'- "{path}"', workflow)

    def test_workflow_records_and_requires_exact_timing_contract_coverage(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        timing = self.contract["timing"]

        self.assertEqual(
            workflow.count("python .github/scripts/ci_timing.py init"),
            len({report["job"] for report in timing["reports"]}),
        )
        self.assertEqual(
            set(re.findall(r"--step ([a-z-]+)", workflow)),
            set(timing["step_categories"]),
        )
        for identity_argument in (
            '--workflow "${{ github.workflow }}"',
            '--repository "${{ github.repository }}"',
            '--run-id "${{ github.run_id }}"',
            '--run-attempt "${{ github.run_attempt }}"',
            '--commit-sha "${{ github.sha }}"',
            "--cache-file uv.lock",
        ):
            self.assertIn(identity_argument, workflow)
        self.assertIn("name: Validate CI timing evidence", workflow)
        self.assertIn("pattern: ci-timing-*", workflow)
        self.assertIn(".github/scripts/ci_pytest.py", workflow)
        self.assertIn(".ci/pytest-${{ matrix.os }}", workflow)
        for trusted_argument in (
            "--expected-repository",
            "--expected-workflow",
            "--expected-workflow-ref",
            "--expected-baseline-id",
            "--expected-run-id",
            "--expected-run-attempt",
            "--expected-commit-sha",
            "--expected-candidate-commit",
            "--expected-cache-sha256",
            "--validation-plan-json",
        ):
            self.assertIn(trusted_argument, workflow)
        self.assertIn("CACHE_LOCK_SHA256: ${{ hashFiles('uv.lock') }}", workflow)
        self.assertIn("--job-result \"timing=$TIMING_RESULT\"", workflow)
        self.assertEqual(timing["comparison_run_count"], 3)
        self.assertEqual(timing["rust_compiler_cache"], "sccache-gha-v0.10.0")

    def test_workflow_uses_bounded_rust_compiler_caching(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        cache_action = (
            "mozilla-actions/sccache-action@"
            "7d986dd989559c6ecdb630a3fd2557667be217ad # v0.0.9"
        )

        self.assertEqual(workflow.count(cache_action), 3)
        self.assertEqual(workflow.count('version: "v0.10.0"'), 3)
        self.assertEqual(workflow.count("Show Rust compiler cache statistics"), 3)
        self.assertEqual(workflow.count("RUSTC_WRAPPER: sccache"), 4)
        self.assertNotIn("path: dist", workflow)

    def test_timing_report_rejects_missing_operating_system(self) -> None:
        timing = _load_timing_module()
        for missing_os in ("ubuntu-latest", "macos-14"):
            with self.subTest(missing_os=missing_os):
                reports = [
                    report
                    for report in _complete_timing_reports()
                    if report["identity"]["os"] != missing_os  # type: ignore[index]
                ]

                with self.assertRaisesRegex(
                    ValueError, f"missing timing reports.*{missing_os}"
                ):
                    timing.validate_timing_reports(
                        reports,
                        contract=self.contract,
                        expected_run_id="9001",
                        expected_commit_sha="a" * 40,
                    )

    def test_timing_validation_accepts_only_the_authenticated_selected_subset(
        self,
    ) -> None:
        timing = _load_timing_module()
        plan = self.module.plan_affected_validation(
            ["python/nfi_backtest_engine/cli.py"],
            self.contract,
            event_name="pull_request",
        )
        expected_ids = plan["timing_reports"]
        reports = [
            report
            for report in _complete_timing_reports()
            if report["identity"]["report_id"] in expected_ids  # type: ignore[index]
        ]

        aggregate = timing.validate_timing_reports(
            reports,
            contract=self.contract,
            expected_run_id="9001",
            expected_commit_sha="a" * 40,
            expected_report_ids=expected_ids,
        )

        self.assertEqual(aggregate["report_count"], len(expected_ids))
        self.assertEqual(
            timing._validation_plan_report_ids(json.dumps(plan), self.contract),
            expected_ids,
        )
        forged = copy.deepcopy(plan)
        forged["timing_reports"] = list(reversed(expected_ids))
        with self.assertRaisesRegex(ValueError, "invalid or unordered"):
            timing._validation_plan_report_ids(json.dumps(forged), self.contract)

    def test_timing_report_rejects_missing_required_step(self) -> None:
        timing = _load_timing_module()
        reports = _complete_timing_reports()
        reports[0]["steps"] = reports[0]["steps"][:-1]  # type: ignore[index]

        with self.assertRaisesRegex(ValueError, "missing required timing steps.*python-tests"):
            timing.validate_timing_reports(
                reports,
                contract=self.contract,
                expected_run_id="9001",
                expected_commit_sha="a" * 40,
            )

    def test_timing_report_rejects_stale_run_or_commit_identity(self) -> None:
        timing = _load_timing_module()
        for field, stale_value, message in (
            ("run_id", "8999", "run identity mismatch"),
            ("commit_sha", "c" * 40, "commit identity mismatch"),
        ):
            reports = _complete_timing_reports()
            reports[0]["identity"][field] = stale_value  # type: ignore[index]
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, message
            ):
                timing.validate_timing_reports(
                    reports,
                    contract=self.contract,
                    expected_run_id="9001",
                    expected_commit_sha="a" * 40,
                )

    def test_timing_report_rejects_stale_cache_build_dirty_and_incomplete_state(
        self,
    ) -> None:
        timing = _load_timing_module()
        cases = (
            ("stale-cache", ("identity", "cache", "key"), "uv-lock-stale", "stale cache"),
            (
                "stale-rust-cache",
                ("identity", "cache", "rust_compiler_cache"),
                "sccache-gha-v0.9.0",
                "Rust compiler cache identity mismatch",
            ),
            ("stale-build", ("identity", "build", "commit_sha"), "c" * 40, "stale build"),
            ("dirty", ("identity", "source_dirty"), True, "dirty worktree"),
            ("timed-out", ("steps", 0, "status"), "timed_out", "not completed"),
            ("skipped", ("steps", 0, "status"), "skipped", "not completed"),
        )
        for name, path, value, message in cases:
            reports = _complete_timing_reports()
            target: object = reports[0]
            for component in path[:-1]:
                target = target[component]  # type: ignore[index]
            target[path[-1]] = value  # type: ignore[index]
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                timing.validate_timing_reports(
                    reports,
                    contract=self.contract,
                    expected_run_id="9001",
                    expected_commit_sha="a" * 40,
                )

    def test_timing_report_rejects_flaky_step_ordering(self) -> None:
        timing = _load_timing_module()
        reports = _complete_timing_reports()
        reports[0]["steps"] = list(reversed(reports[0]["steps"]))  # type: ignore[index]

        with self.assertRaisesRegex(ValueError, "step ordering"):
            timing.validate_timing_reports(
                reports,
                contract=self.contract,
                expected_run_id="9001",
                expected_commit_sha="a" * 40,
            )

    def test_timing_aggregate_supports_identity_bound_three_run_comparison(
        self,
    ) -> None:
        timing = _load_timing_module()
        aggregates = [
            timing.validate_timing_reports(
                _complete_timing_reports(run_id=run_id),
                contract=self.contract,
                expected_run_id=run_id,
                expected_commit_sha="a" * 40,
            )
            for run_id in ("9001", "9002", "9003")
        ]

        comparison = timing.compare_three_runs(
            aggregates,
            contract=self.contract,
            **_trusted_compare_kwargs(timing, self.contract),
        )

        self.assertEqual(comparison["run_ids"], ["9001", "9002", "9003"])
        self.assertEqual(comparison["commit_sha"], "a" * 40)
        self.assertTrue(comparison["steps"])
        stale = copy.deepcopy(aggregates)
        stale[2]["identity"]["commit_sha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            timing.compare_three_runs(
                stale,
                contract=self.contract,
                **_trusted_compare_kwargs(timing, self.contract),
            )

    def test_pytest_timing_report_has_actionable_per_test_resources(
        self,
    ) -> None:
        pytest_timing = _load_pytest_timing_module()
        records = [
            pytest_timing.build_test_record(
                nodeid="tests/parity/test_example.py::test_exact",
                outcome="passed",
                wall_seconds=0.25,
                cpu_seconds=0.20,
                peak_rss_bytes=1024,
            ),
            pytest_timing.build_test_record(
                nodeid="tests/test_release_contract.py::test_policy",
                outcome="passed",
                wall_seconds=0.5,
                cpu_seconds=0.4,
                peak_rss_bytes=2048,
            ),
        ]

        report = pytest_timing.build_pytest_report(records, slowest_count=1)

        self.assertEqual(
            [record["nodeid"] for record in report["tests"]],
            sorted(record["nodeid"] for record in records),
        )
        for record in report["tests"]:
            self.assertRegex(record["test_id"], r"^[0-9a-f]{64}$")
            self.assertIn(record["owner"], {"parity", "release_ci"})
            self.assertGreaterEqual(record["resources"]["cpu_seconds"], 0)
            self.assertGreater(record["resources"]["peak_rss_bytes"], 0)
        self.assertEqual(
            report["slowest_tests"][0]["nodeid"],
            "tests/test_release_contract.py::test_policy",
        )

    def test_timing_report_rejects_stale_run_attempt(self) -> None:
        timing = _load_timing_module()
        reports = _complete_timing_reports()
        reports[0]["identity"]["run_attempt"] = "999"  # type: ignore[index]

        with self.assertRaisesRegex(ValueError, "run attempt identity mismatch"):
            timing.validate_timing_reports(
                reports,
                contract=self.contract,
                expected_run_id="9001",
                expected_run_attempt="1",
                expected_commit_sha="a" * 40,
            )

    def test_three_run_comparison_rejects_stale_repository_cache_or_candidate(
        self,
    ) -> None:
        timing = _load_timing_module()
        aggregates = [
            timing.validate_timing_reports(
                _complete_timing_reports(run_id=run_id),
                contract=self.contract,
                expected_run_id=run_id,
                expected_commit_sha="a" * 40,
            )
            for run_id in ("9001", "9002", "9003")
        ]
        cases = (
            (("identity", "repository"), "other/repository"),
            (("identity", "workflow"), "Other CI"),
            (("identity", "workflow_ref"), ".github/workflows/other.yml"),
            (("identity", "cache_lock_sha256"), "c" * 64),
            (("identity", "candidate", "commit_sha"), "c" * 40),
            (("baseline_id",), "other-baseline"),
            (("identity", "commit_sha"), "c" * 40),
        )
        for path, value in cases:
            stale = copy.deepcopy(aggregates)
            target: object = stale[2]
            for component in path[:-1]:
                target = target[component]  # type: ignore[index]
            target[path[-1]] = value  # type: ignore[index]
            with self.subTest(path=path), self.assertRaisesRegex(
                ValueError, "comparison identity mismatch"
            ):
                timing.compare_three_runs(
                    stale,
                    contract=self.contract,
                    **_trusted_compare_kwargs(timing, self.contract),
                )

    @unittest.skipIf(os.name == "nt", "POSIX regression uses signal.pause")
    def test_timed_command_terminates_descendant_process_tree(self) -> None:
        timing = _load_timing_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "timing.json"
            event_path = root / "descendant.pid"
            report_path.write_text(
                json.dumps(_complete_timing_reports()[0]), encoding="utf-8"
            )
            child = (
                "import os,signal; "
                "print(os.getpid(), flush=True); "
                "signal.pause()"
            )
            parent = (
                "import pathlib,signal,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]], "
                "stdout=subprocess.PIPE,text=True); "
                "pid=child.stdout.readline().strip(); "
                "pathlib.Path(sys.argv[1]).write_text(pid, encoding='utf-8'); "
                "signal.pause()"
            )
            descendant_pid: int | None = None
            try:
                self.assertEqual(
                    timing.run_step(
                        report_path=report_path,
                        step_name="dependency-sync",
                        timeout_seconds=1,
                        command=[
                            sys.executable,
                            "-c",
                            parent,
                            str(event_path),
                            child,
                        ],
                    ),
                    124,
                )
                descendant_pid = int(event_path.read_text(encoding="utf-8"))
                with self.assertRaises(ProcessLookupError):
                    os.kill(descendant_pid, 0)
            finally:
                if descendant_pid is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(descendant_pid, signal.SIGKILL)

    def test_pytest_timing_rejects_non_finite_metrics(self) -> None:
        timing = _load_timing_module()
        for field, value in (
            (("duration_seconds",), float("nan")),
            (("duration_seconds",), float("inf")),
            (("duration_seconds",), float("-inf")),
            (("resources", "cpu_seconds"), float("nan")),
            (("resources", "cpu_seconds"), float("inf")),
            (("resources", "cpu_seconds"), float("-inf")),
        ):
            timing_reports = _complete_timing_reports()
            pytest_reports = _complete_pytest_reports(timing_reports)
            target: object = pytest_reports[0]["tests"][0]  # type: ignore[index]
            for component in field[:-1]:
                target = target[component]  # type: ignore[index]
            target[field[-1]] = value  # type: ignore[index]
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                ValueError, "non-finite pytest timing metric"
            ):
                timing.validate_pytest_reports(
                    pytest_reports,
                    timing_reports=timing_reports,
                    contract=self.contract,
                )

    def test_three_run_comparison_rejects_stale_run_attempt(self) -> None:
        timing = _load_timing_module()
        aggregates = [
            timing.validate_timing_reports(
                _complete_timing_reports(run_id=run_id),
                contract=self.contract,
                expected_run_id=run_id,
                expected_run_attempt="1",
                expected_commit_sha="a" * 40,
            )
            for run_id in ("9001", "9002", "9003")
        ]
        for aggregate in aggregates:
            aggregate["identity"]["run_attempt"] = "999"

        with self.assertRaisesRegex(ValueError, "run attempt identity mismatch"):
            timing.compare_three_runs(
                aggregates,
                contract=self.contract,
                **_trusted_compare_kwargs(timing, self.contract),
            )

    def test_three_run_comparison_rejects_uniform_trusted_identity_forgery(
        self,
    ) -> None:
        timing = _load_timing_module()
        original = [
            timing.validate_timing_reports(
                _complete_timing_reports(run_id=run_id),
                contract=self.contract,
                expected_run_id=run_id,
                expected_run_attempt="1",
                expected_commit_sha="a" * 40,
            )
            for run_id in ("9001", "9002", "9003")
        ]
        cases = (
            (("baseline_id",), "forged-baseline"),
            (("identity", "repository"), "attacker/repository"),
            (("identity", "workflow"), "Attacker CI"),
            (("identity", "workflow_ref"), ".github/workflows/attacker.yml"),
            (("identity", "cache_lock_sha256"), "c" * 64),
            (("identity", "commit_sha"), "c" * 40),
            (("identity", "candidate", "commit_sha"), "c" * 40),
            (("identity", "candidate", "artifact_identity_root"), "d" * 64),
            (("identity", "candidate", "build_identity_root"), "e" * 64),
            (
                (
                    "identity",
                    "candidate",
                    "build_identities",
                    0,
                    "target",
                ),
                "forged-target",
            ),
        )
        for path, forged_value in cases:
            aggregates = copy.deepcopy(original)
            for aggregate in aggregates:
                target: object = aggregate
                for component in path[:-1]:
                    target = target[component]  # type: ignore[index]
                target[path[-1]] = forged_value  # type: ignore[index]
            with self.subTest(path=path), self.assertRaisesRegex(
                ValueError, "trusted comparison identity mismatch"
            ):
                timing.compare_three_runs(
                    aggregates,
                    contract=self.contract,
                    **_trusted_compare_kwargs(timing, self.contract),
                )

    def test_windows_job_lifecycle_assigns_suspended_root_before_resume_and_reaps_tree(
        self,
    ) -> None:
        timing = _load_timing_module()

        class FakeWindowsJobApi:
            def __init__(self, wait_result: int | None | BaseException = 0) -> None:
                self.events: list[str] = []
                self.descendant_alive = True
                self.wait_result = wait_result

            def create_suspended(self, command: list[str], cwd: Path | None) -> object:
                del command, cwd
                self.events.append("create-suspended")
                return object()

            def create_kill_on_close_job(self) -> object:
                self.events.append("create-job")
                return object()

            def assign(self, job: object, process: object) -> None:
                del job, process
                self.events.append("assign")

            def resume(self, process: object) -> None:
                del process
                self.events.append("resume")

            def wait_root(self, process: object, timeout_seconds: int) -> int | None:
                del process, timeout_seconds
                self.events.append("root-exited")
                if isinstance(self.wait_result, BaseException):
                    raise self.wait_result
                return self.wait_result

            def terminate_job(self, job: object, exit_code: int) -> None:
                del job, exit_code
                self.events.append("terminate-job")
                self.descendant_alive = False

            def wait_job_empty(self, job: object) -> None:
                del job
                self.events.append("job-empty")
                if self.descendant_alive:
                    raise AssertionError("descendant survived root exit")

            def terminate_process(self, process: object, exit_code: int) -> None:
                del process, exit_code
                self.events.append("terminate-process")

            def close(self, job: object, process: object | None) -> None:
                del job, process
                self.events.append("close")

        api = FakeWindowsJobApi()
        lifecycle = timing.WindowsJobLifecycle(api)

        self.assertEqual(
            lifecycle.run(["python", "worker.py"], cwd=None, timeout_seconds=30),
            (0, "completed"),
        )
        self.assertEqual(
            api.events,
            [
                "create-job",
                "create-suspended",
                "assign",
                "resume",
                "root-exited",
                "terminate-job",
                "job-empty",
                "close",
            ],
        )
        self.assertFalse(api.descendant_alive)
        timeout_api = FakeWindowsJobApi(None)
        self.assertEqual(
            timing.WindowsJobLifecycle(timeout_api).run(
                ["python", "worker.py"], cwd=None, timeout_seconds=30
            ),
            (124, "timed_out"),
        )
        self.assertEqual(
            timeout_api.events[-3:],
            ["terminate-job", "job-empty", "close"],
        )
        interrupted_api = FakeWindowsJobApi(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            timing.WindowsJobLifecycle(interrupted_api).run(
                ["python", "worker.py"], cwd=None, timeout_seconds=30
            )
        self.assertEqual(
            interrupted_api.events[-3:],
            ["terminate-job", "job-empty", "close"],
        )
        self.assertFalse(interrupted_api.descendant_alive)
        source = Path(__file__).with_name("ci_timing.py").read_text(encoding="utf-8")
        self.assertIn("CREATE_SUSPENDED", source)
        self.assertIn("JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", source)
        self.assertIn("AssignProcessToJobObject", source)
        self.assertIn("JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO", source)
        self.assertNotIn('"taskkill"', source)

    def test_windows_job_setup_closes_every_partial_handle_exactly_once(
        self,
    ) -> None:
        timing = _load_timing_module()

        class FakeKernel32:
            def __init__(self, fail_at: str | None) -> None:
                self.fail_at = fail_at
                self.set_information_calls = 0
                self.closed: list[int] = []

            def CreateJobObjectW(self, security: object, name: object) -> int:
                del security, name
                return 0 if self.fail_at == "create-job" else 101

            def CreateIoCompletionPort(
                self,
                source: object,
                existing: object,
                key: int,
                concurrency: int,
            ) -> int:
                del source, existing, key, concurrency
                return 0 if self.fail_at == "create-completion-port" else 202

            def SetInformationJobObject(
                self,
                handle: int,
                information_class: int,
                information: object,
                size: int,
            ) -> int:
                del handle, information_class, information, size
                self.set_information_calls += 1
                if self.fail_at == "set-limit" and self.set_information_calls == 1:
                    return 0
                if self.fail_at == "associate-port" and self.set_information_calls == 2:
                    return 0
                return 1

            def CloseHandle(self, handle: int) -> int:
                self.closed.append(handle)
                return 1

        cases = (
            ("create-job", []),
            ("create-completion-port", [101]),
            ("set-limit", [202, 101]),
            ("associate-port", [202, 101]),
        )
        for failure, expected_closed in cases:
            kernel = FakeKernel32(failure)
            api = timing.CtypesWindowsJobApi(kernel32=kernel)
            with self.subTest(failure=failure), self.assertRaises(OSError):
                api.create_kill_on_close_job()
            self.assertEqual(kernel.closed, expected_closed)
            self.assertEqual(len(kernel.closed), len(set(kernel.closed)))

        kernel = FakeKernel32(None)
        api = timing.CtypesWindowsJobApi(kernel32=kernel)
        job = api.create_kill_on_close_job()
        self.assertEqual(kernel.closed, [])
        api.close(job, None)
        self.assertEqual(kernel.closed, [202, 101])

    def test_windows_process_and_thread_handles_transfer_and_close_once(self) -> None:
        timing = _load_timing_module()

        class FakeKernel32:
            def __init__(self) -> None:
                self.closed: list[int] = []
                self.fail_create_process = False

            def CreateJobObjectW(self, security: object, name: object) -> int:
                del security, name
                return 101

            def CreateIoCompletionPort(self, *args: object) -> int:
                del args
                return 202

            def SetInformationJobObject(self, *args: object) -> int:
                del args
                return 1

            def CreateProcessW(self, *args: object) -> int:
                if self.fail_create_process:
                    return 0
                information = args[-1]._obj  # type: ignore[attr-defined]
                information.hProcess = 303
                information.hThread = 404
                information.dwProcessId = 505
                information.dwThreadId = 606
                return 1

            def CloseHandle(self, handle: int) -> int:
                self.closed.append(handle)
                return 1

        kernel = FakeKernel32()
        api = timing.CtypesWindowsJobApi(kernel32=kernel)
        job = api.create_kill_on_close_job()
        process = api.create_suspended(["python", "worker.py"], None)
        self.assertEqual(kernel.closed, [])
        api.close(job, process)
        self.assertEqual(kernel.closed, [404, 303, 202, 101])
        self.assertEqual(len(kernel.closed), len(set(kernel.closed)))

        kernel = FakeKernel32()
        kernel.fail_create_process = True
        api = timing.CtypesWindowsJobApi(kernel32=kernel)
        job = api.create_kill_on_close_job()
        with self.assertRaises(OSError):
            api.create_suspended(["python", "worker.py"], None)
        api.close(job, None)
        self.assertEqual(kernel.closed, [202, 101])

    def test_timing_json_io_rejects_nonstandard_non_finite_numbers(self) -> None:
        timing = _load_timing_module()
        pytest_timing = _load_pytest_timing_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for module, name in ((timing, "timing"), (pytest_timing, "pytest")):
                output = root / f"{name}.json"
                with self.subTest(module=name), self.assertRaises(ValueError):
                    module._write_json(output, {"value": float("nan")})
                self.assertFalse(output.exists())
                output.write_text('{"value": NaN}\n', encoding="utf-8")
                reader = (
                    module._read_json
                    if module is timing
                    else module._read_json_object
                )
                with self.assertRaisesRegex(ValueError, "invalid|malformed"):
                    reader(output)

    def test_pytest_timing_rejects_forged_slowest_record(self) -> None:
        timing = _load_timing_module()
        timing_reports = _complete_timing_reports()
        pytest_reports = _complete_pytest_reports(timing_reports)
        forged = pytest_reports[0]["slowest_tests"][0]  # type: ignore[index]
        forged["nodeid"] = "tests/forged.py::test_forged"
        forged["duration_seconds"] = 999.0
        forged["resources"] = {"cpu_seconds": 999.0, "peak_rss_bytes": 1}

        with self.assertRaisesRegex(ValueError, "slowest-test evidence mismatch"):
            timing.validate_pytest_reports(
                pytest_reports,
                timing_reports=timing_reports,
                contract=self.contract,
            )

    @unittest.skipIf(os.name == "nt", "POSIX regression delivers SIGINT by PID")
    def test_sigint_repetitions_cleanup_exact_ready_process_trees(self) -> None:
        timing_script = Path(__file__).with_name("ci_timing.py")
        parent_source = (
            "import os,signal,socket,subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import os,signal; print(os.getpid(),flush=True); signal.pause()'],"
            "stdout=subprocess.PIPE,text=True); "
            "child_pid=child.stdout.readline().strip(); "
            "event=socket.create_connection((sys.argv[1],int(sys.argv[2]))); "
            "event.sendall(f'{os.getpid()},{child_pid}'.encode()); "
            "event.close(); signal.pause()"
        )
        for repetition in range(2):
            with self.subTest(repetition=repetition), tempfile.TemporaryDirectory() as directory:
                report_path = Path(directory) / "timing.json"
                report_path.write_text(
                    json.dumps(_complete_timing_reports()[0]), encoding="utf-8"
                )
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                host, port = listener.getsockname()
                runner = subprocess.Popen(
                    [
                        sys.executable,
                        str(timing_script),
                        "run-step",
                        "--report",
                        str(report_path),
                        "--step",
                        "dependency-sync",
                        "--timeout-seconds",
                        "30",
                        "--",
                        sys.executable,
                        "-c",
                        parent_source,
                        str(host),
                        str(port),
                    ]
                )
                parent_pid: int | None = None
                descendant_pid: int | None = None
                try:
                    connection, _ = listener.accept()
                    with connection:
                        ready = connection.recv(128).decode("ascii")
                    parent_pid, descendant_pid = (int(pid) for pid in ready.split(","))
                    os.kill(runner.pid, signal.SIGINT)
                    self.assertNotEqual(runner.wait(timeout=5), 0)
                    persisted = json.loads(report_path.read_text(encoding="utf-8"))
                    self.assertEqual(persisted["steps"][0]["status"], "interrupted")
                    for pid in (parent_pid, descendant_pid):
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                finally:
                    listener.close()
                    if runner.poll() is None:
                        runner.kill()
                        runner.wait()
                    if parent_pid is not None:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(parent_pid, signal.SIGKILL)
                    if descendant_pid is not None:
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(descendant_pid, signal.SIGKILL)

    def test_changed_json_is_validated_without_project_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "planning/state.json"
            target.parent.mkdir()
            target.write_text('{"complete": true}\n', encoding="utf-8")

            self.assertEqual(
                self.module.validate_text_paths(root, ["planning/state.json"]),
                ["planning/state.json"],
            )

            target.write_text('{"complete": }\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed JSON is invalid"):
                self.module.validate_text_paths(root, ["planning/state.json"])

    def test_dependabot_avoids_known_noisy_update_shapes(self) -> None:
        dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")

        self.assertEqual(dependabot.count('interval: "quarterly"'), 3)
        self.assertNotIn('interval: "monthly"', dependabot)
        self.assertEqual(dependabot.count("open-pull-requests-limit: 0"), 3)
        self.assertNotIn("open-pull-requests-limit: 1", dependabot)
        self.assertIn('update-types:\n          - "patch"', dependabot)
        self.assertIn('- "version-update:semver-major"', dependabot)
        self.assertIn('- "version-update:semver-minor"', dependabot)
        self.assertIn('- dependency-name: "ta-lib"', dependabot)


if __name__ == "__main__":
    unittest.main()
