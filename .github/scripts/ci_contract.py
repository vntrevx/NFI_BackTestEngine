#!/usr/bin/env python3
"""Classify CI paths and enforce the stable aggregate check contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeGuard

AUTOMATION_CLASSIFICATION = "automation-only"
CODE_CLASSIFICATION = "code"
DOCS_CLASSIFICATION = "docs-only"
POLICY_CLASSIFICATION = "policy-only"
AFFECTED_VALIDATION_PLAN_VERSION = "affected-validation-plan-v1"
SUCCESS = "success"
SKIPPED = "skipped"
ZERO_SHA = "0" * 40


def load_contract(path: str | Path) -> dict[str, Any]:
    """Load and minimally validate the CI policy consumed by the workflow."""
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid CI contract: {source}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != "1.3.0":
        raise ValueError("CI contract schema_version must be 1.3.0")
    docs = document.get("docs_only")
    policy = document.get("policy_only")
    automation = document.get("automation_only")
    affected = document.get("affected_validation")
    jobs = document.get("jobs")
    conditional_jobs = document.get("conditional_job_ids")
    classifications = document.get("classifications")
    required = document.get("required_check")
    concurrency = document.get("concurrency")
    pull_request = document.get("pull_request")
    push = document.get("push")
    timing = document.get("timing")
    nightly = document.get("nightly")
    protection = document.get("branch_protection")
    if (
        not isinstance(docs, dict)
        or not _string_list(docs.get("prefixes"))
        or not _string_list(docs.get("files"))
        or not isinstance(policy, dict)
        or not _string_list(policy.get("prefixes"))
        or not _string_list(policy.get("files"))
        or not isinstance(automation, dict)
        or not isinstance(automation.get("prefixes"), list)
        or any(
            not isinstance(prefix, str) or not prefix
            for prefix in automation.get("prefixes", [])
        )
        or not _string_list(automation.get("files"))
        or not _valid_affected_validation(affected, jobs, conditional_jobs)
        or not isinstance(jobs, dict)
        or not _string_list(conditional_jobs)
        or not isinstance(classifications, dict)
        or set(classifications)
        != {
            AUTOMATION_CLASSIFICATION,
            DOCS_CLASSIFICATION,
            POLICY_CLASSIFICATION,
            CODE_CLASSIFICATION,
        }
        or any(not isinstance(value, list) for value in classifications.values())
        or any(
            set(value) - set(conditional_jobs)
            for value in classifications.values()
        )
        or set().union(*(set(value) for value in classifications.values()))
        != set(conditional_jobs)
        or not isinstance(required, dict)
        or not isinstance(required.get("name"), str)
        or required.get("job_id") not in jobs
        or any(job not in jobs for job in conditional_jobs)
        or any(
            not isinstance(job, dict)
            or not isinstance(job.get("name"), str)
            or not isinstance(job.get("timeout_minutes"), int)
            or job["timeout_minutes"] <= 0
            for job in jobs.values()
        )
        or not isinstance(concurrency, dict)
        or concurrency.get("cancel_in_progress") is not True
        or not isinstance(concurrency.get("group"), str)
        or not isinstance(pull_request, dict)
        or pull_request.get("event") != "pull_request"
        or pull_request.get("permissions") != {"contents": "read"}
        or pull_request.get("allows_secrets") is not False
        or pull_request.get("allows_privileged_fork_execution") is not False
        or pull_request.get("allows_official_reference") is not False
        or not isinstance(
            pull_request.get("required_capabilities_by_classification"),
            dict,
        )
        or set(pull_request["required_capabilities_by_classification"])
        != set(classifications)
        or set().union(
            *(
                set(value)
                for value in pull_request[
                    "required_capabilities_by_classification"
                ].values()
            )
        )
        != set(document.get("coverage", {}))
        or not isinstance(push, dict)
        or push.get("event") != "push"
        or push.get("branches") != ["main"]
        or not _string_list(push.get("release_paths"))
        or not _valid_timing_contract(timing, jobs)
        or not _valid_nightly_contract(nightly)
        or not isinstance(protection, dict)
        or protection.get("api", {})
        .get("required_status_checks", {})
        .get("contexts")
        != [required.get("name")]
    ):
        raise ValueError("CI contract is missing path, job, or required-check policy")
    return document


def _valid_affected_validation(value: Any, jobs: Any, conditional_jobs: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "full_files",
        "semantic_prefixes",
        "ast_prefixes",
        "ast_files",
        "platform_prefixes",
        "platform_files",
        "python_matrix",
        "job_order",
    }:
        return False
    sequence_names = (
        "full_files",
        "semantic_prefixes",
        "ast_prefixes",
        "ast_files",
        "platform_prefixes",
        "platform_files",
        "job_order",
    )
    if value.get("schema_version") != AFFECTED_VALIDATION_PLAN_VERSION or any(
        not _sorted_unique_strings(value.get(name)) for name in sequence_names[:-1]
    ):
        return False
    if (
        not _string_list(value.get("job_order"))
        or value["job_order"] != conditional_jobs
        or not isinstance(jobs, dict)
        or any(job not in jobs for job in value["job_order"])
    ):
        return False
    matrices = value.get("python_matrix")
    if not isinstance(matrices, dict) or set(matrices) != {"base", "ast", "platform"}:
        return False
    identities: set[tuple[str, str, str]] = set()
    for records in matrices.values():
        if not isinstance(records, list):
            return False
        for record in records:
            if (
                not isinstance(record, dict)
                or set(record) != {"os", "python-version", "suite"}
                or any(not isinstance(item, str) or not item for item in record.values())
            ):
                return False
            identity = (record["os"], record["python-version"], record["suite"])
            if identity in identities:
                return False
            identities.add(identity)
    return True


def _sorted_unique_strings(value: Any) -> TypeGuard[list[str]]:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
        and value == sorted(set(value))
    )


def classify_paths(paths: Sequence[str], contract: Mapping[str, Any]) -> str:
    """Select the cheapest safe lane, failing closed to full code CI."""
    normalized = sorted({path.strip("/") for path in paths if path.strip("/")})
    if not normalized:
        return CODE_CLASSIFICATION
    if all(_path_matches(path, contract["docs_only"]) for path in normalized):
        return DOCS_CLASSIFICATION
    if all(
        _path_matches(path, contract["docs_only"])
        or _path_matches(path, contract["policy_only"])
        for path in normalized
    ):
        return POLICY_CLASSIFICATION
    if all(
        _path_matches(path, contract["docs_only"])
        or _path_matches(path, contract["policy_only"])
        or _path_matches(path, contract["automation_only"])
        for path in normalized
    ):
        return AUTOMATION_CLASSIFICATION
    return CODE_CLASSIFICATION


def plan_affected_validation(
    paths: Sequence[str],
    contract: Mapping[str, Any],
    *,
    event_name: str,
) -> dict[str, Any]:
    """Derive the exact required jobs and Python identities for one change set."""
    normalized = sorted({path.strip("/") for path in paths if path.strip("/")})
    classification = (
        classify_paths(normalized, contract)
        if event_name == "pull_request"
        else CODE_CLASSIFICATION
    )
    policy_requested = any(
        _path_matches(path, contract["policy_only"])
        or _path_matches(path, contract["automation_only"])
        for path in normalized
    )
    if event_name != "pull_request" or not normalized:
        return _build_validation_plan(
            normalized,
            contract,
            classification=CODE_CLASSIFICATION,
            mode="full",
            capabilities=_full_capabilities(policy_requested),
        )
    if classification != CODE_CLASSIFICATION:
        lane_capabilities = (
            ["policy"] if classification != DOCS_CLASSIFICATION else []
        )
        return _build_validation_plan(
            normalized,
            contract,
            classification=classification,
            mode="affected",
            capabilities=lane_capabilities,
        )

    affected = contract["affected_validation"]
    full_files = frozenset(affected["full_files"])
    semantic_prefixes = tuple(affected["semantic_prefixes"])
    ast_prefixes = tuple(affected["ast_prefixes"])
    ast_files = frozenset(affected["ast_files"])
    platform_prefixes = tuple(affected["platform_prefixes"])
    platform_files = frozenset(affected["platform_files"])
    capabilities: set[str] = {"policy"} if policy_requested else set()
    full = False
    for path in normalized:
        matched = any(
            _path_matches(path, contract[name])
            for name in ("docs_only", "policy_only", "automation_only")
        )
        if path in full_files:
            full = True
            continue
        if path.startswith("rust/"):
            capabilities.update(("python", "rust", "parity"))
            matched = True
        elif path.startswith(semantic_prefixes):
            capabilities.update(("python", "parity"))
            matched = True
        elif path.startswith("tests/"):
            capabilities.add("python")
            matched = True
        if path in ast_files or path.startswith(ast_prefixes):
            capabilities.add("ast")
            matched = True
        if path in platform_files or path.startswith(platform_prefixes):
            capabilities.add("platform")
            matched = True
        if not matched:
            full = True
    return _build_validation_plan(
        normalized,
        contract,
        classification=classification,
        mode="full" if full else "affected",
        capabilities=_full_capabilities(policy_requested) if full else sorted(capabilities),
    )


def validate_affected_plan(
    value: Any,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Recompute and authenticate a caller-supplied affected validation plan."""
    required_fields = {
        "schema_version",
        "mode",
        "classification",
        "changed_paths",
        "capabilities",
        "selected_jobs",
        "python_matrix",
        "timing_reports",
    }
    if not isinstance(value, Mapping) or set(value) != required_fields:
        raise ValueError("affected validation plan fields are invalid")
    mode = value.get("mode")
    if mode not in {"affected", "full"}:
        raise ValueError("affected validation plan mode is invalid")
    paths = value.get("changed_paths")
    if not _sorted_unique_strings(paths):
        raise ValueError("affected validation paths are not sorted and unique")
    expected = plan_affected_validation(
        paths,
        contract,
        event_name="pull_request" if mode == "affected" else "workflow_dispatch",
    )
    if dict(value) != expected:
        raise ValueError("affected validation plan does not match the trusted contract")
    return expected


