#!/usr/bin/env python3
"""Publish one validated discovery fixture candidate as a draft PR."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.compatibility_candidate_plan import (
    CandidatePublicationError,
    build_candidate_plan,
    candidate_branch,
)
from nfi_backtest_engine.fixture import sha256_file, validate_fixture

__all__ = [
    "build_candidate_plan",
    "build_candidate_pr_reconciliation",
    "publish_candidate",
    "reconcile_candidate_prs",
    "sha256_file",
    "validate_fixture",
]


def publish_candidate(
    plan: Mapping[str, Any],
    *,
    repository_root: str | Path,
    repository: str,
    base: str,
) -> dict[str, Any]:
    """Push allowlisted files and keep at most one automated Draft per mode."""
    root = Path(repository_root).resolve()
    branch = str(plan["branch"])
    _validate_current_refs(plan, root, base)
    reconciliation = reconcile_candidate_prs(
        repository,
        trading_mode=str(plan["trading_mode"]),
        desired_branch=branch,
        upstream_commit=str(plan["upstream_commit"]),
        engine_commit=str(plan["engine_commit"]),
        cwd=root,
    )
    existing = _open_pr(repository, branch)
    if existing is not None:
        return {
            "branch": branch,
            "pull_request_url": existing["url"],
            "created": False,
            "ci_dispatched": False,
            "ci_trigger": "existing-pull-request",
            "superseded_pull_requests": reconciliation["closed"],
        }
    blocked = reconciliation["blocked"]
    if blocked:
        raise CandidatePublicationError(
            "a non-Draft automation candidate already occupies this mode's review slot: "
            + ", ".join(f"#{number}" for number in blocked)
        )
    remote_exists = bool(
        _run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=root,
        ).strip()
    )
    if not remote_exists:
        if _run(["git", "status", "--porcelain"], cwd=root).strip():
            raise CandidatePublicationError("candidate publisher requires a clean worktree")
        _run(["git", "switch", "--create", branch], cwd=root)
        fixture_destination = root / str(plan["fixture_destination"])
        fixture_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(plan["fixture_source"]), fixture_destination)
        evidence_destination = root / str(plan["evidence_destination"])
        write_json(
            evidence_destination,
            {
                "schema_version": "1.0.0",
                "claim_boundary": (
                    f"Branch-reaching {plan['trading_mode']} fixture candidate. "
                    "This is compact quick-verification evidence, not a five-year certificate."
                ),
                "fingerprint": plan["fingerprint"],
                "trading_mode": plan["trading_mode"],
                "upstream_commit": plan["upstream_commit"],
                "engine_commit": plan["engine_commit"],
                "strategy_sha256": plan["strategy_sha256"],
                "fixture_id": plan["fixture_id"],
                "manifest_sha256": plan["manifest_sha256"],
                "logical_bytes": plan["logical_bytes"],
                "target_ids": plan["target_ids"],
                "pair": plan["pair"],
                "timerange": plan["timerange"],
                "trade_surface_exact": True,
                "full_state_exact": True,
                "combined_full_x7_certified": False,
            },
        )
        allowlist = {
            str(plan["fixture_destination"]),
            str(plan["evidence_destination"]),
        }
        _run(["git", "add", "--", *sorted(allowlist)], cwd=root)
        changed = {
            line
            for line in _run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=root,
            ).splitlines()
            if line
        }
        if not changed or not all(
            path == str(plan["evidence_destination"])
            or path.startswith(f"{plan['fixture_destination']}/")
            for path in changed
        ):
            raise CandidatePublicationError(
                "candidate publisher staged a path outside its allowlist"
            )
        _run(
            [
                "git",
                "-c",
                "user.name=github-actions",
                "-c",
                "user.email=41898282+github-actions@users.noreply.github.com",
                "commit",
                "-m",
                f"test({plan['trading_mode']}): add discovered fixture {plan['fixture_id']}",
            ],
            cwd=root,
        )
        _validate_current_refs(plan, root, base)
        _run(["git", "push", "origin", f"HEAD:{branch}"], cwd=root)
    title = f"test({plan['trading_mode']}): add discovered fixture {plan['fixture_id']}"
    body = (
        f"<!-- nfi-exact-fixture-candidate:{plan['trading_mode']}:{plan['fingerprint']} -->\n\n"
        f"Automated bounded {plan['trading_mode']} branch discovery candidate.\n\n"
        f"- Fingerprint: `{plan['fingerprint']}`\n"
        f"- Upstream: `{plan['upstream_commit']}`\n"
        f"- Engine: `{plan['engine_commit']}`\n"
        f"- Targets: `{', '.join(plan['target_ids'])}`\n"
        f"- Logical size: `{plan['logical_bytes']}` bytes\n"
        "- Independent official/Native trade surface: exact\n"
        "- Independent official/Native full state: exact\n\n"
        "At most one automated candidate Draft is kept open per trading mode. "
        "A newer immutable identity closes this Draft as superseded. "
        "This PR is never approved or merged automatically."
    )
    _validate_current_refs(plan, root, base)
    url = _run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repository,
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
            "--draft",
        ],
        cwd=root,
    ).strip()
    return {
        "branch": branch,
        "pull_request_url": url,
        "created": True,
        "ci_dispatched": False,
        "ci_trigger": "pull_request",
        "superseded_pull_requests": reconciliation["closed"],
    }


def _validate_current_refs(plan: Mapping[str, Any], root: Path, base: str) -> None:
    current_engine_sha = _run(
        ["git", "ls-remote", "origin", f"refs/heads/{base}"], cwd=root
    ).split()[0]
    current_upstream_sha = _run(
        [
            "git",
            "ls-remote",
            "https://github.com/iterativv/NostalgiaForInfinity.git",
            "refs/heads/main",
        ],
        cwd=root,
    ).split()[0]
    if current_engine_sha != plan.get("engine_commit") or current_upstream_sha != plan.get(
        "upstream_commit"
    ):
        raise CandidatePublicationError("current refs changed before external mutation")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--max-bytes", type=int)
    parser.add_argument("--expected-engine-sha", required=True)
    parser.add_argument("--expected-upstream-sha", required=True)
    parser.add_argument("--reconcile-mode", choices=("spot", "futures"))
    parser.add_argument("--preserve-request-fingerprint")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.reconcile_mode is not None:
        _validate_current_refs(
            {
                "engine_commit": args.expected_engine_sha,
                "upstream_commit": args.expected_upstream_sha,
            },
            args.repo_root.resolve(),
            args.base,
        )
        desired_branch = (
            None
            if args.preserve_request_fingerprint is None
            else candidate_branch(args.reconcile_mode, args.preserve_request_fingerprint)
        )
        result = reconcile_candidate_prs(
            args.repository,
            trading_mode=args.reconcile_mode,
            desired_branch=desired_branch,
            upstream_commit=args.expected_upstream_sha,
            engine_commit=args.expected_engine_sha,
            cwd=args.repo_root.resolve(),
        )
    else:
        if args.preserve_request_fingerprint is not None:
            parser.error("--preserve-request-fingerprint requires --reconcile-mode")
        if args.report is None or args.candidate_dir is None or args.max_bytes is None:
            parser.error("--report, --candidate-dir, and --max-bytes are required for publication")
        report = read_json(args.report)
        if not isinstance(report, dict):
            raise CandidatePublicationError("discovery report must be an object")
        plan = build_candidate_plan(
            report,
            args.candidate_dir,
            args.repo_root,
            max_bytes=args.max_bytes,
        )
        if (
            plan.get("engine_commit") != args.expected_engine_sha
            or plan.get("upstream_commit") != args.expected_upstream_sha
        ):
            raise CandidatePublicationError("workflow refs differ from candidate proof")
        result = publish_candidate(
            plan,
            repository_root=args.repo_root,
            repository=args.repository,
            base=args.base,
        )
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            for key, value in result.items():
                rendered = str(value).lower() if isinstance(value, bool) else value
                handle.write(f"{key}={rendered}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


def build_candidate_pr_reconciliation(
    open_pull_requests: Sequence[Mapping[str, Any]],
    *,
    trading_mode: str,
    desired_branch: str | None,
) -> dict[str, Any]:
    """Select one current review slot and supersede stale automation Drafts."""
    if trading_mode not in {"spot", "futures"}:
        raise CandidatePublicationError("candidate reconciliation trading mode is invalid")
    prefix = f"automation/{trading_mode}-fixture-"
    keep: int | None = None
    close: list[int] = []
    blocked: list[int] = []
    for pull_request in open_pull_requests:
        branch = pull_request.get("headRefName")
        number = pull_request.get("number")
        if (
            not isinstance(branch, str)
            or not branch.startswith(prefix)
            or not isinstance(number, int)
            or isinstance(number, bool)
        ):
            continue
        if desired_branch is not None and branch == desired_branch:
            keep = number
            continue
        if pull_request.get("isDraft") is True:
            close.append(number)
        else:
            blocked.append(number)
    return {
        "trading_mode": trading_mode,
        "desired_branch": desired_branch,
        "keep": keep,
        "close": sorted(close),
        "blocked": sorted(blocked),
    }


def reconcile_candidate_prs(
    repository: str,
    *,
    trading_mode: str,
    desired_branch: str | None,
    upstream_commit: str,
    engine_commit: str,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Close only stale automation-owned Drafts; never mutate non-Draft PRs."""
    open_pull_requests = _open_candidate_prs(repository)
    plan = build_candidate_pr_reconciliation(
        open_pull_requests,
        trading_mode=trading_mode,
        desired_branch=desired_branch,
    )
    replacement = (
        f" The replacement candidate branch is `{desired_branch}`."
        if desired_branch is not None
        else " The current identity did not authorize an exact fixture candidate."
    )
    comment = (
        f"Superseded automatically by upstream `{upstream_commit}` and engine "
        f"`{engine_commit}`.{replacement} The closed PR remains immutable review history; "
        "no approval or merge was performed."
    )
    for number in plan["close"]:
        _run(
            [
                "gh",
                "pr",
                "close",
                str(number),
                "--repo",
                repository,
                "--comment",
                comment,
            ],
            cwd=cwd,
        )
    return {**plan, "closed": plan["close"]}


def _open_candidate_prs(repository: str) -> list[dict[str, Any]]:
    output = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repository,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,body,headRefName,isDraft,url",
        ]
    )
    if not output.strip():
        return []
    records = json.loads(output)
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise CandidatePublicationError("GitHub returned an invalid candidate PR list")
    return records


def _open_pr(repository: str, branch: str) -> dict[str, Any] | None:
    output = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repository,
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,state,url",
        ]
    )
    records = json.loads(output)
    if not isinstance(records, list):
        raise CandidatePublicationError("GitHub returned an invalid pull request list")
    return records[0] if records else None


def _run(arguments: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


if __name__ == "__main__":
    raise SystemExit(main())
