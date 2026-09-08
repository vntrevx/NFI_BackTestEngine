from __future__ import annotations

import ast
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.trade_ir import compile_scalar_ast_program
from nfi_backtest_engine.x7.legacy_entry import source_legacy_entry_program


def _methods() -> dict[str, ast.FunctionDef]:
    cls = ast.parse("""
class Source:
    def callback(self):
        is_short_grind_entry = self.source_entry(last_candle, previous_candle, slice_profit, True)
        return is_short_grind_entry
    def source_entry(self, last_candle, previous_candle, slice_profit, is_derisk):
        return last_candle["entry"] and is_derisk
""").body[0]
    assert isinstance(cls, ast.ClassDef)
    return {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}


def test_legacy_entry_uses_the_source_callee_without_a_generation_alias() -> None:
    methods = _methods()
    assert source_legacy_entry_program(methods["callback"], methods) == "source_entry"


@pytest.mark.parametrize("mutation", ["flag", "arguments", "overwrite", "signature"])
def test_unrepresented_legacy_entry_call_fails_closed(mutation: str) -> None:
    methods = _methods()
    callback = methods["callback"]
    call = callback.body[0].value
    assert isinstance(call, ast.Call)
    if mutation == "flag":
        call.args[-1] = ast.Constant(False)
    elif mutation == "arguments":
        call.args[0], call.args[1] = call.args[1], call.args[0]
    elif mutation == "overwrite":
        callback.body.append(ast.parse("is_short_grind_entry = True").body[0])
    else:
        methods["source_entry"].args.args[-1].arg = "another_flag"
    with pytest.raises(StrategyAnalysisError, match="legacy entry decision"):
        source_legacy_entry_program(callback, methods)


def test_complete_donor_legacy_entry_has_the_native_scalar_input_contract() -> None:
    path = Path(
        "benchmarks/fixtures/captured/x8-original-futures-20220401-20220420-r1/inputs/strategy.py"
    )
    cls = next(node for node in ast.parse(path.read_text()).body if isinstance(node, ast.ClassDef))
    methods = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}
    name = source_legacy_entry_program(methods["short_grind_adjust_trade_position"], methods)
    assert name == "short_grind_entry"
    program = compile_scalar_ast_program(methods[name], constants={})
    assert program["parameters"] == [
        "last_candle",
        "previous_candle",
        "slice_profit",
        "is_derisk",
    ]
