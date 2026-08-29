#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/changed_target_ledger.py --help
"""Generate one deterministic current-HEAD changed-target proof ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nfi_backtest_engine.changed_target_ledger import (
    ChangedTargetLedgerSources,
    build_changed_target_ledger,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-diff", type=Path, required=True)
    parser.add_argument("--semantic-registry", type=Path, required=True)
    parser.add_argument("--fixture-registry", type=Path, required=True)
    parser.add_argument("--spot-targeted-report", type=Path, required=True)
    parser.add_argument("--futures-targeted-report", type=Path, required=True)
    parser.add_argument("--upstream-repository", required=True)
    parser.add_argument("--upstream-ref", required=True)
    parser.add_argument("--upstream-head", required=True)
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_changed_target_ledger(
        ChangedTargetLedgerSources(
            strategy_diff=args.strategy_diff,
            semantic_registry=args.semantic_registry,
            fixture_registry=args.fixture_registry,
            targeted_reports={
                "spot": args.spot_targeted_report,
                "futures": args.futures_targeted_report,
            },
            upstream_repository=args.upstream_repository,
            upstream_ref=args.upstream_ref,
            upstream_head=args.upstream_head,
            baseline_commit=args.baseline_commit,
        ),
        output_path=args.output,
    )
    print(json.dumps(report["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
