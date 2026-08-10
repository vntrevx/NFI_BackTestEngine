#!/usr/bin/env python3
"""Open one evidence-only Draft PR for a blocked generic semantic review."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nfi_backtest_engine.canonical import read_json, write_json

_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_SHA = re.compile(r"[0-9a-f]{40}")
_MODES = {"spot", "futures"}
_REVIEW_KINDS = {"new_opcode", "generic_lowering"}


def build_review_plan(
    decision: Mapping[str, Any],
    repository_root: str | Path,
) -> dict[str, Any]:
    """Validate one fail-closed decision and choose its allowlisted PR path."""

    root = Path(repository_root).resolve()
    if decision.get("schema_version") != "1.0.0":
        raise ValueError("compatibility automation decision schema is unsupported")
    mode = decision.get("trading_mode")
    review_kind = decision.get("review_kind")
    action_fingerprint = decision.get("action_fingerprint")
    identity = decision.get("identity")
    action = decision.get("action")
    verification = decision.get("verification")
    if (
        mode not in _MODES
        or review_kind not in _REVIEW_KINDS
        or _FINGERPRINT.fullmatch(str(action_fingerprint)) is None
        or not isinstance(identity, Mapping)
        or not isinstance(action, Mapping)
        or not isinstance(verification, Mapping)
    ):
        raise ValueError("semantic review decision identity is invalid")
    if (
        decision.get("automation_route") != "semantic_review_draft_pr"
        or decision.get("execution_route") != "official_only"
        or verification.get("exact") is not False
        or action.get("native_promotion_allowed") is not False
        or action.get("draft_pr_allowed") is not True
        or action.get("draft_pr_kind") != review_kind
        or action.get("automatic_semantic_merge_allowed") is not False
    ):
        raise ValueError("semantic review decision is not fail-closed")
    upstream_sha = identity.get("upstream_sha")
    engine_sha = identity.get("engine_sha")
    if (
        _SHA.fullmatch(str(upstream_sha)) is None
        or _SHA.fullmatch(str(engine_sha)) is None
    ):
        raise ValueError("semantic review commit identity is invalid")
    suffix = str(action_fingerprint)[:16]
    destination = Path("planning") / "compatibility-reviews" / f"{mode}-{suffix}.json"
    if (root / destination).exists():
        raise ValueError("semantic review destination already exists on the base branch")
    return {
        "branch": f"automation/{mode}-semantic-review-{suffix}",
        "destination": destination.as_posix(),
        "trading_mode": mode,
        "review_kind": review_kind,
        "action_fingerprint": action_fingerprint,
        "upstream_sha": upstream_sha,
        "engine_sha": engine_sha,
        "document": {
            "schema_version": "compatibility-semantic-review-v1",
            "claim_boundary": (
                "Automation evidence for generic IR review only. Native support remains "
                "blocked until independent trade-surface and full-state exact proof."
            ),
            "decision": dict(decision),
            "review_requirements": [
                "implement only a source-structural generic opcode or lowerer",
                "add a focused unit test and an official captured fixture",
                "prove changed-branch, trade-surface, and full-state exactness",
                "obtain human review and Required CI; never merge automatically",
            ],
        },
    }


def publish_review(
    plan: Mapping[str, Any],
    *,
    repository_root: str | Path,
    repository: str,
    base: str,
) -> dict[str, Any]:
    """Push an evidence-only branch and open a Draft PR without approving it."""

    root = Path(repository_root).resolve()
    branch = str(plan["branch"])
    pending = _existing_pending_review(repository, plan)
    if pending is not None:
        return {
            "branch": pending["headRefName"],
            "pull_request_url": pending["url"],
            "created": False,
            "state": pending["state"],
            "ci_dispatched": False,
            "deduplicated": True,
        }
    existing = _existing_pr(repository, branch)
    if existing is not None:
        return {
            "branch": branch,
            "pull_request_url": existing["url"],
            "created": False,
            "state": existing["state"],
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
            raise ValueError("semantic review publisher requires a clean worktree")
        _run(["git", "switch", "--create", branch], cwd=root)
        destination = root / str(plan["destination"])
        write_json(destination, plan["document"])
        _run(["git", "add", "--", str(plan["destination"])], cwd=root)
        staged = _run(["git", "diff", "--cached", "--name-only"], cwd=root).splitlines()
        if staged != [str(plan["destination"])]:
            raise ValueError("semantic review publisher staged a path outside its allowlist")
        _run(
            [
                "git",
                "-c",
                "user.name=github-actions",
                "-c",
                "user.email=41898282+github-actions@users.noreply.github.com",
                "commit",
                "-m",
                f"chore({plan['trading_mode']}): propose generic semantic review",
            ],
            cwd=root,
        )
        _run(["git", "push", "origin", f"HEAD:{branch}"], cwd=root)
    title = (
        f"chore({plan['trading_mode']}): review {plan['review_kind']} compatibility"
    )
    marker = (
        "<!-- nfi-semantic-review:"
        f"{plan['trading_mode']}:{plan['action_fingerprint']} -->"
    )
    body = (
        f"{marker}\n\n"
        "Automated evidence-only review for a blocked generic Native lowering.\n\n"
        f"- Upstream: `{plan['upstream_sha']}`\n"
        f"- Engine: `{plan['engine_sha']}`\n"
        f"- Review kind: `{plan['review_kind']}`\n"
        f"- Fingerprint: `{plan['action_fingerprint']}`\n\n"
        "This PR does not add Native semantics and must never be approved or merged "
        "automatically. A maintainer must implement the generic behavior and attach "
        "independent exact evidence."
    )
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
        "state": "OPEN",
        "ci_dispatched": False,
        "deduplicated": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decision", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base", default="main")
    args = parser.parse_args()
    decision = read_json(args.decision)
    if not isinstance(decision, dict):
        raise ValueError("compatibility automation decision must be an object")
    plan = build_review_plan(decision, args.repo_root)
    result = publish_review(
        plan,
        repository_root=args.repo_root,
        repository=args.repository,
        base=args.base,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _existing_pr(repository: str, branch: str) -> dict[str, Any] | None:
    records = json.loads(
        _run(
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
    )
    if not isinstance(records, list):
        raise ValueError("GitHub returned an invalid semantic review PR list")
    record = records[0] if records else None
    return dict(record) if isinstance(record, Mapping) else None


def _existing_pending_review(
    repository: str,
    plan: Mapping[str, Any],
) -> dict[str, Any] | None:
    records = json.loads(
        _run(
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
                "body,headRefName,state,url",
            ]
        )
    )
    if not isinstance(records, list):
        raise ValueError("GitHub returned an invalid open semantic review PR list")
    return find_pending_review(records, plan)


def find_pending_review(
    records: list[object],
    plan: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Find an already-open review for the same upstream semantic gap."""

    marker = f"<!-- nfi-semantic-review:{plan['trading_mode']}:"
    upstream = f"- Upstream: `{plan['upstream_sha']}`"
    review_kind = f"- Review kind: `{plan['review_kind']}`"
    for record in records:
        if not isinstance(record, Mapping):
            continue
        body = record.get("body")
        if (
            isinstance(body, str)
            and marker in body
            and upstream in body
            and review_kind in body
            and isinstance(record.get("headRefName"), str)
            and isinstance(record.get("url"), str)
        ):
            return dict(record)
    return None


def _run(arguments: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


if __name__ == "__main__":
    raise SystemExit(main())
