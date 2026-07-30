#!/usr/bin/env python3
"""Publish one size-bounded Spot or Futures fixture candidate as a draft PR."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.fixture import sha256_file, validate_fixture

_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_FIXTURE_ID = re.compile(r"[a-z0-9][a-z0-9.-]*")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_MODES = {"spot", "futures"}


def build_candidate_plan(
    report: Mapping[str, Any],
    candidate_directory: str | Path,
    repository_root: str | Path,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    """Validate candidate identity, exactness, size, and repository destinations."""
    root = Path(repository_root).resolve()
    candidate_input = Path(candidate_directory)
    if candidate_input.is_symlink():
        raise ValueError("fixture candidate root must not be a symlink")
    candidate_root = candidate_input.resolve()
    if not candidate_root.is_dir():
        raise ValueError("fixture candidate directory is missing")
    for path in candidate_root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("fixture candidate contains a symlink or special file")
    if report.get("status") != "candidate_found":
        raise ValueError("discovery report does not contain a fixture candidate")
    fingerprint = report.get("fingerprint")
    trading_mode = report.get("trading_mode")
    candidate = report.get("candidate")
    if (
        _FINGERPRINT.fullmatch(str(fingerprint)) is None
        or trading_mode not in _MODES
        or not isinstance(candidate, Mapping)
    ):
        raise ValueError("discovery candidate identity is invalid")
    if (
        candidate.get("trade_surface_exact") is not True
        or candidate.get("full_state_exact") is not True
    ):
        raise ValueError("fixture candidate lacks independent exact evidence")
    manifest_path = candidate_root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("fixture candidate manifest is missing")
    manifest = validate_fixture(manifest_path)
    manifest_sha256 = sha256_file(manifest_path)
    fixture_id = manifest.get("fixture_id")
    if (
        not isinstance(fixture_id, str)
        or _FIXTURE_ID.fullmatch(fixture_id) is None
        or ".." in fixture_id
    ):
        raise ValueError("fixture candidate id is not a repository-safe slug")
    upstream_commit = report.get("upstream_commit")
    engine_commit = report.get("engine_commit")
    provenance = manifest.get("strategy_provenance")
    if (
        _COMMIT.fullmatch(str(upstream_commit)) is None
        or _COMMIT.fullmatch(str(engine_commit)) is None
        or not isinstance(provenance, Mapping)
        or provenance.get("upstream_commit") != upstream_commit
        or provenance.get("effective_source_sha256") != report.get("strategy_sha256")
        or manifest.get("freqtrade", {}).get("trading_mode") != trading_mode
        or candidate.get("fixture_id") != fixture_id
        or candidate.get("manifest_sha256") != manifest_sha256
    ):
        raise ValueError("fixture candidate identity differs from its sealed manifest")
    logical_bytes = sum(
        path.stat().st_size for path in candidate_root.rglob("*") if path.is_file()
    )
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes <= 0
        or logical_bytes > max_bytes
        or candidate.get("logical_bytes") != logical_bytes
    ):
        raise ValueError("fixture candidate exceeds or differs from its sealed size")
    suffix = str(fingerprint)[:16]
    fixture_relative = Path("benchmarks") / "fixtures" / "captured" / fixture_id
    evidence_relative = Path("benchmarks") / "evidence" / (
        f"future-nfi-{trading_mode}-{suffix}.json"
    )
    if (root / fixture_relative).exists() or (root / evidence_relative).exists():
        raise ValueError("fixture candidate destination already exists")
    target_ids = candidate.get("target_ids")
    if not isinstance(target_ids, list) or not target_ids or not all(
        isinstance(value, str) and value for value in target_ids
    ):
        raise ValueError("fixture candidate target ids are invalid")
    return {
        "fingerprint": fingerprint,
        "branch": f"automation/{trading_mode}-fixture-{suffix}",
        "trading_mode": trading_mode,
        "fixture_id": fixture_id,
        "fixture_source": str(candidate_root),
        "fixture_destination": fixture_relative.as_posix(),
        "evidence_destination": evidence_relative.as_posix(),
        "logical_bytes": logical_bytes,
        "manifest_sha256": manifest_sha256,
        "target_ids": sorted(target_ids),
        "upstream_commit": upstream_commit,
        "engine_commit": engine_commit,
        "strategy_sha256": report.get("strategy_sha256"),
        "timerange": candidate.get("timerange"),
        "pair": candidate.get("pair"),
    }


def publish_candidate(
    plan: Mapping[str, Any],
    *,
    repository_root: str | Path,
    repository: str,
    base: str,
) -> dict[str, Any]:
    """Push the allowlisted files, open a draft PR, and dispatch required CI."""
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
            raise ValueError("candidate publisher requires a clean worktree")
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
                    "This is compact "
                    "quick-verification evidence, not a five-year certificate."
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
            raise ValueError("candidate publisher staged a path outside its allowlist")
        _run(
            [
                "git",
                "-c",
                "user.name=github-actions",
                "-c",
                "user.email=41898282+github-actions@users.noreply.github.com",
                "commit",
                "-m",
                (
                    f"test({plan['trading_mode']}): add discovered fixture "
                    f"{plan['fixture_id']}"
                ),
            ],
            cwd=root,
        )
        _run(["git", "push", "origin", f"HEAD:{branch}"], cwd=root)
    title = (
        f"test({plan['trading_mode']}): add discovered fixture "
        f"{plan['fixture_id']}"
    )
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
        [
            "gh",
            "workflow",
            "run",
            "ci.yml",
            "--repo",
            repository,
            "--ref",
            branch,
        ],
        cwd=root,
    )
    return {
        "branch": branch,
        "pull_request_url": url,
        "created": True,
        "ci_dispatched": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    report = read_json(args.report)
    if not isinstance(report, dict):
        raise ValueError("discovery report must be an object")
    plan = build_candidate_plan(
        report,
        args.candidate_dir,
        args.repo_root,
        max_bytes=args.max_bytes,
    )
    result = publish_candidate(
        plan,
        repository_root=args.repo_root,
        repository=args.repository,
        base=args.base,
    )
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            for key, value in result.items():
                handle.write(
                    f"{key}={str(value).lower() if isinstance(value, bool) else value}\n"
                )
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
        raise ValueError("GitHub returned an invalid pull request list")
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
