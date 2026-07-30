#!/usr/bin/env python3
"""Reconcile deduplicated terminal Spot and Futures discovery gaps."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_CURRENT_MARKER = re.compile(
    r"<!-- nfi-branch-discovery:(spot|futures):([0-9a-f]{64}) -->"
)
_LEGACY_MARKER = re.compile(r"<!-- nfi-futures-discovery:([0-9a-f]{64}) -->")
_ISSUE_STATES = {"coverage_exhausted", "unsupported_semantics"}


def build_issue_plan(
    report: Mapping[str, Any],
    open_issues: Sequence[Mapping[str, Any]],
    *,
    run_url: str,
) -> dict[str, Any]:
    """Create only terminal semantic gaps; budget resumes and candidates stay quiet."""
    status = report.get("status")
    trading_mode = report.get("trading_mode")
    if trading_mode not in {"spot", "futures"}:
        raise ValueError("discovery report trading mode is invalid")
    fingerprint = report.get("fingerprint") if status in _ISSUE_STATES else None
    if fingerprint is not None and re.fullmatch(r"[0-9a-f]{64}", str(fingerprint)) is None:
        raise ValueError("discovery report fingerprint is invalid")
    parsed = [
        (*identity, issue)
        for issue in open_issues
        if (identity := _issue_identity(issue)) is not None
    ]
    candidates = [
        (legacy, int(issue["number"]))
        for mode, issue_fingerprint, legacy, issue in parsed
        if mode == trading_mode and issue_fingerprint == fingerprint
    ] if fingerprint else []
    keep = min(candidates)[1] if candidates else None
    close = sorted([
        int(issue["number"])
        for mode, _issue_fingerprint, _legacy, issue in parsed
        if mode == trading_mode
        if issue.get("number") != keep
    ]) if status != "infrastructure_failed" else []
    create = None
    if fingerprint and keep is None:
        last_message = str(report.get("message", "")).strip()
        create = {
            "title": (
                f"{str(trading_mode).title()} branch discovery gap "
                f"({str(report.get('upstream_commit', ''))[:12]})"
            ),
            "body": (
                f"<!-- nfi-branch-discovery:{trading_mode}:{fingerprint} -->\n\n"
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
        "trading_mode": trading_mode,
        "recovered": status not in _ISSUE_STATES
        and status != "infrastructure_failed",
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
    issues_by_number: dict[int, dict[str, Any]] = {}
    for label in ("nfi-branch-discovery", "nfi-futures-discovery"):
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
            raise ValueError("GitHub returned an invalid discovery issue list")
        for issue in records:
            if isinstance(issue, dict) and isinstance(issue.get("number"), int):
                issues_by_number[int(issue["number"])] = issue
    issues = list(issues_by_number.values())
    plan = build_issue_plan(report, issues, run_url=args.run_url)
    create = plan["create"]
    if isinstance(create, Mapping):
        _gh(
            "issue",
            "create",
            "--repo",
            args.repository,
            "--label",
            "nfi-branch-discovery",
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
                f"{str(plan['trading_mode']).title()} discovery recovered or "
                "advanced to a non-terminal state "
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


def _issue_identity(
    issue: Mapping[str, Any],
) -> tuple[str, str, bool] | None:
    body = issue.get("body")
    if not isinstance(body, str):
        return None
    current = _CURRENT_MARKER.search(body)
    if current is not None:
        return current.group(1), current.group(2), False
    legacy = _LEGACY_MARKER.search(body)
    if legacy is not None:
        return "futures", legacy.group(1), True
    return None


if __name__ == "__main__":
    raise SystemExit(main())
