from __future__ import annotations

import json
from pathlib import Path

from nfi_backtest_engine import cli
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.semantic_inventory import build_semantic_inventory
from nfi_backtest_engine.specs import SEMANTIC_INVENTORY_SCHEMA, validate_schema


def _write_strategy(path: Path, *, class_name: str = "InventoryStrategy") -> None:
    path.write_text(
        "from freqtrade.strategy import IStrategy\n"
        f"class {class_name}(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        dataframe['feature'] = dataframe['close'] * 2\n"
        "        return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[dataframe['feature'] > 1, 'enter_long'] = 1\n"
        "        dataframe.loc[dataframe['feature'] > 1, 'enter_tag'] = 'dynamic-tag'\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        return dataframe\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        return 'profit' if current_profit > 0.1 else None\n",
        encoding="utf-8",
    )


def test_semantic_inventory_maps_ownership_native_boundary_and_exact_fixture(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Strategy.py"
    _write_strategy(source)
    fixture = tmp_path / "fixtures" / "exact"
    fixture.mkdir(parents=True)
    source_sha = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    (fixture / "manifest.json").write_text(
        json.dumps(
            {
                "fixture_id": "exact-custom-exit",
                "evidence_status": "captured",
                "freqtrade": {"trading_mode": "spot"},
                "inputs": [
                    {
                        "role": "strategy",
                        "sha256": source_sha,
                    }
                ],
                "required_coverage": {
                    "callbacks": ["custom_exit"],
                    "entry_tags": ["dynamic-tag"],
                    "protection_methods": [],
                    "sides": ["long"],
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_semantic_inventory(
        source,
        class_name="InventoryStrategy",
        trading_mode="spot",
        fixtures_root=tmp_path / "fixtures",
    )

    validate_schema(report, SEMANTIC_INVENTORY_SCHEMA)
    assert [item["name"] for item in report["vector_methods"]] == [
        "populate_indicators",
        "populate_entry_trend",
        "populate_exit_trend",
    ]
    callback = report["callbacks"][0]
    assert callback["name"] == "custom_exit"
    assert callback["backend"] == "rust-custom-exit-vm"
    assert callback["native_boundary"] == "generic-rust-ir"
    assert callback["exact_fixture_ids"] == ["exact-custom-exit"]
    assert report["summary"] == {
        "vector_method_count": 3,
        "callback_count": 1,
        "active_callback_count": 1,
        "rust_callback_count": 1,
        "source_bound_callback_count": 0,
        "route_count": 0,
        "exact_source_fixture_count": 1,
        "inventory_complete": True,
    }
    assert len(report["fingerprint"]) == 64
    repeated = build_semantic_inventory(
        source,
        class_name="InventoryStrategy",
        trading_mode="spot",
        fixtures_root=tmp_path / "fixtures",
    )
    assert repeated["fingerprint"] == report["fingerprint"]


def test_semantic_inventory_records_lowering_review_without_executing_strategy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "Strategy.py"
    _write_strategy(source)

    def fail_lowering(*args, **kwargs):
        raise StrategyAnalysisError("stateful route changed")

    monkeypatch.setattr(
        "nfi_backtest_engine.semantic_inventory.build_hot_callback_ir",
        fail_lowering,
    )
    report = build_semantic_inventory(
        source,
        class_name="InventoryStrategy",
        fixtures_root=tmp_path / "missing",
    )

    assert report["summary"]["inventory_complete"] is False
    assert report["callbacks"][0]["native_boundary"] == "lowering-review-required"
    assert report["compilation_errors"] == [
        {
            "code": "EXACT_LOWERING_REVIEW_REQUIRED",
            "message": "stateful route changed",
        }
    ]


def test_semantic_inventory_parser_binds_mode_config_and_evidence_root() -> None:
    args = cli.build_parser().parse_args(
        [
            "strategy",
            "semantic-inventory",
            "latest.py",
            "--class",
            "NostalgiaForInfinityX7",
            "--trading-mode",
            "futures",
            "--config",
            "config.json",
            "--fixtures-root",
            "benchmarks/fixtures/captured",
            "--output",
            ".nfi/semantic-inventory.json",
        ]
    )

    assert args.strategy_command == "semantic-inventory"
    assert args.trading_mode == "futures"
    assert args.config == Path("config.json")
    assert args.output == Path(".nfi/semantic-inventory.json")
