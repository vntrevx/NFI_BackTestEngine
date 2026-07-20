from __future__ import annotations

import ast
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.probe_strategy import (
    prepare_probe_strategy,
    write_probe_config,
)


def _strategy_source() -> str:
    return (
        "class X7:\n"
        "  # This comment and formatting must survive.\n"
        "  signal_params = {\n"
        "    'enabled': False,\n"
        "  }\n"
    )


def test_probe_strategy_changes_only_ast_selected_boolean_and_property(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    destination = tmp_path / "probe.py"
    source.write_text(_strategy_source(), encoding="utf-8")

    provenance = prepare_probe_strategy(
        source,
        destination,
        class_name="X7",
        upstream_repository="https://github.com/example/project",
        upstream_commit="a" * 40,
        boolean_toggles=[
            {
                "mapping": "signal_params",
                "key": "enabled",
                "expected": False,
                "replacement": True,
            }
        ],
        protections=[
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 1,
            }
        ],
    )

    transformed = destination.read_text(encoding="utf-8")
    tree = ast.parse(transformed)
    strategy = tree.body[0]
    assert isinstance(strategy, ast.ClassDef)
    assert "# This comment and formatting must survive." in transformed
    assert "'enabled': True" in transformed
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "protections"
        for node in strategy.body
    )
    assert provenance["base_source_sha256"] != provenance["effective_source_sha256"]
    assert len(provenance["transformations"]) == 2


def test_probe_strategy_refuses_upstream_default_drift(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text(_strategy_source(), encoding="utf-8")

    with pytest.raises(SpecValidationError, match="expected True"):
        prepare_probe_strategy(
            source,
            tmp_path / "probe.py",
            class_name="X7",
            upstream_repository="https://github.com/example/project",
            upstream_commit="a" * 40,
            boolean_toggles=[
                {
                    "mapping": "signal_params",
                    "key": "enabled",
                    "expected": True,
                    "replacement": False,
                }
            ],
        )


def test_probe_config_merge_is_recursive_and_records_leaf_paths(tmp_path: Path) -> None:
    source = tmp_path / "config.json"
    destination = tmp_path / "probe.json"
    write_json(
        source,
        {
            "api_server": {"enabled": True},
            "exchange": {"name": "binance", "pairs": ["A"]},
        },
    )

    transformation = write_probe_config(
        source,
        destination,
        overrides={
            "enable_protections": True,
            "exchange": {"pairs": ["B"]},
        },
        remove_paths=["api_server"],
    )

    assert read_json(destination) == {
        "exchange": {"name": "binance", "pairs": ["B"]},
        "enable_protections": True,
    }
    assert transformation["description"].endswith(
        "enable_protections, exchange.pairs, remove:api_server"
    )
