from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.execution_contract import (
    build_execution_contract,
    load_execution_contract,
    validate_native_execution_contract,
)
from nfi_backtest_engine.fixture import validate_fixture
from nfi_backtest_engine.specs import EXECUTION_CONTRACT_SCHEMA, validate_schema
from nfi_backtest_engine.state_trace import trace_summary

ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "planning" / "freqtrade-semantic-profile.json"
SCHEDULER = ROOT / "planning" / "freqtrade-scheduler-contract.json"
CONTRACT = ROOT / "planning" / "freqtrade-execution-contract.json"
RUST_CONTRACT = (
    ROOT
    / "rust"
    / "crates"
    / "nfi-sim-core"
    / "src"
    / "execution_contract.json"
)
FIXTURES = ROOT / "benchmarks" / "fixtures" / "captured"


def test_execution_contract_matches_dependencies_and_embedded_rust() -> None:
    contract = load_execution_contract(
        CONTRACT,
        semantic_profile_path=PROFILE,
        scheduler_contract_path=SCHEDULER,
    )

    validate_schema(contract, EXECUTION_CONTRACT_SCHEMA)
    validate_native_execution_contract(
        contract,
        RUST_CONTRACT.read_text(encoding="utf-8"),
    )
    assert contract == build_execution_contract(PROFILE, SCHEDULER)
    assert contract["scope"]["trading_modes"] == ["spot"]
    assert contract["wallet"]["mutation_order"] == "serial-scheduler-event-order"
    assert contract["entry"]["pre_order_gate_rejection_consumes_order_id"] is False
    assert contract["entry"]["amount_or_confirmation_rejection_consumes_order_id"] is True
    assert contract["precision"]["amount"] == "floor-to-exchange-step"


def test_installed_native_extension_exposes_the_same_execution_contract() -> None:
    from nfi_backtest_engine import _rust

    contract = load_execution_contract(CONTRACT)

    validate_native_execution_contract(contract, _rust.execution_contract_json())


def test_execution_contract_rejects_one_dependency_without_the_other() -> None:
    with pytest.raises(SpecValidationError, match="both dependency contracts"):
        load_execution_contract(CONTRACT, semantic_profile_path=PROFILE)


@pytest.mark.parametrize(
    ("fixture_name", "trade_count", "order_count"),
    [
        ("normal-routing-spot-2025-01-01_04", 6, 15),
        ("stops-only-spot-2025-01-01_04", 2, 4),
    ],
)
def test_spot_execution_fixtures_materialize_orders_wallet_and_full_state(
    fixture_name: str,
    trade_count: int,
    order_count: int,
) -> None:
    manifest_path = FIXTURES / fixture_name / "manifest.json"
    manifest = validate_fixture(manifest_path)
    surface = read_json(
        manifest_path.parent / manifest["artifacts"]["trade_surface"]["path"]
    )
    trace = trace_summary(
        manifest_path.parent / manifest["artifacts"]["state_trace"]["path"]
    )

    assert manifest["freqtrade"]["trading_mode"] == "spot"
    assert len(surface["trades"]) == trade_count
    assert sum(len(trade["orders"]) for trade in surface["trades"]) == order_count
    assert surface["summary"]["starting_balance"] == "1000"
    assert surface["summary"]["final_balance"] != "1000"
    assert trace["source"] == "freqtrade-reference"
    assert trace["include_state"] is True
    assert trace["event_count"] > 0


def test_execution_contract_command_parses_explicit_dependencies() -> None:
    args = cli.build_parser().parse_args(
        [
            "reference",
            "execution-contract",
            "--semantic-profile",
            "semantic-profile.json",
            "--scheduler-contract",
            "scheduler-contract.json",
            "--output",
            "execution-contract.json",
        ]
    )

    assert args.reference_command == "execution-contract"
    assert args.semantic_profile == Path("semantic-profile.json")
    assert args.scheduler_contract == Path("scheduler-contract.json")
    assert args.output == Path("execution-contract.json")
