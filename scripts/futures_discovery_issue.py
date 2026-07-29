#!/usr/bin/env python3
"""Reconcile one deduplicated Issue for terminal Futures discovery gaps."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_MARKER = re.compile(r"<!-- nfi-futures-discovery:([0-9a-f]{64}) -->")
_ISSUE_STATES = {"coverage_exhausted", "unsupported_semantics"}


def build_issue_plan(
    report: Mapping[str, Any],
    open_issues: Sequence[Mapping[str, Any]],
    *,
    run_url: str,
) -> dict[str, Any]:
    """Create only terminal semantic gaps; budget resumes and candidates stay quiet."""
    status = report.get("status")
    fingerprint = report.get("fingerprint") if status in _ISSUE_STATES else None
    if fingerprint is not None and re.fullmatch(r"[0-9a-f]{64}", str(fingerprint)) is None:
        raise ValueError("discovery report fingerprint is invalid")
    existing = {
        match.group(1): issue
        for issue in open_issues
        if isinstance(issue.get("body"), str)
        if (match := _MARKER.search(str(issue["body"]))) is not None
    }
    keep = existing.get(str(fingerprint), {}).get("number") if fingerprint else None
    close = [
        int(issue["number"])
        for issue in open_issues
        if issue.get("number") != keep
    ]
    create = None
    if fingerprint and keep is None:
        last_message = str(report.get("message", "")).strip()
        create = {
            "title": (
                "Futures branch discovery gap "
                f"({str(report.get('upstream_commit', ''))[:12]})"
            ),
            "body": (
                f"<!-- nfi-futures-discovery:{fingerprint} -->\n\n"
                f"Status: `{status}`\n\n"
                f"Upstream: `{report.get('upstream_commit')}`\n\n"
                f"Engine: `{report.get('engine_commit')}`\n\n"
                f"Targets: `{report.get('target_count')}`\n\n"
                f"Searched shards: `{report.get('searched_shard_count')}/"
                f"{report.get('shard_count')}`\n\n"
                f"Last observation: {last_message or 'none'}\n\n"
                f"Workflow: {run_url}\n\n"
                "Official Freqtrade fallback remains available. This issue does not "
                "claim Native support."
            ),
        }
    return {
        "fingerprint": fingerprint,
        "create": create,
        "close": close,
        "recovered": status not in _ISSUE_STATES,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("discovery report must be an object")
    issues = json.loads(
        _gh(
            "issue",
            "list",
            "--repo",
            args.repository,
            "--label",
            "nfi-futures-discovery",
            "--state",
            "open",
            "--json",
            "number,title,body",
        )
    )
    plan = build_issue_plan(report, issues, run_url=args.run_url)
    create = plan["create"]
    if isinstance(create, Mapping):
        _gh(
            "issue",
            "create",
            "--repo",
            args.repository,
            "--label",
            "nfi-futures-discovery",
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
                "Futures discovery recovered or advanced to a non-terminal state "
                f"at `{report.get('upstream_commit')}`."
            ),
        )
    return 0


def _gh(*arguments: str) -> str:
    return subprocess.run(
        ["gh", *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


if __name__ == "__main__":
    raise SystemExit(main())
