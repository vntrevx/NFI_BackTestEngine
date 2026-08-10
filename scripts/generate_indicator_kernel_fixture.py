#!/usr/bin/env python3
"""Regenerate the official TA-Lib column oracle for the Rust vector kernels."""

from __future__ import annotations

import argparse
from pathlib import Path

from nfi_backtest_engine.indicator_kernel_fixture import (
    DEFAULT_ROWS,
    write_talib_kernel_fixture,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    args = parser.parse_args()
    write_talib_kernel_fixture(args.inventory, args.output, rows=args.rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
