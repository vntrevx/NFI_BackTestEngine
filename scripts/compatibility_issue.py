#!/usr/bin/env python3
"""Maintain one deduplicated NFI compatibility review issue."""

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
    targeted_reports: Mapping[str, Mapping[str, Any]] | None = None,
    identity: Mapping[str, Any] | None = None,
    decisions: Mapping[str, Mapping[str, Any]] | None = None,
    run_url: str | None = None,
) -> dict[str, Any]:
    failures = {
        mode: _mode_blockers(
            report,
            (
                targeted_reports.get(mode)
                if targeted_reports is not None
                else None
            ),
        )
        for mode, report in sorted(reports.items())
    }
    failures = {
        mode: blockers
        for mode, blockers in failures.items()
        if blockers
    }
    fingerprint = _canonical_sha256(failures) if failures else None
    issues = sorted(open_issues, key=lambda issue: int(issue["number"]))
    desired: dict[str, str] | None = None
    if fingerprint is not None:
        sections = []
        for mode, blockers in failures.items():
            descriptions = "\n".join(
                f"- `{item.get('code', 'UNKNOWN')}`: {item.get('message', '')}"
                for item in blockers
                if isinstance(item, Mapping)
            ) or "- compatibility report did not contain a structured blocker"
            decision = decisions.get(mode) if decisions is not None else None
            targeted = targeted_reports.get(mode) if targeted_reports is not None else None
            plan = targeted.get("plan") if isinstance(targeted, Mapping) else None
            missing_targets = (
                plan.get("missing_targets") if isinstance(plan, Mapping) else None
            )
            context = [
                f"- Automation route: `{decision.get('automation_route')}`"
                if isinstance(decision, Mapping)
                else "- Automation route: unavailable",
                f"- Review kind: `{decision.get('review_kind')}`"
                if isinstance(decision, Mapping)
                else "- Review kind: unavailable",
                (
                    f"- Missing behavior targets: `{len(missing_targets)}`"
                    if isinstance(missing_targets, list)
                    else "- Missing behavior targets: unavailable"
                ),
            ]
            sections.append(
                f"### {mode}\n\n"
                + "\n".join(context)
                + "\n\n"
                + descriptions
            )
        identity_lines = []
        if identity is not None:
            for label, field in (
                ("Engine commit", "engine_sha"),
                ("Freqtrade digest", "freqtrade_digest"),
                ("Semantic profile", "semantic_profile_sha256"),
                ("Strategy source", "source_sha256"),
            ):
                value = identity.get(field)
                identity_lines.append(
                    f"- {label}: `{value}`"
                    if isinstance(value, str)
                    else f"- {label}: unavailable"
                )
        run_lines = (
            f"\n\nWorkflow run and compact artifacts: {run_url}"
            if isinstance(run_url, str) and run_url
            else ""
        )
        desired = {
            "title": f"Latest NFI compatibility blocker ({upstream_sha[:12]})",
            "body": (
                f"{_MARKER_TEXT(fingerprint)}\n\n"
                f"Upstream commit: `{upstream_sha}`\n\n"
                + (
                    "### Checked identity\n\n"
                    + "\n".join(identity_lines)
                    + "\n\n"
                    if identity_lines
                    else ""
                )
                + "\n\n".join(sections)
                + run_lines
                + "\n\nThis issue is reconciled automatically when the blocker recovers."
            ),
        }

    keep_issue: Mapping[str, Any] | None = None
    if desired is not None:
        keep_issue = next(
            (
                issue
                for issue in issues
                if isinstance(issue.get("body"), str)
                and (match := _MARKER.search(str(issue["body"]))) is not None
                and match.group(1) == fingerprint
            ),
            issues[0] if issues else None,
        )
    keep_number = int(keep_issue["number"]) if keep_issue is not None else None
    create = desired if desired is not None and keep_issue is None else None
    update = None
    if (
        desired is not None
        and keep_issue is not None
        and (
            str(keep_issue.get("title") or "") != desired["title"]
            or str(keep_issue.get("body") or "") != desired["body"]
        )
    ):
        update = {"number": keep_number, **desired}
    close = [
        int(issue["number"])
        for issue in issues
        if int(issue["number"]) != keep_number
    ]
    return {
        "fingerprint": fingerprint,
        "create": create,
        "close": close,
        "update": update,
        "recovered": not failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--upstream-sha", required=True)
    parser.add_argument("--targeted-reports", type=Path)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    reports = {
        mode: _read_object(args.reports / f"report-{mode}.json")
        for mode in ("spot", "futures")
    }
    targeted_reports = (
        {
            mode: _read_object(
                args.targeted_reports / f"targeted-report-{mode}.json"
            )
            for mode in ("spot", "futures")
        }
        if args.targeted_reports is not None
        else None
    )
    identity = _read_object(args.identity)
    decisions = {
        mode: _read_object(args.decisions / f"automation-decision-{mode}.json")
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
        targeted_reports=targeted_reports,
        identity=identity,
        decisions=decisions,
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
            "nfi-compatibility",
            "--title",
            str(create["title"]),
            "--body",
            str(create["body"]),
        )
    update = plan["update"]
    if isinstance(update, Mapping):
        _gh(
            "issue",
            "edit",
            str(update["number"]),
            "--repo",
            args.repository,
            "--title",
            str(update["title"]),
            "--body",
            str(update["body"]),
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


def _mode_blockers(
    compatibility: Mapping[str, Any],
    targeted: Mapping[str, Any] | None,
) -> list[Any]:
    if compatibility.get("native_compatible") is not True:
        return list(compatibility.get("blockers", []))
    if targeted is None:
        return []
    plan = targeted.get("plan")
    if (
        isinstance(plan, Mapping)
        and plan.get("status") == "no-changes"
    ):
        return []
    if targeted.get("verification_state") == "quick_verified":
        return []
    blockers = targeted.get("blockers")
    return list(blockers) if isinstance(blockers, list) else []


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
