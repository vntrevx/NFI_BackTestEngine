from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "select_discovery_cursor",
    ROOT / "scripts" / "select_discovery_cursor.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_cursor_selection_requires_full_fingerprint() -> None:
    assert MODULE.cursor_matches(
        {
            "schema_version": "1.0.0",
            "fingerprint": "a" * 64,
            "next_shard": 3,
        },
        "a" * 64,
    )
    assert not MODULE.cursor_matches(
        {
            "schema_version": "1.0.0",
            "fingerprint": "b" * 64,
            "next_shard": 3,
        },
        "a" * 64,
    )
