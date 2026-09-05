from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.trade_ir import build_trade_dependency_ir
from nfi_backtest_engine.x7 import trade_manager
from nfi_backtest_engine.x7.route_contracts import (
    MANAGED_LONG_PROGRAM_ORDER,
    MANAGED_LONG_ROUTE_SPECS,
    MANAGED_SHORT_PROGRAM_ORDER,
    MANAGED_SHORT_ROUTE_SPECS,
)

_SOURCE = Path("benchmarks/evidence/m22/current-x7-raw/upstream-NostalgiaForInfinityX7.source")
# CPython can change ast.dump output embedded in predicate identities. Keep the
# reviewed serialization variants as version-independent full-document goldens;
# an unknown manager digest must still fail closed.
_REVIEWED_MANAGER_SHA256 = frozenset(
    {
        "76d0a73e8db7e751ef0e5de00708ead80c88664f16efdcdc895325af911ad768",
        "19f4101a9370990d68d1520e0bde706b481d4a69838d75403825b07ff739fea3",
    }
)


def _compile() -> dict[str, object]:
    analysis = analyze_strategy(_SOURCE, class_name="NostalgiaForInfinityX7")
    manager = trade_manager.build_nfi_trade_manager_ir(
        analysis,
        build_trade_dependency_ir(analysis),
    )
    assert manager is not None
    return manager


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_manager_facade_and_declarative_route_contract_are_stable() -> None:
    assert trade_manager.__all__ == [
        "NFI_TRADE_MANAGER_IR_VERSION",
        "build_nfi_trade_manager_ir",
    ]
    assert trade_manager.NFI_TRADE_MANAGER_IR_VERSION == "0.31.0"
    assert MANAGED_LONG_PROGRAM_ORDER == (
        "long_exit_signals",
        "long_exit_main",
        "long_exit_williams_r",
        "long_exit_dec",
    )
    assert MANAGED_SHORT_PROGRAM_ORDER == (
        "short_exit_signals",
        "short_exit_main",
        "short_exit_williams_r",
        "short_exit_dec",
    )
    assert [spec.key for spec in MANAGED_LONG_ROUTE_SPECS] == [
        "long_normal",
        "long_pump",
        "long_quick",
        "long_rebuy",
        "long_high_profit",
        "long_rapid",
        "long_top_coins",
        "long_scalp",
    ]
    assert [spec.key for spec in MANAGED_SHORT_ROUTE_SPECS] == [
        "short_normal",
        "short_pump",
        "short_quick",
        "short_rebuy",
        "short_high_profit",
        "short_rapid",
        "short_scalp",
        "short_top_coins_fallback",
    ]
    assert {spec.side for spec in MANAGED_LONG_ROUTE_SPECS} == {"long"}
    assert {spec.side for spec in MANAGED_SHORT_ROUTE_SPECS} == {"short"}


def test_current_manager_program_is_canonical_and_repeatable() -> None:
    first = _compile()
    second = _compile()

    assert first == second
    assert _canonical_sha256(first) in _REVIEWED_MANAGER_SHA256
    operation = first["operation"]
    assert isinstance(operation, dict)
    assert operation["route_order"] == [
        "long_normal",
        "long_pump",
        "long_quick",
        "long_rebuy",
        "long_high_profit",
        "long_rapid",
        "long_grind",
        "long_btc",
        "long_top_coins",
        "long_scalp",
    ]
    assert operation["short_route_order"] == [
        "short_normal",
        "short_pump",
        "short_quick",
        "short_rebuy",
        "short_high_profit",
        "short_rapid",
        "short_scalp",
        "short_top_coins_fallback",
    ]


def test_non_x7_and_invalid_x7_selection_remain_fail_closed() -> None:
    assert (
        trade_manager.build_nfi_trade_manager_ir(
            {"strategies": [{"name": "OtherStrategy"}], "source": {}},
            {},
        )
        is None
    )
    with pytest.raises(
        StrategyAnalysisError,
        match="requires one selected strategy",
    ):
        trade_manager.build_nfi_trade_manager_ir({"strategies": []}, {})
