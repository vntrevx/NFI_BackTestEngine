"""Fail-fast exact comparison of normalized trade surfaces."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import read_json
from .specs import validate_trade_surface


@dataclass(frozen=True)
class ParityDifference:
    """The first semantic difference between two normalized surfaces."""

    path: str
    expected: Any
    actual: Any
    reason: str

    def render(self) -> str:
        expected = _render_value(self.expected)
        actual = _render_value(self.actual)
        return (
            f"parity mismatch at {self.path}: {self.reason}; expected {expected}, actual {actual}"
        )


class ParityMismatch(AssertionError):
    """Raised when exact trade-surface parity fails."""

    def __init__(self, difference: ParityDifference):
        self.difference = difference
        super().__init__(difference.render())


def first_difference(expected: Any, actual: Any, path: str = "$") -> ParityDifference | None:
    """Return the first deterministic structural or value difference."""
    if type(expected) is not type(actual):
        return ParityDifference(
            path,
            expected,
            actual,
            f"type differs ({type(expected).__name__} != {type(actual).__name__})",
        )

    if isinstance(expected, dict):
        for key in expected:
            child_path = f"{path}.{key}"
            if key not in actual:
                return ParityDifference(child_path, expected[key], _MISSING, "key is missing")
            difference = first_difference(expected[key], actual[key], child_path)
            if difference is not None:
                return difference
        for key in actual:
            if key not in expected:
                return ParityDifference(f"{path}.{key}", _MISSING, actual[key], "unexpected key")
        return None

    if isinstance(expected, list):
        common_length = min(len(expected), len(actual))
        for index in range(common_length):
            difference = first_difference(expected[index], actual[index], f"{path}[{index}]")
            if difference is not None:
                return difference
        if len(expected) != len(actual):
            return ParityDifference(
                f"{path}.length",
                len(expected),
                len(actual),
                "array length differs",
            )
        return None

    if expected != actual:
        return ParityDifference(path, expected, actual, "value differs")
    return None


def compare_surfaces(expected: Any, actual: Any) -> None:
    """Validate and compare two in-memory trade surfaces exactly."""
    validate_trade_surface(expected)
    validate_trade_surface(actual)
    difference = first_difference(expected, actual)
    if difference is not None:
        raise ParityMismatch(difference)


def compare_surface_files(expected_path: str | Path, actual_path: str | Path) -> None:
    expected = read_json(expected_path)
    actual = read_json(actual_path)
    compare_surfaces(expected, actual)


class _Missing:
    pass


_MISSING = _Missing()


def _render_value(value: Any) -> str:
    if value is _MISSING:
        return "<missing>"
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"{rendered} ({type(value).__name__})"
