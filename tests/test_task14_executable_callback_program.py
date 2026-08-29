from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from nfi_backtest_engine.callback_execution_contract import compile_callback_execution_ir
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.executable_callback_program import (
    compile_executable_callback_program,
    validate_executable_callback_program,
)
from nfi_backtest_engine.strategy_ir import analyze_strategy

ROOT = Path(__file__).parents[1]
SPOT = ROOT / "benchmarks/fixtures/captured/current-callback-oracle-spot-r1/inputs/strategy.py"
FUTURES = (
    ROOT / "benchmarks/fixtures/captured/current-callback-oracle-futures-r1/inputs/strategy.py"
)
SCHEMA = ROOT / "python/nfi_backtest_engine/schemas/executable-callback-program-v1.schema.json"
NAMES = {
    "loop_cadence_startup_lookback",
    "bot_loop_start",
    "leverage",
    "custom_stake_amount",
    "confirm_trade_entry",
    "order_filled",
    "adjust_trade_position",
    "custom_stoploss",
    "custom_exit",
    "confirm_trade_exit",
}


def _compile(path: Path, mode: str = "spot") -> dict[str, Any]:
    analysis = analyze_strategy(path, class_name="Task14CallbackOracle")
    execution = compile_callback_execution_ir(analysis, trading_mode=mode, run_mode="backtest")
    return compile_executable_callback_program(
        analysis, execution, trading_mode=mode, run_mode="backtest"
    )


def _fingerprint(document: dict[str, Any]) -> str:
    value = copy.deepcopy(document)
    identity = value["identity"]
    assert isinstance(identity, dict)
    identity.pop("program_fingerprint")
    for ref in identity["source_closure"]:
        ref.pop("diagnostic_path", None)
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _ops(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if isinstance(value.get("op"), str):
            found.append(value["op"])
        for child in value.values():
            found.extend(_ops(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_ops(child))
    return found


def test_oracles_compile_zero_to_full_exact_coverage_and_schema() -> None:
    spot, futures = _compile(SPOT), _compile(FUTURES, "futures")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(spot, schema)
    jsonschema.validate(futures, schema)
    assert set(spot["entrypoints"]) == NAMES
    assert spot["entrypoints"]["leverage"]["active"] is False
    assert futures["entrypoints"]["leverage"]["active"] is True
    assert len(spot["registers"]) == 4
    assert {item["key"] for item in spot["required_custom_state"]} == {
        "system_version",
        "derisk_level_1",
    }
    assert spot["identity"]["program_fingerprint"] == _fingerprint(spot)
    assert futures["identity"]["program_fingerprint"] == _fingerprint(futures)


def test_required_input_types_match_fixed_vocabulary_and_comparison_operands() -> None:
    for program in (_compile(SPOT), _compile(FUTURES, "futures")):
        declarations = {
            (item["entrypoint"], item["name"]): item["type"]["kind"]
            for item in program["required_inputs"]
        }
        assert declarations == {
            ("bot_loop_start", "callback_dataframe"): "record",
            ("bot_loop_start", "callback_dataframe_empty"): "bool",
            ("bot_loop_start", "last_visible_timestamp_seconds"): "f64",
            ("bot_loop_start", "visible_rows"): "i64",
            ("confirm_trade_exit", "config.trading_mode"): "string",
        }
        comparisons = [
            item for item in _walk(program["entrypoints"]["confirm_trade_exit"])
            if item.get("op") == "compare"
        ]
        mode_comparison = next(
            item for item in comparisons
            if item["left"] == {"op": "read_input", "name": "config.trading_mode"}
        )
        assert mode_comparison["comparisons"][0]["right"] == {"op": "literal", "value": "spot"}


def test_input_declaration_contradiction_fails_validation() -> None:
    program = _compile(SPOT)
    declaration = next(
        item for item in program["required_inputs"] if item["name"] == "config.trading_mode"
    )
    declaration["type"] = {"kind": "f64"}
    program["identity"]["program_fingerprint"] = _fingerprint(program)
    with pytest.raises(StrategyAnalysisError, match="CALLBACK_PROGRAM_REGISTER_TYPE_CONFLICT"):
        validate_executable_callback_program(program)


def test_compilation_is_deterministic_and_path_diagnostic_is_not_identity(tmp_path: Path) -> None:
    first = _compile(SPOT)
    assert first == _compile(SPOT)
    copied = tmp_path / "renamed_source.py"
    copied.write_bytes(SPOT.read_bytes())
    second = _compile(copied)
    assert first["identity"]["program_fingerprint"] == second["identity"]["program_fingerprint"]
    assert first["identity"]["source_closure"] != second["identity"]["source_closure"]


def test_register_observation_helper_and_transaction_lowering() -> None:
    program = _compile(SPOT)
    assert {item["type"]["kind"] for item in program["registers"]} == {"i64"}
    ops = _ops(program["entrypoints"])
    assert {
        "set_register",
        "emit_observation",
        "read_custom_state",
        "set_custom_state",
        "read_trade",
        "read_order",
        "raise_callback",
    } <= set(ops)
    adjust = program["entrypoints"]["adjust_trade_position"]
    assert adjust["transaction_policy"] == {
        "ordinary_trade": "commit_on_success_rollback_on_exception",
        "scheduler_prior": "preserve",
        "shared_custom_state": "commit_executed_writes",
        "strategy_registers": "commit_executed_writes",
    }
    observations = [item for item in _walk(adjust) if item.get("op") == "emit_observation"]
    assert observations and observations[0]["channel"] == "strategy_stdout_json"


def _walk(value: object) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        result.append(value)
        for child in value.values():
            result.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_walk(child))
    return result


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("dynamic", "CALLBACK_PROGRAM_DYNAMIC_CUSTOM_STATE_KEY"),
        ("while", "CALLBACK_PROGRAM_UNBOUNDED_CONTROL_FLOW"),
        ("recursion", "CALLBACK_PROGRAM_UNBOUNDED_CONTROL_FLOW"),
        ("unknown_call", "CALLBACK_PROGRAM_UNSUPPORTED_EXPRESSION"),
    ],
)
def test_fail_closed_source_constructs(tmp_path: Path, mutation: str, code: str) -> None:
    source = tmp_path / "bad.py"
    text = SPOT.read_text(encoding="utf-8")
    if mutation == "dynamic":
        text = text.replace(
            'trade.set_custom_data("derisk_level_1", True)', "trade.set_custom_data(pair, True)"
        )
    elif mutation == "while":
        text = text.replace(
            "        eligible = self.adjustments >= 4\n",
            "        while current_profit:\n"
            "            current_profit -= 1\n"
            "        eligible = self.adjustments >= 4\n",
        )
    elif mutation == "recursion":
        text = text.replace(
            "        return value\n\n    def confirm_trade_exit",
            "        return self.custom_exit(pair, trade, current_time, current_rate, "
            "current_profit)\n\n    def confirm_trade_exit",
        )
    else:
        text = text.replace(
            "        return value\n\n    def confirm_trade_exit",
            "        return open(pair)\n\n    def confirm_trade_exit",
        )
    source.write_text(text, encoding="utf-8")
    analysis = analyze_strategy(source, class_name="Task14CallbackOracle")
    execution = compile_callback_execution_ir(analysis, trading_mode="spot", run_mode="backtest")
    with pytest.raises(StrategyAnalysisError, match=code):
        compile_executable_callback_program(
            analysis, execution, trading_mode="spot", run_mode="backtest"
        )


