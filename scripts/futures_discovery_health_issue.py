#!/usr/bin/env python3
"""Deduplicate infrastructure failures in the Futures discovery workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence

_MARKER = re.compile(r"<!-- nfi-futures-discovery-health:([0-9a-f]{64}) -->")


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
    existing = {
        match.group(1): issue
        for issue in open_issues
        if isinstance(issue.get("body"), str)
        if (match := _MARKER.search(str(issue["body"]))) is not None
    }
    keep = existing.get(fingerprint, {}).get("number") if fingerprint else None
    close = [
        int(issue["number"])
        for issue in open_issues
        if issue.get("number") != keep
    ]
    create = None
    if fingerprint and keep is None:
        details = "\n".join(
            f"- `{name}`: `{conclusion}`"
            for name, conclusion in failures.items()
        )
        create = {
            "title": "Futures discovery automation health failure",
            "body": (
                f"<!-- nfi-futures-discovery-health:{fingerprint} -->\n\n"
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
    issues = json.loads(
        _gh(
            "issue",
            "list",
            "--repo",
            args.repository,
            "--label",
            "nfi-futures-discovery-health",
            "--state",
            "open",
            "--json",
            "number,title,body",
        )
    )
    plan = build_health_plan(stages, issues, run_url=args.run_url)
    create = plan["create"]
    if isinstance(create, Mapping):
        _gh(
            "issue",
            "create",
            "--repo",
            args.repository,
            "--label",
            "nfi-futures-discovery-health",
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
            "Futures discovery automation recovered.",
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


if __name__ == "__main__":
    raise SystemExit(main())
