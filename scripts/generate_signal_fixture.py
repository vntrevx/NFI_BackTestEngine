#!/usr/bin/env python3
"""Regenerate the pinned Freqtrade signal-assignment oracle fixture."""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))


def main() -> None:
    """Regenerate the fixture from the pinned source tree."""
    module = import_module("nfi_backtest_engine.signal_fixture")
    module.write_fixture(ROOT / module.FIXTURE_PATH)


if __name__ == "__main__":
    main()
