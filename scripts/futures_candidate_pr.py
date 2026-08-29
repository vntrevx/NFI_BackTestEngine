#!/usr/bin/env python3
"""Publish one validated discovery fixture candidate as a draft PR."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.compatibility_candidate_plan import (
    CandidatePublicationError,
    build_candidate_plan,
)
from nfi_backtest_engine.fixture import sha256_file, validate_fixture

__all__ = [
    "build_candidate_plan",
    "publish_candidate",
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
    """Push allowlisted files and recheck current refs immediately before Draft PR creation."""
    root = Path(repository_root).resolve()
    branch = str(plan["branch"])
    existing = _open_pr(repository, branch)
    if existing is not None:
        return {
            "branch": branch,
            "pull_request_url": existing["url"],
            "created": False,
            "ci_dispatched": False,
        }
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
        f"Automated bounded {plan['trading_mode']} branch discovery candidate.\n\n"
        f"- Fingerprint: `{plan['fingerprint']}`\n"
        f"- Upstream: `{plan['upstream_commit']}`\n"
        f"- Engine: `{plan['engine_commit']}`\n"
        f"- Targets: `{', '.join(plan['target_ids'])}`\n"
        f"- Logical size: `{plan['logical_bytes']}` bytes\n"
        "- Independent official/Native trade surface: exact\n"
        "- Independent official/Native full state: exact\n\n"
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
    _run(
        ["gh", "workflow", "run", "ci.yml", "--repo", repository, "--ref", branch],
        cwd=root,
    )
    return {
        "branch": branch,
        "pull_request_url": url,
        "created": True,
        "ci_dispatched": True,
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
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--expected-engine-sha", required=True)
    parser.add_argument("--expected-upstream-sha", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
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
