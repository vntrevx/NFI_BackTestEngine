from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from nfi_backtest_engine import cli
from nfi_backtest_engine.specs import STATEFUL_COVERAGE_SCHEMA, validate_schema
from nfi_backtest_engine.stateful_coverage import build_stateful_coverage

_SHA = "a" * 64


def _write_strategy(path: Path, *, short_grind_enabled: bool = False) -> None:
    path.write_text(
        f'''from freqtrade.strategy import IStrategy

class NostalgiaForInfinityX7Audit(IStrategy):
    timeframe = "5m"
    position_adjustment_enable = True
    long_entry_signal_params = {{
        "long_entry_condition_42_enable": True,
        "long_entry_condition_43_enable": False,
    }}
    short_entry_signal_params = {{
        "short_entry_condition_620_enable": {short_grind_enabled!r},
    }}
    long_normal_mode_tags = ["42", "43"]
    short_grind_mode_tags = ["620"]

    def populate_entry_trend(self, df, metadata):
        long_entry_signal_params = self.long_entry_signal_params
        entry_tags = []
        for enabled_long_entry_signal in long_entry_signal_params:
            long_entry_condition_index = int(enabled_long_entry_signal.rsplit("_", 2)[1])
            if long_entry_signal_params[enabled_long_entry_signal]:
                item_long_entry = df["close"] > 0
                _append_entry_tag(entry_tags, item_long_entry, f"{{long_entry_condition_index}} ")
        short_entry_signal_params = self.short_entry_signal_params
        for enabled_short_entry_signal in short_entry_signal_params:
            short_entry_condition_index = int(enabled_short_entry_signal.rsplit("_", 2)[1])
            if short_entry_signal_params[enabled_short_entry_signal]:
                item_short_entry = df["close"] < 0
                _append_entry_tag(entry_tags, item_short_entry, f"{{short_entry_condition_index}} ")
        return df

    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        enter_tags = trade.enter_tag.split()
        if any(tag in self.long_normal_mode_tags for tag in enter_tags):
            return "long-exit"
        if any(tag in self.short_grind_mode_tags for tag in enter_tags):
            return "short-exit"
        return None

    def adjust_trade_position(self, trade, current_time, current_rate,
                              current_profit, min_stake, max_stake, **kwargs):
        for order in trade.select_filled_orders(trade.exit_side):
            if order.safe_remaining * current_rate > min_stake:
                return order.safe_remaining, "remaining"
        enter_tags = trade.enter_tag.split()
        if any(tag in self.long_normal_mode_tags for tag in enter_tags):
            return 1.0, "long-adjust"
        if any(tag in self.short_grind_mode_tags for tag in enter_tags):
            return 1.0, "short-adjust"
        return None
''',
        encoding="utf-8",
    )


def _program(version: str = "test-program-v1") -> dict[str, Any]:
    return {
        "schema_version": version,
        "fingerprint": _SHA,
    }


def _hot_ir() -> dict[str, Any]:
    managed_exit = {
        **_program("managed-exit-program-v1"),
        "routes": [{"id": "long_normal", "match": {"entry_tags": ["42", "43"]}}],
        "partial_fill_policy": "filled-orders-have-zero-remaining",
    }
    managed_short_exit = {
        **_program("managed-exit-program-v1"),
        "routes": [],
    }
    operation = {
        "schema_version": "test-manager-v1",
        "supported_routes": {
            "long_normal": {
                "entry_tags": ["42", "43"],
            }
        },
        "route_order": ["long_normal"],
        "managed_exit_program": managed_exit,
        "supported_short_routes": {},
        "short_route_order": [],
        "managed_short_exit_program": managed_short_exit,
        "position_adjustment": {
            "entry_tags": ["42", "43"],
            "program": _program("system-adjustment-program-v1"),
        },
    }
    return {
        "schema_version": "test-hot-ir-v1",
        "fingerprint": _SHA,
        "hot_loop_ready": True,
        "callbacks": [
            {
                "name": "custom_exit",
                "backend": "rust-nfi-x7-trade-manager",
                "executable_in_rust": True,
            },
            {
                "name": "adjust_trade_position",
                "backend": "rust-nfi-x7-position-adjustment",
                "executable_in_rust": True,
            },
        ],
        "nfi_trade_manager": {
            "schema_version": "test-manager-v1",
            "backend": "rust-nfi-x7-trade-manager",
            "operation": operation,
            "proof": {"operation_sha256": _SHA},
        },
    }


def test_stateful_coverage_proves_reachable_routes_and_separates_dormant_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "Strategy.py"
    _write_strategy(source)
    monkeypatch.setattr(
        "nfi_backtest_engine.stateful_coverage.build_hot_callback_ir",
        lambda *args, **kwargs: _hot_ir(),
    )

    report = build_stateful_coverage(
        source,
        class_name="NostalgiaForInfinityX7Audit",
        trading_mode="futures",
        upstream_repository="https://example.invalid/nfi.git",
        upstream_commit="b" * 40,
    )

    validate_schema(report, STATEFUL_COVERAGE_SCHEMA)
    assert report["summary"]["closure_complete"] is True
    assert report["summary"]["reachable_stateful_gap_count"] == 0
    assert report["entry_signals"][0]["enabled_tags"] == ["42"]
    assert report["dormant_stateful_routes"] == [
        {
            "side": "short",
            "tag": "620",
            "route_keys": ["short_grind_mode_tags"],
            "reason": "source-signal-disabled",
            "qualifies_as_native_coverage": False,
        }
    ]
    assert report["live_only_exclusions"][0]["native_policy"] == (
        "filled-orders-have-zero-remaining"
    )
    assert report["qualification"]["full_state_certified"] is False


