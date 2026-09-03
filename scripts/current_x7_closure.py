#!/usr/bin/env python3
"""Seal a compact current-X7 identity, target inventory, and discovery cursor."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.current_x7_closure import build_current_x7_closure
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import sha256_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-diff", type=Path, required=True)
    parser.add_argument("--spot-report", type=Path, required=True)
    parser.add_argument("--futures-report", type=Path, required=True)
    parser.add_argument("--spot-request", type=Path, required=True)
    parser.add_argument("--futures-request", type=Path, required=True)
    parser.add_argument("--spot-policy", type=Path, required=True)
    parser.add_argument("--futures-policy", type=Path, required=True)
    parser.add_argument("--upstream-repository", required=True)
    parser.add_argument("--upstream-ref", required=True)
    parser.add_argument("--upstream-commit", required=True)
    parser.add_argument("--baseline-upstream-commit", required=True)
    parser.add_argument("--engine-commit", required=True)
    parser.add_argument("--semantic-profile-sha256", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    requests = {
        "spot": read_json(args.spot_request),
        "futures": read_json(args.futures_request),
    }
    policies = {
        "spot": read_json(args.spot_policy),
        "futures": read_json(args.futures_policy),
    }
    for mode, path in (("spot", args.spot_policy), ("futures", args.futures_policy)):
        identity = requests[mode].get("identity")
        if (
            not isinstance(identity, dict)
            or identity.get("policy_sha256") != sha256_file(path)
        ):
            raise SpecValidationError(f"current X7 {mode} policy file identity differs")
    document = build_current_x7_closure(
        read_json(args.strategy_diff),
        {
            "spot": read_json(args.spot_report),
            "futures": read_json(args.futures_report),
        },
        requests,
        policies,
        upstream_repository=args.upstream_repository,
        upstream_ref=args.upstream_ref,
        upstream_commit=args.upstream_commit,
        baseline_upstream_commit=args.baseline_upstream_commit,
        engine_commit=args.engine_commit,
        semantic_profile_sha256=args.semantic_profile_sha256,
        observed_at=args.observed_at,
    )
    write_json(args.output, document)
    print(json.dumps({"fingerprint": document["fingerprint"], "modes": document["modes"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
