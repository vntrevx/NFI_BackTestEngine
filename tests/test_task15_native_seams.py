from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from nfi_backtest_engine.callback_execution_contract import compile_callback_execution_ir
from nfi_backtest_engine.errors import TraceError
from nfi_backtest_engine.executable_callback_program import compile_executable_callback_program
from nfi_backtest_engine.portfolio_official_projection import (
    project_official_portfolio_boundaries,
)
from nfi_backtest_engine.strategy_ir import analyze_strategy

ROOT = Path(__file__).parents[1]
MANIFEST = next(
    (ROOT / "benchmarks/fixtures/captured").glob(
        "current-portfolio-pressure-3p-*/manifest.json"
    )
)
STRATEGY = next(
    (ROOT / "benchmarks/fixtures/captured").glob(
        "current-portfolio-pressure-3p-*/inputs/strategy.py"
    )
)


def test_portfolio_callbacks_compile_from_source_without_fixture_routing() -> None:
    analysis = analyze_strategy(STRATEGY, class_name="PortfolioPressureOracle")
    execution = compile_callback_execution_ir(
        analysis,
        trading_mode="spot",
        run_mode="backtest",
    )

    program = compile_executable_callback_program(
        analysis,
        execution,
        trading_mode="spot",
        run_mode="backtest",
    )

    assert program["entrypoints"]["custom_stake_amount"]["active"] is True
    assert program["entrypoints"]["adjust_trade_position"]["active"] is True
    assert program["entrypoints"]["custom_exit"]["active"] is True
    assert program["identity"]["source_closure"]


def test_official_projection_requires_observable_boundaries_and_kills_mutation() -> None:
    manifest = json.loads(MANIFEST.read_bytes())
    root = MANIFEST.parent
    auxiliary = [
        json.loads((root / item["path"]).read_bytes())
        for item in manifest["inputs"]
        if item["role"] == "auxiliary"
    ]
    trace = next(
        item
        for item in auxiliary
        if item["schema_version"] == "freqtrade-portfolio-pressure-trace-v1"
    )
    authentication = next(
        item
        for item in auxiliary
        if item["schema_version"] == "official-source-authentication-v1"
    )
    contract_path = (
        ROOT
        / "python/nfi_backtest_engine/contracts/freqtrade-portfolio-pressure-contract-v1.json"
    )
    contract = json.loads(contract_path.read_bytes())
    surface = json.loads(
        (root / manifest["artifacts"]["trade_surface"]["path"]).read_bytes()
    )

    projected = project_official_portfolio_boundaries(
        trace,
        surface,
        slot_limit=1,
        contract=contract,
        authentication=authentication,
    )

    assert len(projected) == 44
    assert [event["sequence"] for event in projected] == list(range(44))
    assert projected[-1]["boundary"] == "force_exit"
    missing_observable = copy.deepcopy(trace)
    missing_observable["events"] = [
        event
        for event in missing_observable["events"]
        if not (
            event["phase"] == "trade.entry"
            and event["timestamp_ms"] == 1735690500000
        )
    ]
    with pytest.raises(TraceError, match="lacks captured trade.entry observable"):
        project_official_portfolio_boundaries(
            missing_observable,
            surface,
            slot_limit=1,
            contract=contract,
            authentication=authentication,
        )
    mutated = copy.deepcopy(trace)
    next(
        item
        for item in mutated["callbacks"]
        if item["callback"] == "custom_stake_amount"
        and item["timestamp_ms"] == 1735692300000
    )["proposed_stake"] = 900.0
    changed = project_official_portfolio_boundaries(
        mutated,
        surface,
        slot_limit=1,
        contract=contract,
        authentication=authentication,
    )
    assert changed != projected
