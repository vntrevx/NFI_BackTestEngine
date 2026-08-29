"""Canonical JSON hashing shared by changed-signal promotion validators."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_sha256(value: Any) -> str:
    """Hash one strict canonical JSON value without presentation whitespace."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
