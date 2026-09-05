from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import validate_fixture
from nfi_backtest_engine.futures_contract import (
    build_futures_contract,
    load_futures_contract,
    validate_native_futures_contract,
)
from nfi_backtest_engine.specs import FUTURES_CONTRACT_SCHEMA, validate_schema
from nfi_backtest_engine.state_trace import trace_summary

ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "planning" / "freqtrade-semantic-profile.json"
SCHEDULER = ROOT / "planning" / "freqtrade-scheduler-contract.json"
EXECUTION = ROOT / "planning" / "freqtrade-execution-contract.json"
CONTRACT = ROOT / "planning" / "freqtrade-futures-contract.json"
RUST_CONTRACT = (
    ROOT
    / "rust"
    / "crates"
    / "nfi-sim-core"
    / "src"
    / "futures_contract.json"
)
FIXTURES = ROOT / "benchmarks" / "fixtures" / "captured"


def test_futures_contract_matches_dependencies_and_embedded_rust() -> None:
    contract = load_futures_contract(
        CONTRACT,
        semantic_profile_path=PROFILE,
        scheduler_contract_path=SCHEDULER,
        execution_contract_path=EXECUTION,
    )

    validate_schema(contract, FUTURES_CONTRACT_SCHEMA)
    validate_native_futures_contract(
        contract,
        RUST_CONTRACT.read_text(encoding="utf-8"),
    )
    assert contract == build_futures_contract(PROFILE, SCHEDULER, EXECUTION)
    assert contract["scope"] == {
        "trading_mode": "futures",
        "exchange": "binance",
        "margin_mode": "isolated",
        "unknown_exchange_or_margin_semantics": "fail-before-native-promotion",
        "parity_requirement": "trade-surface-and-full-state-exact-zero-tolerance",
    }
    assert contract["funding"]["running_segment_moves_to_next_filled_order"] is True
    assert contract["exit_collision"][
        "rejected_stop_does_not_fall_through_to_liquidation"
    ] is True


def test_installed_native_extension_exposes_the_same_futures_contract() -> None:
    from nfi_backtest_engine import _rust

    contract = load_futures_contract(CONTRACT)

    validate_native_futures_contract(contract, _rust.futures_contract_json())


def test_futures_contract_requires_all_dependency_paths() -> None:
    with pytest.raises(SpecValidationError, match="all three dependency contracts"):
        load_futures_contract(CONTRACT, semantic_profile_path=PROFILE)


@pytest.mark.parametrize(
    ("fixture_name", "side", "protection", "exit_reason"),
    [
        ("x7-futures-lifecycle-long-v17.4.435-2022-04-10_04-20", "long", None, None),
        ("x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20", "short", None, None),
        (
            "x7-liquidation-stoploss-guard-futures-v17.4.435-2022-04-29_05-02",
            "long",
            "StoplossGuard",
            "liquidation",
        ),
        ("x7-cooldown-futures-v17.4.435-2023-01-01_16", "long", "CooldownPeriod", None),
        ("x7-low-profit-futures-v17.4.435-2023-01-01_16", "long", "LowProfitPairs", None),
        (
            "x7-low-profit-integer-futures-v17.5.38-2023-01-01_16",
            "long",
            "LowProfitPairs",
            None,
        ),
        ("x7-max-drawdown-futures-v17.4.435-2023-01-01_16", "long", "MaxDrawdown", None),
    ],
)
def test_futures_semantic_fixtures_are_materialized_full_state_evidence(
    fixture_name: str,
    side: str,
    protection: str | None,
    exit_reason: str | None,
) -> None:
    manifest_path = FIXTURES / fixture_name / "manifest.json"
    manifest = validate_fixture(manifest_path)
    coverage = manifest["required_coverage"]
    surface = read_json(
        manifest_path.parent / manifest["artifacts"]["trade_surface"]["path"]
    )
    trace = trace_summary(
        manifest_path.parent / manifest["artifacts"]["state_trace"]["path"]
    )
    input_roles = {item["role"] for item in manifest["inputs"]}

    assert manifest["freqtrade"]["trading_mode"] == "futures"
    assert manifest["freqtrade"]["margin_mode"] == "isolated"
    assert side in coverage["sides"]
    if protection is not None:
        assert protection in coverage["protection_methods"]
    if exit_reason is not None:
        assert exit_reason in coverage["exit_reasons"]
    assert "funding_candles" in input_roles
    assert "mark_candles" in input_roles
    assert surface["trades"]
    assert trace["source"] == "freqtrade-reference"
    assert trace["include_state"] is True
    assert trace["event_count"] > 0


def test_futures_contract_command_parses_explicit_dependencies() -> None:
    args = cli.build_parser().parse_args(
        [
            "reference",
            "futures-contract",
            "--semantic-profile",
            "semantic-profile.json",
            "--scheduler-contract",
            "scheduler-contract.json",
            "--execution-contract",
            "execution-contract.json",
            "--output",
            "futures-contract.json",
        ]
    )

    assert args.reference_command == "futures-contract"
    assert args.semantic_profile == Path("semantic-profile.json")
    assert args.scheduler_contract == Path("scheduler-contract.json")
    assert args.execution_contract == Path("execution-contract.json")
    assert args.output == Path("futures-contract.json")
