#!/usr/bin/env python3
"""Dependency-free tests for the required CI path policy."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[2]


def _load_module() -> ModuleType:
    source = Path(__file__).with_name("ci_contract.py")
    spec = importlib.util.spec_from_file_location("nfi_ci_contract", source)
    if spec is None or spec.loader is None:
        raise AssertionError("CI contract module is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def test_runtime_or_unknown_paths_fail_closed_to_code(self) -> None:
        for paths in (
            ["python/nfi_backtest_engine/cli.py"],
            ["docs/ci-policy.md", "rust/crates/nfi-sim-core/Cargo.toml"],
            ["planning/futures-discovery-policy.json"],
            [".github/workflows/release.yml"],
            [],
        ):
            with self.subTest(paths=paths):
                self.assertEqual(
                    self.module.classify_paths(paths, self.contract),
                    self.module.CODE_CLASSIFICATION,
                )

    def test_required_result_matrix_matches_selected_lane(self) -> None:
        conditional = self.contract["conditional_job_ids"]
        for classification, selected_jobs in self.contract["classifications"].items():
            selected = set(selected_jobs)
            results = {
                job: "success" if job in selected else "skipped"
                for job in conditional
            }
            with self.subTest(classification=classification):
                self.assertTrue(
                    self.module.required_results_pass(
                        classification,
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
                        changes_result="success",
                        documentation_result="success",
                        job_results=wrong,
                        contract=self.contract,
                    )
                )

    def test_workflow_exposes_stable_aggregate_and_lane_conditions(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("name: Required CI", workflow)
        self.assertIn("needs.changes.outputs.policy_changes == 'true'", workflow)
        self.assertIn("needs.changes.outputs.code_changes == 'true'", workflow)
        self.assertNotIn("pull_request_target:", workflow)
        self.assertIn('paths-ignore:', workflow)
        self.assertIn('- "planning/compatibility-reviews/**"', workflow)
        for path in self.contract["push"]["release_paths"]:
            self.assertIn(f'- "{path}"', workflow)

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

        self.assertIn('update-types:\n          - "patch"', dependabot)
        self.assertIn('- "version-update:semver-major"', dependabot)
        self.assertIn('- "version-update:semver-minor"', dependabot)
        self.assertIn('- dependency-name: "ta-lib"', dependabot)


if __name__ == "__main__":
    unittest.main()
