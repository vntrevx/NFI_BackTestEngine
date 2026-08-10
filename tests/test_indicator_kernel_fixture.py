from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from nfi_backtest_engine.indicator_kernel_fixture import (
    KernelFixtureError,
    generate_talib_kernel_fixture,
)


def _inventory(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "fingerprint": "a" * 64,
                "upstream": {"repository": "example.invalid/nfi", "commit": "b" * 40},
                "source": {"sha256": "c" * 64},
                "operations": [
                    {
                        "family": "talib",
                        "callable": "talib.RSI",
                        "occurrences": [
                            {
                                "arguments": [
                                    {"name": "#0", "literal": None},
                                    {"name": "timeperiod", "literal": 14},
                                ]
                            },
                            {
                                "arguments": [
                                    {"name": "#0", "literal": None},
                                    {"name": "timeperiod", "literal": 3},
                                ]
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_fixture_is_deterministic_bit_exact_and_inventory_driven(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    _inventory(inventory)

    first = generate_talib_kernel_fixture(inventory, rows=600)
    second = generate_talib_kernel_fixture(inventory, rows=600)

    assert first == second
    assert first["fingerprint"] == second["fingerprint"]
    assert {case["arguments"]["timeperiod"] for case in first["cases"]} == {3, 14}
    assert all(case["name"] == "RSI" for case in first["cases"])
    assert all(len(case["outputs"][0]["values"]) == 600 for case in first["cases"])
    tokens = {token for case in first["cases"] for token in case["outputs"][0]["values"]}
    assert "nan" in tokens
    assert any(token.startswith("0x") for token in tokens)


def test_fixture_fails_closed_for_non_literal_parameter(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    _inventory(inventory)
    document = json.loads(inventory.read_text(encoding="utf-8"))
    invalid = copy.deepcopy(document)
    invalid["operations"][0]["occurrences"][0]["arguments"][1] = {
        "name": "timeperiod",
        "literal": None,
    }
    inventory.write_text(json.dumps(invalid), encoding="utf-8")

    with pytest.raises(KernelFixtureError, match="non-literal"):
        generate_talib_kernel_fixture(inventory, rows=600)