def _full_capabilities(policy_requested: bool) -> list[str]:
    capabilities = {"ast", "parity", "platform", "python", "rust"}
    if policy_requested:
        capabilities.add("policy")
    return sorted(capabilities)


def _build_validation_plan(
    paths: list[str],
    contract: Mapping[str, Any],
    *,
    classification: str,
    mode: str,
    capabilities: Sequence[str],
) -> dict[str, Any]:
    capability_set = set(capabilities)
    affected = contract["affected_validation"]
    matrix = list(affected["python_matrix"]["base"]) if "python" in capability_set else []
    if "platform" in capability_set:
        matrix.extend(affected["python_matrix"]["platform"])
    if "ast" in capability_set:
        matrix.extend(affected["python_matrix"]["ast"])
    selected = set()
    if "policy" in capability_set:
        selected.add("policy")
    if "python" in capability_set:
        selected.update(("python", "python-quality"))
    if "rust" in capability_set:
        selected.add("rust-quality")
    if "parity" in capability_set:
        selected.add("parity")
    if selected - {"policy"}:
        selected.add("timing")
    selected_jobs = [job for job in affected["job_order"] if job in selected]
    matrix_keys = {
        (record["os"], record["python-version"], record["suite"]) for record in matrix
    }
    timing_reports = []
    for spec in contract["timing"]["reports"]:
        job = spec["job"]
        if (
            job == "python"
            and (spec["os"], spec["python"], spec["suite"]) in matrix_keys
        ) or (job != "python" and job in selected):
            timing_reports.append(_timing_report_id(spec))
    return {
        "schema_version": AFFECTED_VALIDATION_PLAN_VERSION,
        "mode": mode,
        "classification": classification,
        "changed_paths": paths,
        "capabilities": sorted(capability_set),
        "selected_jobs": selected_jobs,
        "python_matrix": matrix,
        "timing_reports": timing_reports,
    }


