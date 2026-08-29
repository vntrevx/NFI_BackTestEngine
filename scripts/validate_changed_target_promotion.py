#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/validate_changed_target_promotion.py --help
"""Validate workflow Native decisions against the authoritative target ledger."""

from __future__ import annotations

import argparse
from pathlib import Path

from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.changed_target_workflow import validate_changed_target_promotion


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    args = parser.parse_args()
    ledger = read_json(args.ledger)
    decisions = {
        mode: read_json(args.decisions / f"automation-decision-{mode}.json")
        for mode in ("futures", "spot")
    }
    validate_changed_target_promotion(ledger, decisions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
