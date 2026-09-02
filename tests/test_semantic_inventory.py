from __future__ import annotations

import json
from pathlib import Path

from nfi_backtest_engine import cli
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.semantic_inventory import build_semantic_inventory
from nfi_backtest_engine.specs import (
    SEMANTIC_INVENTORY_SCHEMA,
    validate_schema,
)

ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_PROFILE = ROOT / "planning" / "freqtrade-semantic-profile.json"


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
    summary = report["summary"]
    assert {
        key: summary[key]
        for key in (
            "vector_method_count",
            "callback_count",
            "active_callback_count",
            "rust_callback_count",
            "source_bound_callback_count",
            "route_count",
            "exact_source_fixture_count",
            "inventory_complete",
        )
    } == {
        "vector_method_count": 3,
        "callback_count": 1,
        "active_callback_count": 1,
        "rust_callback_count": 1,
        "source_bound_callback_count": 0,
        "route_count": 0,
        "exact_source_fixture_count": 1,
        "inventory_complete": False,
    }
    assert summary["obligation_count"] > 0
    assert summary["unknown_obligation_count"] == 1
    assert summary["native_promotion"] is False
    assert {item["code"] for item in report["blockers"]} == {
        "UNOBSERVED_UPSTREAM_REF"
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

    def fail_lowering(*_args, **_kwargs):
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


def test_current_contract_identity_and_unknown_callback_omission_are_characterized(
    tmp_path: Path,
) -> None:
    profile = json.loads(SEMANTIC_PROFILE.read_text(encoding="utf-8"))
    assert profile["reference"] == {
        "version": "2026.5.1",
        "image": "freqtradeorg/freqtrade",
        "image_index_digest": (
            "sha256:d47d7053dc07eca2ace20385575143090ba88621007e5e8b76052dca6038799a"
        ),
        "image_platform_digest": (
            "sha256:bc5b7276118a8539d09ea797cb32c198d029a805815a29c6d27d5f610a3e0b6b"
        ),
        "image_config_digest": (
            "sha256:8615e1e2f8c429b27f57a0bcb948dfac1abe6828df8300c63ebd88a16ec6cabc"
        ),
        "platform": "linux/amd64",
        "ccxt_version": "4.5.55",
    }
    assert profile["fingerprint"] == (
        "5b3c11b13e4a3d9fe00e3231755d5d6369ff9a32957a794bf0ffc807b76ce2a8"
    )

    source = tmp_path / "UnknownCallback.py"
    _write_strategy(source, class_name="UnknownCallback")
    source.write_text(
        source.read_text(encoding="utf-8")
        + "    def future_exchange_callback(self, trade, **kwargs):\n"
        + "        return trade\n",
        encoding="utf-8",
    )

    report = build_semantic_inventory(
        source,
        class_name="UnknownCallback",
        fixtures_root=tmp_path / "missing",
    )

    # The legacy callback projection still omits this future API, so the typed
    # registry must preserve the old boundary as an explicit promotion blocker.
    assert "future_exchange_callback" not in {
        callback["name"] for callback in report["callbacks"]
    }
    assert report["summary"]["inventory_complete"] is False
    assert report["summary"]["native_promotion"] is False
    assert {item["code"] for item in report["blockers"]} == {
        "UNKNOWN_STRATEGY_CALLBACK",
        "UNOBSERVED_UPSTREAM_REF",
    }


def test_unknown_callback_and_ast_construct_create_typed_native_blockers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "UnknownSemantics.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class UnknownSemantics(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        return dataframe\n"
        "    def future_exchange_callback(self, trade, **kwargs):\n"
        "        match trade:\n"
        "            case None:\n"
        "                return False\n"
        "            case _:\n"
        "                return True\n",
        encoding="utf-8",
    )

    report = build_semantic_inventory(
        source,
        class_name="UnknownSemantics",
        fixtures_root=tmp_path / "missing",
    )

    blocker_codes = {item["code"] for item in report["blockers"]}
    assert blocker_codes == {
        "UNKNOWN_STRATEGY_CALLBACK",
        "UNKNOWN_REACHABLE_AST_NODE",
        "UNOBSERVED_UPSTREAM_REF",
    }
    assert report["summary"]["unknown_obligation_count"] == len(
        report["blockers"]
    )
    assert report["summary"]["unknown_obligation_count"] >= 2
    assert report["summary"]["native_promotion"] is False


def test_transitive_local_helper_change_invalidates_registry_fingerprint(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "strategy_helper.py"
    helper.write_text("def scale(value):\n    return value * 2\n", encoding="utf-8")
    source = tmp_path / "ClosureStrategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "from strategy_helper import scale\n"
        "class ClosureStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        dataframe['scaled'] = scale(dataframe['close'])\n"
        "        return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    first = build_semantic_inventory(
        source,
        class_name="ClosureStrategy",
        fixtures_root=tmp_path / "missing",
    )
    helper.write_text("def scale(value):\n    return value * 3\n", encoding="utf-8")
    second = build_semantic_inventory(
        source,
        class_name="ClosureStrategy",
        fixtures_root=tmp_path / "missing",
    )

    assert first["fingerprint"] != second["fingerprint"]


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


def test_semantic_registry_parser_binds_source_closure_and_upstream_identity() -> None:
    args = cli.build_parser().parse_args(
        [
            "strategy",
            "semantic-registry",
            "latest.py",
            "--class",
            "NostalgiaForInfinityX7",
            "--source-root",
            "upstream",
            "--upstream-repository",
            "https://example.invalid/nfi.git",
            "--upstream-commit",
            "a" * 40,
            "--upstream-ref",
            "refs/heads/main",
            "--upstream-source-path",
            "user_data/strategies/NostalgiaForInfinityX7.py",
            "--output",
            "planning/freqtrade-nfi-semantic-obligation-registry.json",
        ]
    )

    assert args.strategy_command == "semantic-registry"
    assert args.source_root == Path("upstream")
    assert args.upstream_repository == "https://example.invalid/nfi.git"
    assert args.upstream_commit == "a" * 40
    assert args.upstream_ref == "refs/heads/main"
    assert args.upstream_source_path == (
        "user_data/strategies/NostalgiaForInfinityX7.py"
    )