def _timing_report_id(spec: Mapping[str, Any]) -> str:
    return "-".join((spec["job"], spec["os"], spec["python"], spec["suite"]))


def required_results_pass(
    classification: str,
    *,
    validation_plan: Any,
    changes_result: str,
    documentation_result: str,
    job_results: Mapping[str, str],
    contract: Mapping[str, Any],
) -> bool:
    """Evaluate all component jobs behind the stable Required CI check."""
    try:
        plan = validate_affected_plan(validation_plan, contract)
    except ValueError:
        return False
    if (
        classification != plan["classification"]
        or changes_result != SUCCESS
        or documentation_result != SUCCESS
    ):
        return False
    conditional_jobs = contract["conditional_job_ids"]
    if set(job_results) != set(conditional_jobs):
        return False
    selected = set(plan["selected_jobs"])
    return all(
        job_results[job] == (SUCCESS if job in selected else SKIPPED)
        for job in conditional_jobs
    )


def _path_matches(path: str, policy: Mapping[str, Any]) -> bool:
    files = frozenset(policy["files"])
    prefixes = tuple(policy["prefixes"])
    return path in files or path.startswith(prefixes)


def validate_text_paths(root: str | Path, paths: Sequence[str]) -> list[str]:
    """Validate changed text and JSON without installing project dependencies."""
    repository = Path(root).resolve()
    validated: list[str] = []
    text_suffixes = {".json", ".md", ".ps1", ".sh", ".toml", ".txt", ".yaml", ".yml"}
    text_names = {"LICENSE"}
    for raw_path in sorted(set(paths)):
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"changed path escapes repository: {raw_path}")
        target = (repository / relative).resolve()
        if not target.exists():
            continue
        if not target.is_relative_to(repository) or not target.is_file():
            raise ValueError(f"changed path is not a repository file: {raw_path}")
        if target.suffix.lower() not in text_suffixes and target.name not in text_names:
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"changed text is not UTF-8: {raw_path}") from exc
        if "\0" in content:
            raise ValueError(f"changed text contains NUL: {raw_path}")
        if target.suffix.lower() == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError(f"changed JSON is invalid: {raw_path}: {exc}") from exc
        validated.append(relative.as_posix())
    return validated


