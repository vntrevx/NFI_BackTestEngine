#!/usr/bin/env python3
"""Atomically reseal intentionally changed regression-contract repository files."""

from __future__ import annotations

import argparse
from pathlib import Path

from nfi_backtest_engine.regression_contract import (
    reseal_regression_contract_repository_files,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("python/nfi_backtest_engine/contracts/regression-v1.1.0.json"),
    )
    parser.add_argument("--path", action="append", required=True, dest="paths")
    args = parser.parse_args()
    reseal_regression_contract_repository_files(
        args.contract,
        repository_root=args.root,
        relative_paths=args.paths,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