def test_newly_enabled_source_tag_becomes_a_reachable_gap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "Strategy.py"
    _write_strategy(source, short_grind_enabled=True)
    monkeypatch.setattr(
        "nfi_backtest_engine.stateful_coverage.build_hot_callback_ir",
        lambda *args, **kwargs: _hot_ir(),
    )

    report = build_stateful_coverage(
        source,
        class_name="NostalgiaForInfinityX7Audit",
        trading_mode="futures",
    )

    assert report["summary"]["closure_complete"] is False
    assert {item["code"] for item in report["reachable_stateful_gaps"]} == {
        "REACHABLE_EXIT_ROUTE_NOT_NATIVE",
        "REACHABLE_ADJUSTMENT_ROUTE_NOT_NATIVE",
    }
    assert all(item["tags"] == ["620"] for item in report["reachable_stateful_gaps"])
    assert report["dormant_stateful_routes"] == []


def test_unsealed_adjustment_program_does_not_count_as_native_coverage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "Strategy.py"
    _write_strategy(source)
    hot_ir = _hot_ir()
    hot_ir["nfi_trade_manager"]["operation"]["position_adjustment"]["program"] = {}
    monkeypatch.setattr(
        "nfi_backtest_engine.stateful_coverage.build_hot_callback_ir",
        lambda *args, **kwargs: copy.deepcopy(hot_ir),
    )

    report = build_stateful_coverage(
        source,
        class_name="NostalgiaForInfinityX7Audit",
        trading_mode="spot",
    )

    assert [item["code"] for item in report["reachable_stateful_gaps"]] == [
        "REACHABLE_ADJUSTMENT_ROUTE_NOT_NATIVE"
    ]
    adjustment = next(
        item
        for item in report["native_contracts"]
        if item["id"] == "adjust-position:long:position_adjustment"
    )
    assert adjustment["status"] == "missing-or-unsealed"


def test_managed_exit_tag_mismatch_does_not_count_as_native_coverage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "Strategy.py"
    _write_strategy(source)
    hot_ir = _hot_ir()
    route = hot_ir["nfi_trade_manager"]["operation"]["managed_exit_program"]["routes"][0]
    route["match"]["entry_tags"] = ["different-source-tag"]
    monkeypatch.setattr(
        "nfi_backtest_engine.stateful_coverage.build_hot_callback_ir",
        lambda *args, **kwargs: copy.deepcopy(hot_ir),
    )

    report = build_stateful_coverage(
        source,
        class_name="NostalgiaForInfinityX7Audit",
        trading_mode="spot",
    )

    assert [item["code"] for item in report["reachable_stateful_gaps"]] == [
        "REACHABLE_EXIT_ROUTE_NOT_NATIVE"
    ]
    assert report["reachable_stateful_gaps"][0]["tags"] == ["42"]


def test_safe_remaining_requires_a_serialized_live_only_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "Strategy.py"
    _write_strategy(source)
    hot_ir = _hot_ir()
    del hot_ir["nfi_trade_manager"]["operation"]["managed_exit_program"][
        "partial_fill_policy"
    ]
    monkeypatch.setattr(
        "nfi_backtest_engine.stateful_coverage.build_hot_callback_ir",
        lambda *args, **kwargs: copy.deepcopy(hot_ir),
    )

    report = build_stateful_coverage(
        source,
        class_name="NostalgiaForInfinityX7Audit",
        trading_mode="spot",
    )

    assert report["live_only_exclusions"] == []
    assert [item["code"] for item in report["reachable_stateful_gaps"]] == [
        "LIVE_ONLY_BRANCH_POLICY_MISSING"
    ]


def test_entry_tag_identity_change_fails_closed_before_route_qualification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "Strategy.py"
    _write_strategy(source)
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            'f"{long_entry_condition_index} "',
            '"hardcoded-tag "',
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "nfi_backtest_engine.stateful_coverage.build_hot_callback_ir",
        lambda *args, **kwargs: _hot_ir(),
    )

    report = build_stateful_coverage(
        source,
        class_name="NostalgiaForInfinityX7Audit",
        trading_mode="spot",
    )

    assert report["summary"]["closure_complete"] is False
    assert report["reachable_stateful_gaps"][0]["code"] == "ENTRY_SIGNAL_PROOF_MISSING"


def test_stateful_coverage_parser_requires_mode_and_output() -> None:
    args = cli.build_parser().parse_args(
        [
            "strategy",
            "stateful-coverage",
            "latest.py",
            "--class",
            "NostalgiaForInfinityX7",
            "--trading-mode",
            "futures",
            "--upstream-repository",
            "https://example.invalid/nfi.git",
            "--upstream-commit",
            "b" * 40,
            "--output",
            ".nfi/stateful-coverage.json",
        ]
    )

    assert args.strategy_command == "stateful-coverage"
    assert args.trading_mode == "futures"
    assert args.upstream_commit == "b" * 40
    assert args.output == Path(".nfi/stateful-coverage.json")
