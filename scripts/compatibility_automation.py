#!/usr/bin/env python3
"""Write one deterministic compatibility automation decision."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nfi_backtest_engine.compatibility_automation import (
    classify_compatibility_automation,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--strategy-diff", type=Path, required=True)
    parser.add_argument("--compatibility", type=Path, required=True)
    parser.add_argument("--targeted", type=Path, required=True)
    parser.add_argument("--discovery", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = classify_compatibility_automation(
        args.identity,
        args.strategy_diff,
        args.compatibility,
        args.targeted,
        discovery=args.discovery,
        output_path=args.output,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
