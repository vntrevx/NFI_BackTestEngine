"""Authenticate this bounded qualification without rerunning backtests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nfi_backtest_engine.fixture import validate_fixture


def main() -> None:
    evidence = Path(__file__).resolve().parent
    root = evidence.parents[2]
    report = json.loads((evidence / "verification.json").read_text())
    authenticated = 0

    def visit(value):
        nonlocal authenticated
        if isinstance(value, dict):
            if {"path", "sha256", "bytes"} <= value.keys():
                path = root / value["path"]
                data = path.read_bytes()
                assert len(data) == value["bytes"], path
                assert hashlib.sha256(data).hexdigest() == value["sha256"], path
                authenticated += 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(report)
    for case in report["cases"]:
        validate_fixture(root / case["manifest"]["path"])
        assert case["source_identical"]
        assert case["official_memory"]["verdict"] == "within_limit"
        assert case["official_swap"]["peak_bytes"] == 0
    print(
        json.dumps(
            {
                "authenticated_artifacts": authenticated,
                "fixtures": len(report["cases"]),
                "valid": True,
            }
        )
    )


if __name__ == "__main__":
    main()
