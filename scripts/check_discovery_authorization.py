#!/usr/bin/env python3
"""Recheck paired discovery artifacts against a trusted authorization."""

from __future__ import annotations

import argparse
from pathlib import Path

from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.discovery_publication import (
    validate_discovery_authorization,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--discovery-dir", type=Path, required=True)
    parser.add_argument("--source-run-id", required=True)
    args = parser.parse_args()

    status = read_json(args.status)
    identity = status.get("identity") if isinstance(status, dict) else None
    if not isinstance(identity, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in identity.items()
    ):
        parser.error("compatibility status identity must contain string fields")
    validate_discovery_authorization(
        args.authorization,
        {
            mode: args.discovery_dir / f"nfi-branch-discovery-{mode}"
            for mode in ("spot", "futures")
        },
        expected_identity=identity,
        expected_source_run_id=args.source_run_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
