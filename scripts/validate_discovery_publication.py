#!/usr/bin/env python3
"""Authorize paired discovery artifacts for ledger and Draft-PR mutation."""

from __future__ import annotations

import argparse
from pathlib import Path

from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.discovery_publication import authorize_discovery_publication


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proof-dir", type=Path, required=True)
    parser.add_argument("--discovery-dir", type=Path, required=True)
    parser.add_argument("--fixtures-root", type=Path, required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    proof = args.proof_dir
    source_run = read_json(proof / "source-run.json")
    if (
        not isinstance(source_run, dict)
        or str(source_run.get("source_run_id")) != args.source_run_id
    ):
        parser.error("source-run proof differs from --source-run-id")
    authorize_discovery_publication(
        proof / "compatibility-identity.json",
        proof / "strategy-diff.json",
        {
            mode: proof / f"report-{mode}.json"
            for mode in ("spot", "futures")
        },
        {
            mode: proof / f"targeted-report-{mode}.json"
            for mode in ("spot", "futures")
        },
        {
            mode: args.discovery_dir / f"nfi-branch-discovery-{mode}"
            for mode in ("spot", "futures")
        },
        {
            mode: Path("planning") / f"{mode}-discovery-policy.json"
            for mode in ("spot", "futures")
        },
        args.fixtures_root,
        source_run_id=args.source_run_id,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
