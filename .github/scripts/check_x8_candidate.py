#!/usr/bin/env python3
"""Check two bounded original-X8 captures using the installed candidate wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from nfi_backtest_engine import __version__
from nfi_backtest_engine.fixture_engine import run_fixture_engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    rows = []
    for mode in ("spot", "futures"):
        manifest = (
            root / f"benchmarks/fixtures/captured/x8-bounded-original-{mode}-r1/manifest.json"
        )
        run = run_fixture_engine(
            manifest,
            args.output / mode,
            verification_level="full",
            timeout_seconds=600,
        )
        if not run["complete"] or not run["parity"]["equal"]:
            raise RuntimeError(f"installed X8 {mode} parity failed")
        state = run["parity"]["state_trace"]
        if not state["checked"] or not state["equal"]:
            raise RuntimeError(f"installed X8 {mode} full-state parity was not checked")
        rows.append(
            {
                "mode": mode,
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "trade_count": run["execution"]["trade_count"],
                "state_count": state["actual"]["event_count"],
                "state_sha256": state["actual"]["stream_hash"],
                "build": run["execution"]["build"],
                "equal": True,
            }
        )
    report = {"package_version": __version__, "cases": rows, "complete": True}
    (args.output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
