from __future__ import annotations

import ast
from dataclasses import dataclass

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.x7.managed_exit_ir import compile_managed_exit_ir
from nfi_backtest_engine.x7.routes import _method_ast_sha256


@dataclass(frozen=True)
class _Spec:
    key: str
    profile: str
    method: str


_SPECS = (
    _Spec("normal", "normal", "long_exit_normal"),
    _Spec("pump", "pump", "long_exit_pump"),
    _Spec("quick", "quick", "long_exit_quick"),
    _Spec("profit", "high-profit", "long_exit_profit"),
)
_CONSTANTS = {
    "normal_tags": ["1", "2"],
    "pump_tags": ["21"],
    "quick_tags": ["41"],
    "profit_tags": ["81"],
    "normal_name": "normal",
    "pump_name": "pump",
    "quick_name": "quick",
    "profit_name": "profit",
}


def _methods(
    *,
    swap_routes: bool = False,
    swap_decisions: bool = False,
) -> dict[str, ast.FunctionDef]:
    first, second = ("pump", "normal") if swap_routes else ("normal", "pump")
    decisions = (
        "self.decision_b, self.decision_a"
        if swap_decisions
        else "self.decision_a, self.decision_b"
    )
    source = f'''
class Strategy:
    def custom_exit(self, trade):
        enter_tag = trade.enter_tag
        enter_tags = enter_tag.split()
        normal_tags = self.normal_tags
        pump_tags = self.pump_tags
        quick_tags = self.quick_tags
        profit_tags = self.profit_tags
        if any(c in {first}_tags for c in enter_tags):
            sell, signal_name = long_exit_{first}()
            if sell and signal_name is not None:
                return f"{{signal_name}} ( {{enter_tag}})"
        if any(c in {second}_tags for c in enter_tags):
            sell, signal_name = long_exit_{second}()
            if sell and signal_name is not None:
                return f"{{signal_name}} ( {{enter_tag}})"
        if any(c in quick_tags for c in enter_tags):
            sell, signal_name = self.long_exit_quick()
            if sell and signal_name is not None:
                return f"{{signal_name}} ( {{enter_tag}})"
        if any(c in profit_tags for c in enter_tags):
            sell, signal_name = self.long_exit_profit()
            if sell and signal_name is not None:
                return f"{{signal_name}} ( {{enter_tag}})"

    def long_exit_normal(self, profit_init_ratio):
        mode_name = self.normal_name
        sell = False
        if profit_init_ratio > 0.0:
            sell, signal_name = self.decision_a(profit_init_ratio)
            if not sell:
                sell, signal_name = self.decision_b(profit_init_ratio)
        if not sell:
            sell, signal_name = self.stop_policy()
        return sell, signal_name

    def long_exit_pump(self, profit_init_ratio):
        mode_name = self.pump_name
        sell = False
        if profit_init_ratio > 0.0:
            checks = ({decisions},)
            for exit_check in checks:
                sell, signal_name = exit_check(profit_init_ratio)
                if sell:
                    break
        if not sell:
            sell, signal_name = self.stop_policy()
        return sell, signal_name

    def long_exit_quick(self, profit_init_ratio):
        mode_name = self.quick_name
        sell = False
        if profit_init_ratio > 0.0:
            for exit_check in (self.decision_a, self.decision_b):
                sell, signal_name = exit_check(profit_init_ratio)
                if sell:
                    break
        if not sell:
            sell, signal_name = self.quick_policy()
        return sell, signal_name

    def long_exit_profit(self, profit_init_ratio):
        mode_name = self.profit_name
        sell = False
        for exit_func in (self.decision_a, self.decision_b):
            sell, signal_name = exit_func(profit_init_ratio)
            if sell:
                break
        if not sell:
            sell, signal_name = self.stop_policy()
        return sell, signal_name
'''
    tree = ast.parse(source)
    class_node = tree.body[0]
    assert isinstance(class_node, ast.ClassDef)
    return {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef)
    }


def _compile(**kwargs: bool):
    return compile_managed_exit_ir(
        _methods(**kwargs),
        _CONSTANTS,
        _SPECS,
        legacy_route_methods={},
    )


