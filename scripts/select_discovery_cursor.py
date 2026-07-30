#!/usr/bin/env python3
"""Select a ledger cursor only when its full discovery fingerprint still matches."""

from __future__ import annotations

import argparse
import shutil
from datetime import UTC, datetime
from pathlib import Path

from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.futures_discovery import (
    build_discovery_request,
    load_discovery_policy,
)


def cursor_matches(cursor: object, fingerprint: str) -> bool:
    return (
        isinstance(cursor, dict)
        and cursor.get("schema_version") == "1.0.0"
        and cursor.get("fingerprint") == fingerprint
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-diff", type=Path, required=True)
    parser.add_argument("--compatibility", type=Path, required=True)
    parser.add_argument("--fixtures-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--upstream-commit", required=True)
    parser.add_argument("--engine-commit", required=True)
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--baseline-upstream-commit")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.baseline_source is None) != (args.baseline_upstream_commit is None):
        raise ValueError("baseline source and upstream commit must be supplied together")
    policy = load_discovery_policy(args.policy)
    request = build_discovery_request(
        args.strategy_diff,
        args.compatibility,
        args.fixtures_root,
        policy=policy,
        upstream_commit=args.upstream_commit,
        engine_commit=args.engine_commit,
        as_of=datetime.now(UTC).date(),
        baseline_upstream_commit=args.baseline_upstream_commit,
        baseline_source_sha256=(
            sha256_file(args.baseline_source)
            if args.baseline_source is not None
            else None
        ),
    )
    cursor = read_json(args.candidate)
    if cursor_matches(cursor, request["fingerprint"]):
        if args.output.exists():
            raise ValueError(f"selected cursor already exists: {args.output}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.candidate, args.output)
        print(f"selected matching discovery cursor: {request['fingerprint']}")
    else:
        print(f"ignored superseded discovery cursor: {request['fingerprint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
