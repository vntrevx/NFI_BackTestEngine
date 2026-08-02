from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.trade_ir import build_trade_dependency_ir
from nfi_backtest_engine.x7.managed_exit_ir import (
    _compile_state_policy,
    compile_managed_exit_ir,
)
from nfi_backtest_engine.x7.managed_short_exit_ir import compile_managed_short_exit_ir
from nfi_backtest_engine.x7.trade_manager import (
    _MANAGED_LONG_ROUTE_SPECS,
    _MANAGED_LONG_STATEFUL_STEPS,
    _MANAGED_SHORT_ROUTE_SPECS,
    _MANAGED_SHORT_STATEFUL_STEPS,
    build_nfi_trade_manager_ir,
)


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
        include_state_program=False,
    )


def test_basic_exit_ir_reads_route_and_decision_source_order() -> None:
    compiled = _compile()

    assert compiled.program["execution_mode"] == "primary"
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


def test_exit_runtime_uses_structural_helpers_without_hash_gates() -> None:
    import nfi_backtest_engine.x7.trade_manager as trade_manager

    long_wrappers = {spec.method for spec in _MANAGED_LONG_ROUTE_SPECS}
    short_wrappers = {spec.method for spec in _MANAGED_SHORT_ROUTE_SPECS}

    assert long_wrappers.isdisjoint(_MANAGED_LONG_STATEFUL_STEPS)
    assert short_wrappers.isdisjoint(_MANAGED_SHORT_STATEFUL_STEPS)
    assert set(_MANAGED_LONG_STATEFUL_STEPS) == {
        "long_exit_stoploss",
        "exit_profit_target",
        "mark_profit_target",
        "_set_profit_target",
        "_remove_profit_target",
    }
    assert set(_MANAGED_SHORT_STATEFUL_STEPS) == {"short_exit_stoploss"}
    for name in (
        "_MANAGED_LONG_METHOD_SHA256",
        "_MANAGED_SHORT_METHOD_SHA256",
        "_LONG_GRIND_METHOD_SHA256",
        "_LONG_BTC_METHOD_SHA256",
    ):
        assert not hasattr(trade_manager, name)


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
            include_state_program=False,
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
        include_state_program=False,
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


def test_managed_exit_state_is_source_compiled_for_all_long_routes() -> None:
    source = Path(
        "benchmarks/fixtures/captured/"
        "x7-futures-lifecycle-long-v17.4.435-2022-04-10_04-20/inputs/strategy.py"
    )
    analysis = analyze_strategy(source, class_name="NostalgiaForInfinityX7")
    constants = analysis["strategies"][0]["constants"]
    tree = ast.parse(source.read_text(encoding="utf-8"))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7"
    )
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef)
    }

    quick = _compile_state_policy(methods["long_exit_quick"], constants)
    rapid = _compile_state_policy(methods["long_exit_rapid"], constants)
    rebuy = _compile_state_policy(methods["long_exit_rebuy"], constants)
    high_profit = _compile_state_policy(methods["long_exit_high_profit"], constants)
    scalp = _compile_state_policy(methods["long_exit_scalp"], constants)

    assert quick["inline_exit"]["position"] == "after-stop"
    assert quick["inline_exit"]["minimum_profit"] == 0.02
    # Literal-only arithmetic is folded before the managed-exit program is sealed.
    assert len(quick["inline_exit"]["program"]["expressions"]) == 86
    assert rapid["inline_exit"]["position"] == "before-stop"
    assert rapid["inline_exit"]["minimum_profit"] == 0.005
    assert rebuy["stop"] == {
        "kind": "stake-threshold",
        "enabled": False,
        "futures_threshold": 1.4,
        "spot_threshold": 0.48,
        "divide_by_leverage": True,
    }
    assert high_profit["target"]["max_target_floor"] == 0.03
    assert high_profit["target"]["suppress_protected_exit"] is False
    assert scalp["target"]["pure_scalp_trailing"] is True

    changed = copy.deepcopy(methods["long_exit_quick"])
    mutated = False
    for node in ast.walk(changed):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "profit_ratio"
            and isinstance(node.comparators[0], ast.BinOp)
        ):
            right = node.comparators[0].right
            if isinstance(right, ast.Constant) and right.value == 0.001:
                right.value = 0.002
                mutated = True
                break
    assert mutated
    changed_policy = _compile_state_policy(changed, constants)
    assert changed_policy["target"]["u_e_raise_delta"] == 0.002
    assert changed_policy != quick


