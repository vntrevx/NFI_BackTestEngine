#!/usr/bin/env python3
"""Create one issue per compatibility blocker fingerprint and close on recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_MARKER = re.compile(r"<!-- nfi-compatibility-fingerprint:([0-9a-f]{64}) -->")


def build_issue_plan(
    reports: Mapping[str, Mapping[str, Any]],
    open_issues: Sequence[Mapping[str, Any]],
    *,
    upstream_sha: str,
) -> dict[str, Any]:
    failures = {
        mode: list(report.get("blockers", []))
        for mode, report in sorted(reports.items())
        if report.get("native_compatible") is not True
    }
    fingerprint = (
        _canonical_sha256(failures) if failures else None
    )
    existing_by_fingerprint = {
        match.group(1): issue
        for issue in open_issues
        if isinstance(issue.get("body"), str)
        if (match := _MARKER.search(str(issue["body"]))) is not None
    }
    keep_number = (
        existing_by_fingerprint.get(fingerprint, {}).get("number")
        if fingerprint is not None
        else None
    )
    close = [
        int(issue["number"])
        for issue in open_issues
        if issue.get("number") != keep_number
    ]
    create = None
    if fingerprint is not None and keep_number is None:
        sections = []
        for mode, blockers in failures.items():
            descriptions = "\n".join(
                f"- `{item.get('code', 'UNKNOWN')}`: {item.get('message', '')}"
                for item in blockers
                if isinstance(item, Mapping)
            ) or "- compatibility report did not contain a structured blocker"
            sections.append(f"### {mode}\n\n{descriptions}")
        create = {
            "title": f"Latest NFI compatibility blocker ({upstream_sha[:12]})",
            "body": (
                f"{_MARKER_TEXT(fingerprint)}\n\n"
                f"Upstream commit: `{upstream_sha}`\n\n"
                + "\n\n".join(sections)
                + "\n\nThis issue is reconciled automatically when the blocker recovers."
            ),
        }
    return {
        "fingerprint": fingerprint,
        "create": create,
        "close": close,
        "recovered": not failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--upstream-sha", required=True)
    args = parser.parse_args()
    reports = {
        mode: _read_object(args.reports / f"report-{mode}.json")
        for mode in ("spot", "futures")
    }
    issues = json.loads(
        _gh(
            "issue",
            "list",
            "--repo",
            args.repository,
            "--label",
            "nfi-compatibility",
            "--state",
            "open",
            "--json",
            "number,title,body",
        )
    )
    plan = build_issue_plan(
        reports,
        issues,
        upstream_sha=args.upstream_sha,
    )
    create = plan["create"]
    if isinstance(create, Mapping):
        _gh(
            "issue",
            "create",
            "--repo",
            args.repository,
            "--label",
            "nfi-compatibility",
            "--title",
            str(create["title"]),
            "--body",
            str(create["body"]),
        )
    for number in plan["close"]:
        comment = (
            f"Compatibility recovered at `{args.upstream_sha}`."
            if plan["recovered"]
            else f"Superseded by a new blocker fingerprint at `{args.upstream_sha}`."
        )
        _gh(
            "issue",
            "close",
            str(number),
            "--repo",
            args.repository,
            "--comment",
            comment,
        )
    return 0


def _MARKER_TEXT(fingerprint: str) -> str:
    return f"<!-- nfi-compatibility-fingerprint:{fingerprint} -->"


def _gh(*arguments: str) -> str:
    completed = subprocess.run(
        ["gh", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"compatibility report is not an object: {path}")
    return value


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