def changed_paths(base: str, head: str) -> list[str]:
    """Read a NUL-delimited git diff, failing closed when either commit is absent."""
    if not base or base == ZERO_SHA or not head:
        return []
    for commit in (base, head):
        check = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if check.returncode != 0:
            return []
    completed = subprocess.run(
        ["git", "diff", "--name-only", "-z", base, head],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [
        item.decode(sys.getfilesystemencoding(), errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    ]


def build_nightly_matrix(
    root: str | Path,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Discover every configured fixture and assign it to exactly one balanced shard."""
    repository = Path(root).resolve()
    nightly = contract["nightly"]
    shard_count = int(nightly["shard_count"])
    inventory = _discover_nightly_fixtures(repository, nightly["fixture_globs"])
    shards: list[dict[str, Any]] = [
        {
            "shard_index": index,
            "shard_count": shard_count,
            "logical_bytes": 0,
            "fixtures": [],
        }
        for index in range(shard_count)
    ]
    for fixture in sorted(
        inventory,
        key=lambda item: (-item["logical_bytes"], item["manifest"]),
    ):
        selected = min(
            shards,
            key=lambda shard: (
                shard["logical_bytes"],
                len(shard["fixtures"]),
                shard["shard_index"],
            ),
        )
        selected["fixtures"].append(fixture)
        selected["logical_bytes"] += fixture["logical_bytes"]
    for shard in shards:
        shard["fixtures"].sort(key=lambda item: item["manifest"])
        shard["fixture_count"] = len(shard["fixtures"])
    ordered_inventory = sorted(inventory, key=lambda item: item["manifest"])
    return {
        "schema_version": "1.0.0",
        "fixture_count": len(ordered_inventory),
        "shard_count": shard_count,
        "verification_level": nightly["verification_level"],
        "inventory_sha256": _canonical_sha256(ordered_inventory),
        "inventory": ordered_inventory,
        "shards": shards,
        "matrix": {
            "include": [
                {
                    "shard_index": shard["shard_index"],
                    "shard_count": shard_count,
                    "fixture_count": shard["fixture_count"],
                    "logical_bytes": shard["logical_bytes"],
                }
                for shard in shards
            ]
        },
    }


def run_nightly_shard(
    root: str | Path,
    contract: Mapping[str, Any],
    *,
    shard_index: int,
    artifact_root: str | Path,
    dry_run: bool,
) -> tuple[dict[str, Any], bool]:
    """Run or plan one generated shard while retaining per-fixture outcomes."""
    repository = Path(root).resolve()
    matrix = build_nightly_matrix(repository, contract)
    if shard_index < 0 or shard_index >= matrix["shard_count"]:
        raise ValueError(f"nightly shard index is out of range: {shard_index}")
    shard = matrix["shards"][shard_index]
    artifacts = Path(artifact_root).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    results = []
    failures = []
    for position, fixture in enumerate(shard["fixtures"]):
        destination = artifacts / f"fixture-{position:03d}"
        command = _nightly_fixture_command(
            fixture["manifest"],
            destination,
            verification_level=matrix["verification_level"],
        )
        if dry_run:
            results.append(
                {
                    **fixture,
                    "status": "planned",
                    "command": command,
                    "output_directory": str(destination),
                }
            )
            continue
        try:
            completed = subprocess.run(
                command,
                cwd=repository,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
            )
        except OSError as exc:
            completed = subprocess.CompletedProcess(
                command,
                127,
                stdout="",
                stderr=str(exc),
            )
        stdout_path = artifacts / f"fixture-{position:03d}.stdout.log"
        stderr_path = artifacts / f"fixture-{position:03d}.stderr.log"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        result = {
            **fixture,
            "status": "passed" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode,
            "command": command,
            "output_directory": str(destination),
            "stdout_sha256": _sha256_file(stdout_path),
            "stderr_sha256": _sha256_file(stderr_path),
        }
        results.append(result)
        if completed.returncode != 0:
            message = _normalized_failure_message(
                completed.stderr or completed.stdout,
                repository=repository,
                artifact_root=artifacts,
                fixture=fixture,
            )
            failures.append(
                {
                    "fingerprint": _canonical_sha256(
                        {
                            "stage": "fixture-full-parity",
                            "exit_code": completed.returncode,
                            "message": message,
                        }
                    ),
                    "stage": "fixture-full-parity",
                    "fixture_id": fixture["fixture_id"],
                    "manifest": fixture["manifest"],
                    "exit_code": completed.returncode,
                    "message": message,
                }
            )
    report = {
        "schema_version": "1.0.0",
        "inventory_sha256": matrix["inventory_sha256"],
        "shard_index": shard_index,
        "shard_count": matrix["shard_count"],
        "dry_run": dry_run,
        "assignments": [item["manifest"] for item in shard["fixtures"]],
        "results": results,
        "failures": failures,
        "passed": not failures,
    }
    return report, not failures


def summarize_nightly_reports(
    matrix: Mapping[str, Any],
    reports: Sequence[Mapping[str, Any]],
    *,
    job_results: Mapping[str, str],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify one-time fixture coverage and group repeated root-cause failures."""
    expected = [item["manifest"] for item in matrix["inventory"]]
    observed = [
        str(manifest)
        for report in reports
        for manifest in report.get("assignments", [])
    ]
    observed_counts = {path: observed.count(path) for path in sorted(set(observed))}
    duplicates = sorted(path for path, count in observed_counts.items() if count > 1)
    missing = sorted(set(expected) - set(observed))
    unexpected = sorted(set(observed) - set(expected))
    grouped: dict[str, dict[str, Any]] = {}
    for report in reports:
        for failure in report.get("failures", []):
            fingerprint = str(failure["fingerprint"])
            group = grouped.setdefault(
                fingerprint,
                {
                    "fingerprint": fingerprint,
                    "stage": failure.get("stage"),
                    "exit_code": failure.get("exit_code"),
                    "message": failure.get("message"),
                    "occurrences": 0,
                    "fixture_ids": [],
                    "manifests": [],
                    "shards": [],
                },
            )
            group["occurrences"] += 1
            if failure.get("fixture_id") is not None:
                group["fixture_ids"].append(str(failure["fixture_id"]))
            if failure.get("manifest") is not None:
                group["manifests"].append(str(failure["manifest"]))
            group["shards"].append(report.get("shard_index"))
    detailed_fixture_failures = bool(grouped)
    for job, result in sorted(job_results.items()):
        if result == SUCCESS or (job == "fixtures" and detailed_fixture_failures):
            continue
        fingerprint = _canonical_sha256(
            {"stage": "nightly-job", "job": job, "result": result}
        )
        grouped[fingerprint] = {
            "fingerprint": fingerprint,
            "stage": "nightly-job",
            "job": job,
            "result": result,
            "message": f"nightly job {job} concluded {result}",
            "occurrences": 1,
            "fixture_ids": [],
            "manifests": [],
            "shards": [],
        }
    expected_jobs = set(contract["nightly"]["job_ids"])
    job_contract_valid = set(job_results) == expected_jobs
    if not job_contract_valid:
        fingerprint = _canonical_sha256(
            {
                "stage": "nightly-job-contract",
                "expected": sorted(expected_jobs),
                "observed": sorted(job_results),
            }
        )
        grouped[fingerprint] = {
            "fingerprint": fingerprint,
            "stage": "nightly-job-contract",
            "message": "nightly aggregate job results differ from the contract",
            "occurrences": 1,
            "fixture_ids": [],
            "manifests": [],
            "shards": [],
        }
    all_jobs_passed = job_contract_valid and all(
        job_results[job] == SUCCESS for job in expected_jobs
    )
    failures = []
    for group in grouped.values():
        group["fixture_ids"] = sorted(set(group["fixture_ids"]))
        group["manifests"] = sorted(set(group["manifests"]))
        group["shards"] = sorted(
            {int(value) for value in group["shards"] if isinstance(value, int)}
        )
        failures.append(group)
    failures.sort(key=lambda item: item["fingerprint"])
    passed = (
        not duplicates
        and not missing
        and not unexpected
        and all_jobs_passed
        and not failures
    )
    return {
        "schema_version": "1.0.0",
        "passed": passed,
        "dry_run": bool(reports) and all(report.get("dry_run") is True for report in reports),
        "inventory_sha256": matrix["inventory_sha256"],
        "expected_fixture_count": len(expected),
        "observed_fixture_count": len(observed),
        "unique_observed_fixture_count": len(set(observed)),
        "duplicates": duplicates,
        "missing": missing,
        "unexpected": unexpected,
        "job_results": dict(sorted(job_results.items())),
        "job_contract_valid": job_contract_valid,
        "failure_occurrence_count": sum(
            failure["occurrences"] for failure in failures
        ),
        "unique_failure_count": len(failures),
        "failures": failures,
    }


def _discover_nightly_fixtures(
    root: Path,
    patterns: Sequence[str],
) -> list[dict[str, Any]]:
    records = []
    fixture_ids: set[str] = set()
    paths = sorted(
        {
            path.resolve()
            for pattern in patterns
            for path in root.glob(pattern)
            if path.is_file()
        }
    )
    if not paths:
        raise ValueError("nightly fixture inventory is empty")
    for manifest in paths:
        if not manifest.is_relative_to(root):
            raise ValueError(f"nightly fixture escaped the repository: {manifest}")
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid nightly fixture manifest: {manifest}") from exc
        fixture_id = document.get("fixture_id") if isinstance(document, dict) else None
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in fixture_ids:
            raise ValueError(f"nightly fixture_id is missing or duplicated: {manifest}")
        fixture_ids.add(fixture_id)
        logical_bytes = sum(
            path.stat().st_size
            for path in manifest.parent.rglob("*")
            if path.is_file()
        )
        records.append(
            {
                "fixture_id": fixture_id,
                "manifest": manifest.relative_to(root).as_posix(),
                "logical_bytes": logical_bytes,
            }
        )
    return records


def _nightly_fixture_command(
    manifest: str,
    destination: Path,
    *,
    verification_level: str,
) -> list[str]:
    executable = Path(sys.executable).with_name(
        "nfi-bte.exe" if os.name == "nt" else "nfi-bte"
    )
    return [
        str(executable),
        "engine",
        "fixture",
        manifest,
        "--output-dir",
        str(destination),
        "--level",
        verification_level,
    ]


def _normalized_failure_message(
    output: str,
    *,
    repository: Path,
    artifact_root: Path,
    fixture: Mapping[str, Any],
) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    message = "\n".join(lines[-8:]) if lines else "command failed without output"
    for value in (
        str(repository),
        str(artifact_root),
        str(fixture["manifest"]),
        str(fixture["fixture_id"]),
    ):
        message = message.replace(value, "<dynamic>")
    return re.sub(r"\b[0-9a-f]{40,64}\b", "<identity>", message)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: str | Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_github_output(path: str | Path, values: Mapping[str, str]) -> None:
    with Path(path).open("a", encoding="utf-8") as handle:
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"GitHub output contains a newline: {name}")
            handle.write(f"{name}={value}\n")


def _parse_job_results(values: Sequence[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for value in values:
        job, separator, result = value.partition("=")
        if not separator or not job or not result or job in results:
            raise ValueError(f"invalid job result: {value!r}")
        results[job] = result
    return results


def _string_list(value: Any) -> TypeGuard[list[str]]:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and bool(item) for item in value)
    )


def _valid_timing_contract(value: Any, jobs: Mapping[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False
    categories = value.get("step_categories")
    reports = value.get("reports")
    if (
        value.get("schema_version") != "1.0.0"
        or not isinstance(value.get("baseline_id"), str)
        or value.get("workflow_name") != "CI"
        or value.get("workflow_ref") != ".github/workflows/ci.yml"
        or not isinstance(value.get("repository"), str)
        or value.get("cache_file") != "uv.lock"
        or value.get("rust_compiler_cache") != "sccache-gha-v0.10.0"
        or not isinstance(value.get("artifact_retention_days"), int)
        or value["artifact_retention_days"] <= 0
        or value.get("comparison_run_count") != 3
        or not isinstance(value.get("pytest_slowest_count"), int)
        or value["pytest_slowest_count"] <= 0
        or not _string_list(value.get("pytest_ownership_groups"))
        or len(set(value["pytest_ownership_groups"]))
        != len(value["pytest_ownership_groups"])
        or not isinstance(categories, dict)
        or not categories
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(category, str)
            or not category
            for name, category in categories.items()
        )
        or not isinstance(reports, list)
        or not reports
    ):
        return False
    identities: set[tuple[str, str, str, str]] = set()
    covered_steps: set[str] = set()
    for report in reports:
        if not isinstance(report, dict):
            return False
        job = report.get("job")
        os_name = report.get("os")
        python_version = report.get("python")
        suite = report.get("suite")
        steps = report.get("required_steps")
        timeout = report.get("step_timeout_seconds")
        if (
            not isinstance(job, str)
            or not job
            or not isinstance(os_name, str)
            or not os_name
            or not isinstance(python_version, str)
            or not python_version
            or not isinstance(suite, str)
            or not suite
            or job not in jobs
            or not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or timeout <= 0
            or timeout > jobs[job]["timeout_minutes"] * 60
            or not _string_list(steps)
            or len(set(steps)) != len(steps)
            or any(step not in categories for step in steps)
        ):
            return False
        identity = (job, os_name, python_version, suite)
        if identity in identities:
            return False
        identities.add(identity)
        covered_steps.update(steps)
    return covered_steps == set(categories)


def _valid_nightly_contract(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    concurrency = value.get("concurrency")
    smoke = value.get("official_reference_smoke")
    patterns = value.get("fixture_globs")
    jobs = value.get("job_ids")
    return (
        isinstance(value.get("workflow"), str)
        and value.get("events") == ["schedule", "workflow_dispatch"]
        and value.get("permissions") == {"contents": "read"}
        and _string_list(patterns)
        and all(
            not Path(pattern).is_absolute() and ".." not in Path(pattern).parts
            for pattern in patterns
        )
        and value.get("verification_level") == "full"
        and isinstance(value.get("shard_count"), int)
        and not isinstance(value.get("shard_count"), bool)
        and value["shard_count"] > 0
        and _string_list(jobs)
        and len(set(jobs)) == len(jobs)
        and isinstance(value.get("failure_report"), str)
        and isinstance(value.get("artifact_retention_days"), int)
        and value["artifact_retention_days"] > 0
        and isinstance(concurrency, dict)
        and isinstance(concurrency.get("group"), str)
        and concurrency.get("cancel_in_progress") is True
        and isinstance(smoke, dict)
        and isinstance(smoke.get("manifest"), str)
        and smoke.get("trace") in {"off", "hash", "full"}
        and smoke.get("network") == "none"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path(".github/ci-contract.json"),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    classify = commands.add_parser("classify")
    classify.add_argument("--event-name", required=True)
    classify.add_argument("--base", default="")
    classify.add_argument("--head", default="")
    classify.add_argument("--path", action="append", default=[])
    classify.add_argument("--github-output", type=Path)
    verify = commands.add_parser("verify-results")
    verify.add_argument("--classification", required=True)
    verify.add_argument("--validation-plan-json", required=True)
    verify.add_argument("--changes-result", required=True)
    verify.add_argument("--documentation-result", required=True)
    verify.add_argument("--job-result", action="append", default=[])
    matrix = commands.add_parser("nightly-matrix")
    matrix.add_argument("--root", type=Path, default=Path("."))
    matrix.add_argument("--output", type=Path, required=True)
    matrix.add_argument("--github-output", type=Path)
    shard = commands.add_parser("run-nightly-shard")
    shard.add_argument("--root", type=Path, default=Path("."))
    shard.add_argument("--shard-index", type=int, required=True)
    shard.add_argument("--artifact-root", type=Path, required=True)
    shard.add_argument("--output", type=Path, required=True)
    shard.add_argument("--dry-run", action="store_true")
    summarize = commands.add_parser("summarize-nightly")
    summarize.add_argument("--root", type=Path, default=Path("."))
    summarize.add_argument("--reports", type=Path, required=True)
    summarize.add_argument("--job-result", action="append", default=[])
    summarize.add_argument("--output", type=Path, required=True)
    validate_text = commands.add_parser("validate-text")
    validate_text.add_argument("--root", type=Path, default=Path("."))
    validate_text.add_argument("--paths-json", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    contract = load_contract(args.contract)
    if args.command == "classify":
        paths = (
            list(args.path)
            if args.path
            else changed_paths(args.base, args.head)
        )
        plan = plan_affected_validation(paths, contract, event_name=args.event_name)
        classification = plan["classification"]
        selected_jobs = plan["selected_jobs"]
        result = {
            "classification": classification,
            "automation_changes": str(
                classification == AUTOMATION_CLASSIFICATION
            ).lower(),
            "policy_changes": str("policy" in selected_jobs).lower(),
            "code_changes": str(bool(set(selected_jobs) - {"policy"})).lower(),
            "changed_paths_json": json.dumps(
                plan["changed_paths"],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "selected_jobs_json": json.dumps(
                selected_jobs,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "python_matrix_json": json.dumps(
                {"include": plan["python_matrix"]},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "validation_plan_json": json.dumps(
                plan,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        if args.github_output is not None:
            _write_github_output(args.github_output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "verify-results":
        passed = required_results_pass(
            args.classification,
            validation_plan=json.loads(args.validation_plan_json),
            changes_result=args.changes_result,
            documentation_result=args.documentation_result,
            job_results=_parse_job_results(args.job_result),
            contract=contract,
        )
        print("Required CI passed" if passed else "Required CI failed")
        return 0 if passed else 1
    if args.command == "validate-text":
        paths = json.loads(args.paths_json)
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError("--paths-json must be a JSON array of strings")
        validated = validate_text_paths(args.root, paths)
        print(
            json.dumps(
                {"validated_text_paths": validated},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "nightly-matrix":
        matrix = build_nightly_matrix(args.root, contract)
        _write_json(args.output, matrix)
        if args.github_output is not None:
            _write_github_output(
                args.github_output,
                {
                    "matrix": json.dumps(
                        matrix["matrix"],
                        separators=(",", ":"),
                    ),
                    "inventory_sha256": matrix["inventory_sha256"],
                    "fixture_count": str(matrix["fixture_count"]),
                },
            )
        print(
            json.dumps(
                {
                    "fixture_count": matrix["fixture_count"],
                    "inventory_sha256": matrix["inventory_sha256"],
                    "shard_count": matrix["shard_count"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "run-nightly-shard":
        report, passed = run_nightly_shard(
            args.root,
            contract,
            shard_index=args.shard_index,
            artifact_root=args.artifact_root,
            dry_run=args.dry_run,
        )
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "dry_run": report["dry_run"],
                    "fixture_count": len(report["assignments"]),
                    "passed": passed,
                    "shard_index": report["shard_index"],
                },
                sort_keys=True,
            )
        )
        return 0 if passed else 1
    if args.command == "summarize-nightly":
        matrix = build_nightly_matrix(args.root, contract)
        reports = []
        for path in sorted(args.reports.glob("shard-*.json")):
            report = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise ValueError(f"nightly shard report is not an object: {path}")
            reports.append(report)
        summary = summarize_nightly_reports(
            matrix,
            reports,
            job_results=_parse_job_results(args.job_result),
            contract=contract,
        )
        _write_json(args.output, summary)
        print(
            json.dumps(
                {
                    "passed": summary["passed"],
                    "unique_failure_count": summary["unique_failure_count"],
                    "unique_fixture_count": summary["unique_observed_fixture_count"],
                },
                sort_keys=True,
            )
        )
        return 0 if summary["passed"] else 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
