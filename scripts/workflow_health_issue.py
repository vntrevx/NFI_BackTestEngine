#!/usr/bin/env python3
"""Maintain one deduplicated issue for compatibility automation failures."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

_MARKER = re.compile(r"<!-- nfi-automation-health-fingerprint:([0-9a-f]{64}) -->")


def build_health_issue_plan(
    stages: Mapping[str, str],
    open_issues: Sequence[Mapping[str, Any]],
    *,
    changed: bool,
    run_url: str,
) -> dict[str, Any]:
    expected = {"discover"} if not changed else set(stages)
    failures = {
        stage: result
        for stage, result in sorted(stages.items())
        if stage in expected and result != "success"
    }
    fingerprint = _canonical_sha256(failures) if failures else None
    existing = {
        match.group(1): issue
        for issue in open_issues
        if isinstance(issue.get("body"), str)
        if (match := _MARKER.search(str(issue["body"]))) is not None
    }
    keep = (
        existing.get(fingerprint, {}).get("number")
        if fingerprint is not None
        else None
    )
    close = [
        int(issue["number"])
        for issue in open_issues
        if issue.get("number") != keep
    ]
    create = None
    if fingerprint is not None and keep is None:
        details = "\n".join(
            f"- `{stage}`: `{result}`"
            for stage, result in failures.items()
        )
        create = {
            "title": "Latest NFI compatibility automation health failure",
            "body": (
                f"<!-- nfi-automation-health-fingerprint:{fingerprint} -->\n\n"
                f"{details}\n\nRun: {run_url}\n\n"
                "This issue is reconciled automatically after a healthy run."
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
    parser.add_argument("--changed", choices=("true", "false"), required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--stage", action="append", default=[])
    args = parser.parse_args()
    stages = _stages(args.stage)
    issues = json.loads(
        _gh(
            "issue",
            "list",
            "--repo",
            args.repository,
            "--label",
            "nfi-automation-health",
            "--state",
            "open",
            "--json",
            "number,title,body",
        )
    )
    plan = build_health_issue_plan(
        stages,
        issues,
        changed=args.changed == "true",
        run_url=args.run_url,
    )
    create = plan["create"]
    if isinstance(create, Mapping):
        _gh(
            "issue",
            "create",
            "--repo",
            args.repository,
            "--label",
            "nfi-automation-health",
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
            (
                "Compatibility automation recovered."
                if plan["healthy"]
                else "Superseded by a different automation failure."
            ),
        )
    return 0


def _stages(values: list[str]) -> dict[str, str]:
    stages: dict[str, str] = {}
    for value in values:
        name, separator, result = value.partition("=")
        if (
            separator != "="
            or not name
            or result not in {"success", "failure", "cancelled", "skipped"}
        ):
            raise ValueError(f"invalid stage result: {value}")
        stages[name] = result
    return stages


def _gh(*arguments: str) -> str:
    completed = subprocess.run(
        ["gh", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