def test_basic_exit_ir_reads_route_and_decision_source_order() -> None:
    compiled = _compile()

    assert compiled.long_route_order == ("normal", "pump", "quick", "profit")
    routes = compiled.program["routes"]
    assert [route["id"] for route in routes] == ["normal", "pump", "quick", "profit"]
    assert routes[0]["match"] == {"operator": "any", "entry_tags": ["1", "2"]}
    assert routes[0]["initial_profit_gate"] == {
        "operator": "greater-than",
        "value": 0.0,
    }
    assert routes[0]["profit_basis"] == "initial-stake"
    assert routes[-1]["initial_profit_gate"] is None
    assert routes[1]["decision_program_order"] == ["decision_a", "decision_b"]


def test_basic_exit_ir_changes_as_source_order_changes() -> None:
    original = _compile()
    changed = _compile(swap_routes=True, swap_decisions=True)

    assert changed.long_route_order[:2] == ("pump", "normal")
    assert changed.program["routes"][0]["id"] == "pump"
    assert changed.program["routes"][0]["decision_program_order"] == [
        "decision_b",
        "decision_a",
    ]
    assert changed.program["fingerprint"] != original.program["fingerprint"]


def test_compiled_prefix_mask_keeps_the_stateful_remainder_identity_bound() -> None:
    methods = _methods()
    compiled = _compile()
    original = methods["long_exit_normal"]
    modified = _methods()["long_exit_normal"]
    gate_index = next(iter(compiled.wrapper_statement_indices["long_exit_normal"]))
    gate = modified.body[gate_index]
    assert isinstance(gate, ast.If)
    gate.body.reverse()

    assert _method_ast_sha256(original) != _method_ast_sha256(modified)
    assert _method_ast_sha256(
        original,
        remove_statement_indices=frozenset({gate_index}),
    ) == _method_ast_sha256(
        modified,
        remove_statement_indices=frozenset({gate_index}),
    )


def test_basic_exit_ir_rejects_an_unknown_long_route() -> None:
    methods = _methods()
    custom_exit = methods["custom_exit"]
    custom_exit.body.append(
        ast.parse(
            "if any(c in mystery_tags for c in enter_tags):\n"
            "    sell, signal_name = self.long_exit_mystery()\n"
        ).body[0]
    )

    with pytest.raises(StrategyAnalysisError, match="unclassified long routes"):
        compile_managed_exit_ir(
            methods,
            _CONSTANTS,
            _SPECS,
            legacy_route_methods={},
        )


def test_special_exit_ir_compiles_compound_matcher_and_current_stake_basis() -> None:
    source = '''
class Strategy:
    def custom_exit(self, enter_tags, enter_tag):
        rebuy_tags = self.rebuy_tags
        compound_tags = self.compound_tags
        if all(c in rebuy_tags for c in enter_tags) or (
            any(c in rebuy_tags for c in enter_tags)
            and all(c in compound_tags for c in enter_tags)
        ):
            sell, signal_name = self.long_exit_rebuy()
            if sell and signal_name is not None:
                return f"{signal_name} ( {enter_tag})"

    def long_exit_rebuy(self, profit_current_stake_ratio):
        mode_name = self.rebuy_name
        signal_args = (mode_name, profit_current_stake_ratio)
        sell = False
        for exit_func in (self.decision_a, self.decision_b):
            sell, signal_name = exit_func(*signal_args)
            if sell:
                break
        if not sell:
            sell, signal_name = self.stateful_stop()
        return sell, signal_name
'''
    tree = ast.parse(source)
    class_node = tree.body[0]
    assert isinstance(class_node, ast.ClassDef)
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef)
    }
    compiled = compile_managed_exit_ir(
        methods,
        {
            "rebuy_tags": ["61", "62"],
            "compound_tags": ["61", "62", "120"],
            "rebuy_name": "long_rebuy",
        },
        (_Spec("rebuy", "rebuy", "long_exit_rebuy"),),
        legacy_route_methods={},
    )

    route = compiled.program["routes"][0]
    assert route["match"] == {
        "operator": "any-of",
        "operands": [
            {"operator": "all", "entry_tags": ["61", "62"]},
            {
                "operator": "all-of",
                "operands": [
                    {"operator": "any", "entry_tags": ["61", "62"]},
                    {"operator": "all", "entry_tags": ["61", "62", "120"]},
                ],
            },
        ],
    }
    assert route["profit_basis"] == "current-stake"
    assert route["decision_program_order"] == ["decision_a", "decision_b"]
