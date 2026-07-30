#!/usr/bin/env python3
"""Resolve a prior upstream source revision by its content digest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

_COMMIT = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def find_source_commit(
    commit_shas: Iterable[str],
    *,
    expected_sha256: str,
    fetch_source: Callable[[str], bytes],
) -> tuple[str, bytes]:
    """Return the first history revision whose exact source digest matches."""
    _validate_digest(expected_sha256)
    for commit_sha in commit_shas:
        _validate_commit(commit_sha)
        source = fetch_source(commit_sha)
        if hashlib.sha256(source).hexdigest() == expected_sha256:
            return commit_sha, source
    raise ValueError("expected source digest was not found in bounded upstream history")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--max-commits", type=int, default=200)
    parser.add_argument("--output-source", type=Path, required=True)
    parser.add_argument("--output-metadata", type=Path, required=True)
    args = parser.parse_args()
    _validate_request(
        repository=args.repository,
        head_sha=args.head_sha,
        source_path=args.source_path,
        expected_sha256=args.expected_sha256,
        max_commits=args.max_commits,
    )
    token = os.environ.get("GH_TOKEN", "")
    commits = _iter_path_commits(
        repository=args.repository,
        head_sha=args.head_sha,
        source_path=args.source_path,
        max_commits=args.max_commits,
        token=token,
    )
    commit_sha, source = find_source_commit(
        commits,
        expected_sha256=args.expected_sha256,
        fetch_source=lambda revision: _fetch_source(
            repository=args.repository,
            commit_sha=revision,
            source_path=args.source_path,
        ),
    )
    args.output_source.parent.mkdir(parents=True, exist_ok=True)
    args.output_source.write_bytes(source)
    metadata = {
        "schema_version": "1.0.0",
        "repository": args.repository,
        "head_sha": args.head_sha,
        "source_path": args.source_path,
        "resolved_commit": commit_sha,
        "source_sha256": args.expected_sha256,
    }
    args.output_metadata.parent.mkdir(parents=True, exist_ok=True)
    args.output_metadata.write_text(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, sort_keys=True))
    return 0


def _iter_path_commits(
    *,
    repository: str,
    head_sha: str,
    source_path: str,
    max_commits: int,
    token: str,
) -> Iterable[str]:
    yielded = 0
    page = 1
    while yielded < max_commits:
        page_size = min(100, max_commits - yielded)
        query = urllib.parse.urlencode(
            {
                "sha": head_sha,
                "path": source_path,
                "per_page": page_size,
                "page": page,
            }
        )
        document = json.loads(
            _request(
                f"https://api.github.com/repos/{repository}/commits?{query}",
                token=token,
                accept="application/vnd.github+json",
            )
        )
        if not isinstance(document, list):
            raise ValueError("upstream commit history response is not an array")
        if not document:
            return
        for record in document:
            if not isinstance(record, dict) or not isinstance(record.get("sha"), str):
                raise ValueError("upstream commit history contains an invalid record")
            yield str(record["sha"])
            yielded += 1
            if yielded >= max_commits:
                return
        if len(document) < page_size:
            return
        page += 1


def _fetch_source(
    *,
    repository: str,
    commit_sha: str,
    source_path: str,
) -> bytes:
    encoded_path = urllib.parse.quote(source_path, safe="/")
    return _request(
        f"https://raw.githubusercontent.com/{repository}/{commit_sha}/{encoded_path}",
        token="",
        accept="text/plain",
    )


def _request(url: str, *, token: str, accept: str) -> bytes:
    headers = {
        "Accept": accept,
        "User-Agent": "nfi-backtest-engine-compatibility",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(content) > _MAX_RESPONSE_BYTES:
        raise ValueError("upstream response exceeds the bounded size limit")
    return content


def _validate_request(
    *,
    repository: str,
    head_sha: str,
    source_path: str,
    expected_sha256: str,
    max_commits: int,
) -> None:
    repository_parts = repository.split("/")
    if (
        _REPOSITORY.fullmatch(repository) is None
        or any(part in {".", ".."} for part in repository_parts)
    ):
        raise ValueError("repository must use OWNER/NAME form")
    _validate_commit(head_sha)
    _validate_digest(expected_sha256)
    path = PurePosixPath(source_path)
    if path.is_absolute() or ".." in path.parts or source_path in {"", "."}:
        raise ValueError("source path must be a repository-relative path")
    if isinstance(max_commits, bool) or not 1 <= max_commits <= 500:
        raise ValueError("max commits must be between 1 and 500")


def _validate_commit(value: str) -> None:
    if _COMMIT.fullmatch(value) is None:
        raise ValueError("commit must be 40 lowercase hexadecimal characters")


def _validate_digest(value: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError("source digest must be 64 lowercase hexadecimal characters")


if __name__ == "__main__":
    raise SystemExit(main())
