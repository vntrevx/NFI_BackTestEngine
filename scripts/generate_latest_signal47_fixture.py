#!/usr/bin/env python3
"""Regenerate the latest X7 Signal 47 boundary evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from nfi_backtest_engine.latest_signal47_fixture import (  # noqa: E402
    FIXTURE_PATH,
    write_fixture,
)


def main() -> None:
    """Parse explicit source identities and write deterministic evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("current_source", type=Path)
    parser.add_argument("baseline_source", type=Path)
    parser.add_argument("--current-commit", required=True)
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / FIXTURE_PATH)
    args = parser.parse_args()
    write_fixture(
        args.current_source,
        args.baseline_source,
        current_commit=args.current_commit,
        baseline_commit=args.baseline_commit,
        output=args.output,
    )


if __name__ == "__main__":
    main()
