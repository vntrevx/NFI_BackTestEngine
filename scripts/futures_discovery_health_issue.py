#!/usr/bin/env python3
"""Deduplicate infrastructure failures in the dual-mode discovery workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence

_CURRENT_MARKER = re.compile(
    r"<!-- nfi-branch-discovery-health:([0-9a-f]{64}) -->"
)
_LEGACY_MARKER = re.compile(
    r"<!-- nfi-futures-discovery-health:([0-9a-f]{64}) -->"
)


def build_health_plan(
    stages: Mapping[str, str],
    open_issues: Sequence[Mapping[str, object]],
    *,
    run_url: str,
) -> dict[str, object]:
    failures = {
        name: conclusion
        for name, conclusion in sorted(stages.items())
        if conclusion not in {"success", "skipped"}
    }
    fingerprint = _canonical_sha256(failures) if failures else None
    parsed = [
        (*identity, issue)
        for issue in open_issues
        if (identity := _health_identity(issue)) is not None
    ]
    candidates = [
        (legacy, int(issue["number"]))
        for issue_fingerprint, legacy, issue in parsed
        if issue_fingerprint == fingerprint
    ] if fingerprint else []
    keep = min(candidates)[1] if candidates else None
    close = sorted([
        int(issue["number"])
        for _issue_fingerprint, _legacy, issue in parsed
        if issue.get("number") != keep
    ])
    create = None
    if fingerprint and keep is None:
        details = "\n".join(
            f"- `{name}`: `{conclusion}`"
            for name, conclusion in failures.items()
        )
        create = {
            "title": "NFI branch discovery automation health failure",
            "body": (
                f"<!-- nfi-branch-discovery-health:{fingerprint} -->\n\n"
                f"{details}\n\nWorkflow: {run_url}\n\n"
                "The discovery cursor was not advanced. A later run will retry."
            ),
        }
    return {
        "fingerprint": fingerprint,
        "create": create,
        "close": close,
        "healthy": not failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--stage", action="append", required=True)
    args = parser.parse_args()
    stages = {}
    for value in args.stage:
        name, separator, conclusion = value.partition("=")
        if not separator or not name or not conclusion:
            raise ValueError("--stage must use NAME=CONCLUSION")
        stages[name] = conclusion
    issues_by_number: dict[int, dict[str, object]] = {}
    for label in (
        "nfi-branch-discovery-health",
        "nfi-futures-discovery-health",
    ):
        records = json.loads(
            _gh(
                "issue",
                "list",
                "--repo",
                args.repository,
                "--label",
                label,
                "--state",
                "open",
                "--json",
                "number,title,body",
            )
        )
        if not isinstance(records, list):
            raise ValueError("GitHub returned an invalid discovery health issue list")
        for issue in records:
            if isinstance(issue, dict) and isinstance(issue.get("number"), int):
                issues_by_number[int(issue["number"])] = issue
    issues = list(issues_by_number.values())
    plan = build_health_plan(stages, issues, run_url=args.run_url)
    create = plan["create"]
    if isinstance(create, Mapping):
        _gh(
            "issue",
            "create",
            "--repo",
            args.repository,
            "--label",
            "nfi-branch-discovery-health",
            "--title",
            str(create["title"]),
            "--body",
            str(create["body"]),
        )
    for number in plan["close"]:
        _gh(
            "issue",
            "close",
            str(number),
            "--repo",
            args.repository,
            "--comment",
            "NFI branch discovery automation recovered.",
        )
    return 0


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _gh(*arguments: str) -> str:
    return subprocess.run(
        ["gh", *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _health_identity(
    issue: Mapping[str, object],
) -> tuple[str, bool] | None:
    body = issue.get("body")
    if not isinstance(body, str):
        return None
    current = _CURRENT_MARKER.search(body)
    if current is not None:
        return current.group(1), False
    legacy = _LEGACY_MARKER.search(body)
    if legacy is not None:
        return legacy.group(1), True
    return None


if __name__ == "__main__":
    raise SystemExit(main())
