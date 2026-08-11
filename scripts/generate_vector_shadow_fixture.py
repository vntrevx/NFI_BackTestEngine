#!/usr/bin/env python3
"""Regenerate the M21 independent Python/Rust vector-shadow bundle."""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))


def main() -> None:
    """Write the canonical programs and Python oracle fixture."""
    module = import_module("nfi_backtest_engine.vector_shadow_fixture")
    module.write_bundle(ROOT)


if __name__ == "__main__":
    main()