def test_identity_staleness_and_program_mutations_fail_closed() -> None:
    analysis = analyze_strategy(SPOT, class_name="Task14CallbackOracle")
    execution = compile_callback_execution_ir(analysis, trading_mode="spot", run_mode="backtest")
    stale = copy.deepcopy(execution)
    stale["fingerprint"] = "0" * 64
    with pytest.raises(StrategyAnalysisError, match="CALLBACK_PROGRAM_EXECUTION_IR_STALE"):
        compile_executable_callback_program(
            analysis, stale, trading_mode="spot", run_mode="backtest"
        )
    program = _compile(SPOT)
    program["entrypoints"]["custom_exit"]["instructions"][0]["op"] = "unknown"
    with pytest.raises(StrategyAnalysisError, match="CALLBACK_PROGRAM_UNKNOWN_OPCODE"):
        validate_executable_callback_program(program)
    program = _compile(SPOT)
    program["identity"]["callback_contract_fingerprint"] = "0" * 64
    with pytest.raises(StrategyAnalysisError, match="CALLBACK_PROGRAM_CONTRACT_IDENTITY_MISMATCH"):
        validate_executable_callback_program(program)


def test_source_coverage_unknown_callback_and_register_type_fail_closed(tmp_path: Path) -> None:
    copied = tmp_path / "strategy.py"
    copied.write_bytes(SPOT.read_bytes())
    analysis = analyze_strategy(copied, class_name="Task14CallbackOracle")
    execution = compile_callback_execution_ir(analysis, trading_mode="spot", run_mode="backtest")
    copied.write_text(copied.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(StrategyAnalysisError, match="CALLBACK_PROGRAM_SOURCE_STALE"):
        compile_executable_callback_program(
            analysis, execution, trading_mode="spot", run_mode="backtest"
        )

    analysis = analyze_strategy(SPOT, class_name="Task14CallbackOracle")
    execution = compile_callback_execution_ir(analysis, trading_mode="spot", run_mode="backtest")
    analysis["strategies"][0]["strategy_callbacks"].append("custom_roi")
    with pytest.raises(StrategyAnalysisError, match="CALLBACK_PROGRAM_UNKNOWN_CALLBACK"):
        compile_executable_callback_program(
            analysis, execution, trading_mode="spot", run_mode="backtest"
        )

    text = SPOT.read_text(encoding="utf-8").replace(
        "        self.adjustments += 1", '        self.adjustments = "bad"'
    )
    copied.write_text(text, encoding="utf-8")
    analysis = analyze_strategy(copied, class_name="Task14CallbackOracle")
    execution = compile_callback_execution_ir(analysis, trading_mode="spot", run_mode="backtest")
    with pytest.raises(StrategyAnalysisError, match="CALLBACK_PROGRAM_REGISTER_TYPE_CONFLICT"):
        compile_executable_callback_program(
            analysis, execution, trading_mode="spot", run_mode="backtest"
        )


def test_no_forbidden_identity_routing_in_compiler_sources() -> None:
    text = "\n".join(
        (ROOT / "python/nfi_backtest_engine" / name).read_text(encoding="utf-8")
        for name in (
            "executable_callback_program.py",
            "executable_callback_compiler.py",
            "executable_callback_expressions.py",
            "executable_callback_validation.py",
        )
    ).lower()
    for forbidden in (
        "task14callbackoracle",
        "current-callback-oracle",
        "basename",
        "timerange",
        "expected_output",
        "expected-result",
        "allowlist",
    ):
        assert forbidden not in text
