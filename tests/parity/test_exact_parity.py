from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.parity import ParityMismatch, compare_surfaces, first_difference
from nfi_backtest_engine.specs import validate_trade_surface

ROOT = Path(__file__).parents[2]
SURFACE = ROOT / "benchmarks" / "fixtures" / "contract" / "normal-routing" / "trade-surface.json"


def test_equal_surface_has_exact_parity() -> None:
    surface = read_json(SURFACE)
    compare_surfaces(surface, deepcopy(surface))


def test_first_semantic_difference_reports_exact_path() -> None:
    expected = read_json(SURFACE)
    actual = deepcopy(expected)
    actual["trades"][0]["orders"][1]["price"] = "3700.0000000001"

    with pytest.raises(ParityMismatch) as error:
        compare_surfaces(expected, actual)

    difference = error.value.difference
    assert difference.path == "$.trades[0].orders[1].price"
    assert difference.expected == "3700"
    assert difference.actual == "3700.0000000001"


def test_array_order_is_semantic() -> None:
    expected = read_json(SURFACE)
    actual = deepcopy(expected)
    actual_orders = actual["trades"][0]["orders"]
    actual_orders[0], actual_orders[1] = actual_orders[1], actual_orders[0]

    difference = first_difference(expected, actual)

    assert difference is not None
    assert difference.path == "$.trades[0].orders[0].sequence"


def test_noncanonical_decimal_is_rejected_before_comparison() -> None:
    surface = read_json(SURFACE)
    surface["trades"][0]["open_rate"] = "3760.0"

    with pytest.raises(SpecValidationError, match="decimal is not canonical"):
        validate_trade_surface(surface)


def test_binary_float_is_rejected_by_surface_schema() -> None:
    surface = read_json(SURFACE)
    surface["trades"][0]["open_rate"] = 3760.0

    with pytest.raises(SpecValidationError, match="not of type 'string'"):
        validate_trade_surface(surface)