def test_managed_short_exit_ir_compiles_its_own_routes_state_and_fallback() -> None:
    source = Path(
        "benchmarks/fixtures/captured/"
        "x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20/inputs/strategy.py"
    )
    analysis = analyze_strategy(source, class_name="NostalgiaForInfinityX7")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7"
    )
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef)
    }

    compiled = compile_managed_short_exit_ir(
        methods,
        analysis["strategies"][0]["constants"],
        _MANAGED_SHORT_ROUTE_SPECS,
    )
    routes = {route["id"]: route for route in compiled.program["routes"]}

    assert compiled.program["execution_mode"] == "primary"
    assert compiled.short_route_order == (
        "short_normal",
        "short_pump",
        "short_quick",
        "short_rebuy",
        "short_high_profit",
        "short_rapid",
        "short_scalp",
        "short_top_coins_fallback",
    )
    assert routes["short_rebuy"]["profit_basis"] == "current-stake"
    assert routes["short_quick"]["state_program"]["inline_exit"]["position"] == (
        "after-stop"
    )
    assert routes["short_rapid"]["state_program"]["inline_exit"]["position"] == (
        "before-stop"
    )
    assert routes["short_scalp"]["match"]["operator"] == "any-of"
    assert routes["short_scalp"]["state_program"]["target"][
        "pure_scalp_matcher"
    ] == {"operator": "all", "entry_tags": ["661"]}
    assert routes["short_top_coins_fallback"]["match"] == {
        "operator": "all-of",
        "operands": [
            {"operator": "is-short"},
            {
                "operator": "not",
                "operands": [
                    {
                        "operator": "any",
                        "entry_tags": analysis["strategies"][0]["constants"][
                            "short_exit_known_mode_tags"
                        ],
                    }
                ],
            },
        ],
    }

    changed_tree = ast.parse(source.read_text(encoding="utf-8"))
    changed_class = next(
        node
        for node in changed_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7"
    )
    changed_methods = {
        node.name: node
        for node in changed_class.body
        if isinstance(node, ast.FunctionDef)
    }
    changed = changed_methods["short_exit_quick"]
    threshold = next(
        node
        for node in ast.walk(changed)
        if isinstance(node, ast.Constant) and node.value == 22.0
    )
    threshold.value = 21.0
    changed_compilation = compile_managed_short_exit_ir(
        changed_methods,
        analysis["strategies"][0]["constants"],
        _MANAGED_SHORT_ROUTE_SPECS,
    )
    changed_routes = {
        route["id"]: route for route in changed_compilation.program["routes"]
    }
    assert (
        changed_routes["short_quick"]["state_program"]["inline_exit"]["program"]
        != routes["short_quick"]["state_program"]["inline_exit"]["program"]
    )


def test_changed_short_wrapper_builds_without_a_route_hash_gate(tmp_path: Path) -> None:
    source = Path(
        "benchmarks/fixtures/captured/"
        "x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20/inputs/strategy.py"
    )
    text = source.read_text(encoding="utf-8")
    old = "(last_rsi_14 < 22.0):\n        sell, signal_name = True, f\"exit_{mode_name}_q_1\""
    new = "(last_rsi_14 < 21.0):\n        sell, signal_name = True, f\"exit_{mode_name}_q_1\""
    assert text.count(old) == 1
    changed_source = tmp_path / "NostalgiaForInfinityX7.py"
    changed_source.write_text(text.replace(old, new), encoding="utf-8")

    analysis = analyze_strategy(changed_source, class_name="NostalgiaForInfinityX7")
    manager = build_nfi_trade_manager_ir(
        analysis,
        build_trade_dependency_ir(analysis),
    )

    assert manager is not None
    operation = manager["operation"]
    assert operation["schema_version"] == "0.29.0"
    assert operation["managed_short_exit_program"]["execution_mode"] == "primary"


def test_new_uncompiled_short_exit_result_fails_closed() -> None:
    source = Path(
        "benchmarks/fixtures/captured/"
        "x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20/inputs/strategy.py"
    )
    analysis = analyze_strategy(source, class_name="NostalgiaForInfinityX7")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7"
    )
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef)
    }
    wrapper = methods["short_exit_quick"]
    wrapper.body.insert(
        -1,
        ast.parse(
            'if not sell:\n    sell, signal_name = True, "new_uncompiled_exit"\n'
        ).body[0],
    )

    with pytest.raises(StrategyAnalysisError, match="uncompiled direct sell result"):
        compile_managed_short_exit_ir(
            methods,
            analysis["strategies"][0]["constants"],
            _MANAGED_SHORT_ROUTE_SPECS,
        )
