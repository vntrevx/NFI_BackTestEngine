#!/usr/bin/env python3
"""Classify CI paths and enforce the stable aggregate check contract."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CODE_CLASSIFICATION = "code"
DOCS_CLASSIFICATION = "docs-only"
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
    if not isinstance(document, dict) or document.get("schema_version") != "1.0.0":
        raise ValueError("CI contract schema_version must be 1.0.0")
    docs = document.get("docs_only")
    jobs = document.get("jobs")
    code_jobs = document.get("code_job_ids")
    required = document.get("required_check")
    concurrency = document.get("concurrency")
    protection = document.get("branch_protection")
    if (
        not isinstance(docs, dict)
        or not _string_list(docs.get("prefixes"))
        or not _string_list(docs.get("files"))
        or not isinstance(jobs, dict)
        or not _string_list(code_jobs)
        or not isinstance(required, dict)
        or not isinstance(required.get("name"), str)
        or required.get("job_id") not in jobs
        or any(job not in jobs for job in code_jobs)
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
        or not isinstance(protection, dict)
        or protection.get("api", {})
        .get("required_status_checks", {})
        .get("contexts")
        != [required.get("name")]
    ):
        raise ValueError("CI contract is missing path, job, or required-check policy")
    return document


def classify_paths(paths: Sequence[str], contract: Mapping[str, Any]) -> str:
    """Return docs-only only when every changed path is explicitly allowed."""
    normalized = sorted({path.strip("/") for path in paths if path.strip("/")})
    if not normalized:
        return CODE_CLASSIFICATION
    docs = contract["docs_only"]
    files = frozenset(docs["files"])
    prefixes = tuple(docs["prefixes"])
    if all(path in files or path.startswith(prefixes) for path in normalized):
        return DOCS_CLASSIFICATION
    return CODE_CLASSIFICATION


def required_results_pass(
    classification: str,
    *,
    changes_result: str,
    documentation_result: str,
    job_results: Mapping[str, str],
    contract: Mapping[str, Any],
) -> bool:
    """Evaluate all component jobs behind the stable Required CI check."""
    if (
        classification not in {CODE_CLASSIFICATION, DOCS_CLASSIFICATION}
        or changes_result != SUCCESS
        or documentation_result != SUCCESS
    ):
        return False
    code_jobs = contract["code_job_ids"]
    if set(job_results) != set(code_jobs):
        return False
    expected = SUCCESS if classification == CODE_CLASSIFICATION else SKIPPED
    return all(job_results[job] == expected for job in code_jobs)


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


def _string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and bool(item) for item in value)
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
    verify.add_argument("--changes-result", required=True)
    verify.add_argument("--documentation-result", required=True)
    verify.add_argument("--job-result", action="append", default=[])
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
        classification = (
            CODE_CLASSIFICATION
            if args.event_name == "workflow_dispatch"
            else classify_paths(paths, contract)
        )
        result = {
            "classification": classification,
            "code_changes": str(classification == CODE_CLASSIFICATION).lower(),
            "changed_paths_json": json.dumps(
                sorted(paths),
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
            changes_result=args.changes_result,
            documentation_result=args.documentation_result,
            job_results=_parse_job_results(args.job_result),
            contract=contract,
        )
        print("Required CI passed" if passed else "Required CI failed")
        return 0 if passed else 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
