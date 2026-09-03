from __future__ import annotations

import ast
import copy
from functools import cache
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.x7.adjustment_ir import compile_system_adjustment_ir
from nfi_backtest_engine.x7.adjustments import (
    _build_adjustment_constants,
    _resolve_fallback_feature_aliases,
)

_SOURCE = Path(
    "benchmarks/fixtures/captured/"
    "x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20/inputs/strategy.py"
)


@cache
def _inputs() -> tuple[dict[str, ast.FunctionDef], dict[str, object]]:
    analysis = analyze_strategy(_SOURCE, class_name="NostalgiaForInfinityX7")
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    strategy = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7"
    )
    methods = {node.name: node for node in strategy.body if isinstance(node, ast.FunctionDef)}
    return methods, analysis["strategies"][0]["constants"]


def _compile(
    method: ast.FunctionDef | None = None,
    exit_method: ast.FunctionDef | None = None,
) -> dict[str, object]:
    methods, constants = _inputs()
    method = method or methods["long_grind_adjust_trade_position_v3"]
    exit_method = exit_method or methods["long_grind_exit_v3"]
    descriptor = _build_adjustment_constants(constants, method, side="long")
    return compile_system_adjustment_ir(
        method,
        exit_method,
        constants,
        side="long",
        retry_policy=descriptor["policy"],
    )


def _compile_short(
    method: ast.FunctionDef | None = None,
    exit_method: ast.FunctionDef | None = None,
) -> dict[str, object]:
    methods, constants = _inputs()
    method = method or methods["short_grind_adjust_trade_position_v3"]
    exit_method = exit_method or methods["short_grind_exit_v3"]
    descriptor = _build_adjustment_constants(constants, method, side="short")
    return compile_system_adjustment_ir(
        method,
        exit_method,
        constants,
        side="short",
        retry_policy=descriptor["policy"],
    )


def _without_maximum_state(
    method: ast.FunctionDef,
    *,
    remove_arguments: bool = True,
) -> ast.FunctionDef:
    changed = copy.deepcopy(method)
    changed.body = [
        statement
        for statement in changed.body
        if not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get_custom_data", "set_custom_data"}
            and any(
                keyword.arg == "key"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
                and keyword.value.value.startswith("grind_")
                and "_cluster_max_profit_" in keyword.value.value
                for keyword in node.keywords
            )
            for node in ast.walk(statement)
        )
    ]
    if remove_arguments:
        for node in ast.walk(changed):
            if isinstance(node, ast.Call):
                node.args = [
                    argument
                    for argument in node.args
                    if not (
                        isinstance(argument, ast.Name)
                        and argument.id.startswith("grind_")
                        and "_cluster_max_profit_" in argument.id
                    )
                ]
    return changed


def _without_maximum_parameters(method: ast.FunctionDef) -> ast.FunctionDef:
    changed = copy.deepcopy(method)
    changed.args.args = [
        argument
        for argument in changed.args.args
        if argument.arg not in {"max_profit", "max_profit_rate"}
    ]
    ast.fix_missing_locations(changed)
    return changed


def _with_grind_enable_aliases(method: ast.FunctionDef) -> ast.FunctionDef:
    changed = copy.deepcopy(method)
    aliases: list[ast.stmt] = []
    for level in range(1, 6):
        name = f"system_v3_grind_{level}_enable"
        tag = f"grind_{level}_entry"
        branch = next(
            statement
            for statement in changed.body
            if isinstance(statement, ast.If)
            and any(
                isinstance(node, ast.Constant) and node.value == tag
                for node in ast.walk(statement)
            )
            and isinstance(statement.test, ast.BoolOp)
            and isinstance(statement.test.values[0], ast.Attribute)
            and statement.test.values[0].attr == name
        )
        branch.test.values[0] = ast.Name(id=name, ctx=ast.Load())
        aliases.extend(ast.parse(f"{name} = self.{name}\n").body)
    changed.body[:0] = aliases
    ast.fix_missing_locations(changed)
    return changed


def _with_exit_method_alias(
    method: ast.FunctionDef,
    *,
    target: str = "long_grind_exit_v3",
) -> ast.FunctionDef:
    changed = copy.deepcopy(method)

    class _ExitAliasWriter(ast.NodeTransformer):
        def visit_Call(self, node: ast.Call) -> ast.AST:
            updated = self.generic_visit(node)
            if (
                isinstance(updated, ast.Call)
                and isinstance(updated.func, ast.Attribute)
                and isinstance(updated.func.value, ast.Name)
                and updated.func.value.id == "self"
                and updated.func.attr == "long_grind_exit_v3"
            ):
                updated.func = ast.Name(id="long_grind_exit_v3", ctx=ast.Load())
            return updated

    changed = _ExitAliasWriter().visit(changed)
    changed.body[:0] = ast.parse(f"long_grind_exit_v3 = self.{target}\n").body
    ast.fix_missing_locations(changed)
    return changed


def test_adjustment_accepts_exact_local_exit_method_alias() -> None:
    methods, _constants = _inputs()

    direct = _compile()
    aliased = _compile(
        method=_with_exit_method_alias(methods["long_grind_adjust_trade_position_v3"])
    )

    assert aliased == direct


def test_adjustment_rejects_changed_local_exit_method_alias() -> None:
    methods, _constants = _inputs()
    changed = _with_exit_method_alias(
        methods["long_grind_adjust_trade_position_v3"],
        target="long_grind_entry_v3",
    )

    with pytest.raises(StrategyAnalysisError, match="method alias changed"):
        _compile(method=changed)


def _fallback_alias_method() -> ast.FunctionDef:
    method = ast.parse(
        "def callback(self, last_candle, trade):\n"
        '    last_rsi_3 = last_candle["RSI_3"]\n'
        "    is_futures_mode = self.is_futures_mode\n"
        "    trade_is_short = trade.is_short\n"
        "    trade_liquidation_price = trade.liquidation_price\n"
    ).body[0]
    assert isinstance(method, ast.FunctionDef)
    return method


def test_adjustment_resolves_exact_fallback_aliases_to_source_operands() -> None:
    source = ast.parse(
        "last_rsi_3 > 10.0"
        " and is_futures_mode"
        " and trade_liquidation_price is not None"
        " and (trade_is_short or not trade_is_short)",
        mode="eval",
    ).body
    expected = ast.parse(
        'last_candle["RSI_3"] > 10.0'
        " and self.is_futures_mode"
        " and trade.liquidation_price is not None"
        " and (trade.is_short or not trade.is_short)",
        mode="eval",
    ).body

    resolved = _resolve_fallback_feature_aliases(
        _fallback_alias_method(),
        source,
        level=5,
    )

    assert ast.dump(resolved, include_attributes=False) == ast.dump(
        expected,
        include_attributes=False,
    )


def test_adjustment_rejects_changed_fallback_runtime_alias() -> None:
    method = _fallback_alias_method()
    assignment = next(
        statement
        for statement in method.body
        if isinstance(statement, ast.Assign)
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "trade_is_short"
    )
    assert isinstance(assignment.value, ast.Attribute)
    assignment.value.attr = "liquidation_price"
    source = ast.parse("trade_is_short", mode="eval").body

    with pytest.raises(StrategyAnalysisError, match="fallback alias changed: trade_is_short"):
        _resolve_fallback_feature_aliases(method, source, level=5)


def test_adjustment_rejects_non_feature_fallback_alias() -> None:
    method = _fallback_alias_method()
    assignment = next(
        statement
        for statement in method.body
        if isinstance(statement, ast.Assign)
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "last_rsi_3"
    )
    assignment.value = ast.Name(id="slice_profit", ctx=ast.Load())
    source = ast.parse("last_rsi_3 > 10.0", mode="eval").body

    with pytest.raises(StrategyAnalysisError, match="fallback alias changed: last_rsi_3"):
        _resolve_fallback_feature_aliases(method, source, level=4)


def test_adjustment_accepts_exact_local_grind_enable_aliases() -> None:
    methods, constants = _inputs()
    method = methods["long_grind_adjust_trade_position_v3"]

    direct = _build_adjustment_constants(constants, method, side="long")
    aliased = _build_adjustment_constants(
        constants,
        _with_grind_enable_aliases(method),
        side="long",
    )

    assert aliased["policy"] == direct["policy"]


def test_adjustment_rejects_changed_local_grind_enable_alias() -> None:
    methods, constants = _inputs()
    changed = _with_grind_enable_aliases(methods["long_grind_adjust_trade_position_v3"])
    assignment = next(
        statement
        for statement in changed.body
        if isinstance(statement, ast.Assign)
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "system_v3_grind_1_enable"
    )
    assert isinstance(assignment.value, ast.Attribute)
    assignment.value.attr = "system_v3_grind_2_enable"

    with pytest.raises(StrategyAnalysisError, match="grind 1 enable alias changed"):
        _build_adjustment_constants(constants, changed, side="long")


def test_long_adjustment_compiles_source_order_tags_and_dynamic_levels() -> None:
    program = _compile()
    actions = program["source_order"]

    assert program["schema_version"] == "system-adjustment-program-v2"
    assert program["side"] == "long"
    assert program["execution_mode"] == "primary"
    assert len(actions) == 19
    assert [record["level"] for record in program["order_scan"]["grind_levels"]] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert [
        record["minimum_scale_leverage"] for record in program["order_scan"]["grind_levels"]
    ] == [
        "trade-leverage",
        "market-mode-leverage",
        "market-mode-leverage",
        "market-mode-leverage",
        "market-mode-leverage",
    ]
    assert [(record["kind"], record["level"]) for record in actions[:7]] == [
        ("derisk", 1),
        ("derisk", 2),
        ("derisk", 3),
        ("derisk", 4),
        ("grind-entry", 1),
        ("grind-exit", 1),
        ("grind-derisk", 1),
    ]
    assert actions[4]["tag"] == "grind_1_entry"
    assert actions[5]["append_entry_ids"] is True
    assert ["literal", "return-none"] in actions[4]["decision_program"]["expressions"]
    assert ["literal", None] not in actions[4]["decision_program"]["expressions"]
    assert "RSI_3" in program["input_contract"]["indexed_fields"]["last_candle"]

    assert [
        (
            record["maximum_profit_stake_key"],
            record["maximum_profit_rate_key"],
        )
        for record in program["order_scan"]["grind_levels"]
    ] == [
        (
            f"grind_{level}_cluster_max_profit_stake",
            f"grind_{level}_cluster_max_profit_rate",
        )
        for level in range(1, 6)
    ]


@pytest.mark.parametrize(
    ("side", "method_name"),
    [
        ("long", "long_grind_adjust_trade_position_v3"),
        ("short", "short_grind_adjust_trade_position_v3"),
    ],
)
def test_adjustment_compiles_complete_absence_of_maximum_state(
    side: str,
    method_name: str,
) -> None:
    methods, _constants = _inputs()
    original = _compile() if side == "long" else _compile_short()
    changed = _without_maximum_state(methods[method_name])
    changed_exit = _without_maximum_parameters(methods[f"{side}_grind_exit_v3"])

    program = (
        _compile(method=changed, exit_method=changed_exit)
        if side == "long"
        else _compile_short(method=changed, exit_method=changed_exit)
    )

    assert program["source_order"] == original["source_order"]
    original_scan = copy.deepcopy(original["order_scan"])
    absent_scan = copy.deepcopy(program["order_scan"])
    for record in original_scan["grind_levels"]:
        record.pop("maximum_profit_stake_key")
        record.pop("maximum_profit_rate_key")
    for record in absent_scan["grind_levels"]:
        assert record.pop("maximum_profit_stake_key") is None
        assert record.pop("maximum_profit_rate_key") is None
    assert absent_scan == original_scan


def test_adjustment_rejects_residual_maximum_helper_arguments() -> None:
    methods, _constants = _inputs()
    changed = _without_maximum_state(
        methods["long_grind_adjust_trade_position_v3"],
        remove_arguments=False,
    )

    with pytest.raises(StrategyAnalysisError, match="residual maximum state"):
        _compile(method=changed)


def test_adjustment_rejects_maximum_state_on_unknown_level() -> None:
    methods, _constants = _inputs()
    changed = copy.deepcopy(methods["long_grind_adjust_trade_position_v3"])
    changed.body[:0] = ast.parse(
        "grind_6_cluster_max_profit_stake = "
        "trade.get_custom_data(key='grind_6_cluster_max_profit_stake') or 0.0\n"
        "if grind_6_current_grind_profit_stake > "
        "grind_6_cluster_max_profit_stake:\n"
        "    trade.set_custom_data("
        "key='grind_6_cluster_max_profit_stake', "
        "value=grind_6_current_grind_profit_stake)\n"
    ).body

    with pytest.raises(StrategyAnalysisError, match="maximum levels changed"):
        _compile(method=changed)


def test_adjustment_rejects_exit_argument_removal_without_signature_change() -> None:
    methods, _constants = _inputs()
    changed = _without_maximum_state(methods["long_grind_adjust_trade_position_v3"])

    with pytest.raises(StrategyAnalysisError, match="exit arguments changed"):
        _compile(method=changed)


@pytest.mark.parametrize("mutation", ["partial", "renamed"])
def test_adjustment_rejects_ambiguous_maximum_state(mutation: str) -> None:
    methods, _constants = _inputs()
    changed = copy.deepcopy(methods["long_grind_adjust_trade_position_v3"])
    target = "grind_3_cluster_max_profit_rate"
    if mutation == "partial":
        index = next(
            index
            for index, statement in enumerate(changed.body)
            if isinstance(statement, ast.If)
            and any(
                isinstance(node, ast.Constant) and node.value == target
                for node in ast.walk(statement)
            )
        )
        del changed.body[index]
    else:
        for node in ast.walk(changed):
            if isinstance(node, ast.Constant) and node.value == target:
                node.value = f"{target}_renamed"

    with pytest.raises(
        StrategyAnalysisError,
        match="maximum (?:reads|keys) changed|custom state shape changed",
    ):
        _compile(method=changed)


def test_long_adjustment_stake_literal_mutation_recompiles_without_hash_gate() -> None:
    methods, _constants = _inputs()
    changed = copy.deepcopy(methods["long_grind_adjust_trade_position_v3"])
    branch = next(
        node
        for node in changed.body
        if isinstance(node, ast.If)
        and any(
            isinstance(item, ast.Constant) and item.value == "grind_1_entry"
            for item in ast.walk(node)
        )
    )
    literals = [
        node for node in ast.walk(branch) if isinstance(node, ast.Constant) and node.value == 1.5
    ]
    assert len(literals) == 2
    for literal in literals:
        literal.value = 1.65

    original = _compile()
    mutated = _compile(method=changed)
    assert mutated["fingerprint"] != original["fingerprint"]
    assert (
        mutated["source_order"][4]["decision_program"]
        != (original["source_order"][4]["decision_program"])
    )


def test_long_adjustment_exit_condition_mutation_recompiles() -> None:
    methods, _constants = _inputs()
    changed_exit = copy.deepcopy(methods["long_grind_exit_v3"])
    threshold = next(
        node
        for node in ast.walk(changed_exit)
        if isinstance(node, ast.Constant) and node.value == 99.0
    )
    threshold.value = 98.0

    original = _compile()
    mutated = _compile(exit_method=changed_exit)
    assert mutated["fingerprint"] != original["fingerprint"]
    assert (
        mutated["source_order"][5]["decision_program"]
        != (original["source_order"][5]["decision_program"])
    )


def test_long_adjustment_exit_wrapper_condition_is_compiled() -> None:
    methods, _constants = _inputs()
    changed = copy.deepcopy(methods["long_grind_adjust_trade_position_v3"])
    wrapper = next(
        statement
        for statement in changed.body
        if isinstance(statement, ast.If)
        and any(
            isinstance(node, ast.Constant) and node.value == "grind_1_exit"
            for node in ast.walk(statement)
        )
    )
    wrapper.test = ast.BoolOp(
        op=ast.And(),
        values=[
            wrapper.test,
            ast.Compare(
                left=ast.Name(id="grind_1_current_grind_profit_rate", ctx=ast.Load()),
                ops=[ast.GtE()],
                comparators=[
                    ast.BinOp(
                        left=ast.BinOp(
                            left=ast.Name(id="grind_1_profit_threshold", ctx=ast.Load()),
                            op=ast.Add(),
                            right=ast.Name(id="fee_open_rate", ctx=ast.Load()),
                        ),
                        op=ast.Add(),
                        right=ast.Name(id="fee_close_rate", ctx=ast.Load()),
                    )
                ],
            ),
        ],
    )
    ast.fix_missing_locations(changed)

    original = _compile()
    mutated = _compile(method=changed)
    action = mutated["source_order"][5]
    bindings = {binding["name"]: binding for binding in action["bindings"]}

    assert mutated["fingerprint"] != original["fingerprint"]
    assert action["decision_program"] != original["source_order"][5]["decision_program"]
    assert bindings["grind_1_current_grind_profit_rate"]["kind"] == "cluster-profit-rate"
    assert bindings["grind_1_profit_threshold"]["kind"] == "cluster-profit-threshold"
    assert bindings["fee_open_rate"]["kind"] == "fee-open-rate"
    assert bindings["fee_close_rate"]["kind"] == "fee-close-rate"


def test_long_adjustment_ignores_config_read_used_only_by_observability() -> None:
    methods, _constants = _inputs()
    changed_exit = copy.deepcopy(methods["long_grind_exit_v3"])
    reporting_branch = next(
        node
        for node in ast.walk(changed_exit)
        if isinstance(node, ast.If)
        and any(isinstance(item, ast.Name) and item.id == "stake_fmt" for item in ast.walk(node))
    )
    reporting_branch.body.insert(
        0,
        ast.Assign(
            targets=[ast.Name(id="stake_currency", ctx=ast.Store())],
            value=ast.Subscript(
                value=ast.Attribute(
                    value=ast.Name(id="self", ctx=ast.Load()),
                    attr="config",
                    ctx=ast.Load(),
                ),
                slice=ast.Constant(value="stake_currency"),
                ctx=ast.Load(),
            ),
        ),
    )
    reporting_branch.body.insert(
        1,
        ast.If(
            test=ast.Name(id="send_notifications", ctx=ast.Load()),
            body=[
                ast.Expr(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Attribute(
                                value=ast.Name(id="self", ctx=ast.Load()),
                                attr="dp",
                                ctx=ast.Load(),
                            ),
                            attr="send_msg",
                            ctx=ast.Load(),
                        ),
                        args=[ast.Name(id="stake_currency", ctx=ast.Load())],
                        keywords=[],
                    )
                )
            ],
            orelse=[],
        ),
    )
    ast.fix_missing_locations(changed_exit)

    original = _compile()
    changed = _compile(exit_method=changed_exit)

    assert (
        changed["source_order"][5]["decision_program"]
        == (original["source_order"][5]["decision_program"])
    )


def test_long_adjustment_binds_source_level_derisk_enablement_generically() -> None:
    methods, _constants = _inputs()
    changed = copy.deepcopy(methods["long_grind_adjust_trade_position_v3"])
    first_derisk = next(
        node
        for node in changed.body
        if isinstance(node, ast.If)
        and any(
            isinstance(item, ast.Constant) and item.value == "derisk_level_1"
            for item in ast.walk(node)
        )
    )
    first_derisk.test = ast.BoolOp(
        op=ast.And(),
        values=[
            ast.Name(id="derisk_enable", ctx=ast.Load()),
            ast.Name(id="derisk_1_enable", ctx=ast.Load()),
            ast.Compare(
                left=ast.Name(id="derisk_1_threshold", ctx=ast.Load()),
                ops=[ast.Lt()],
                comparators=[ast.Constant(value=0.0)],
            ),
            ast.Compare(
                left=ast.Name(id="derisk_1_stake", ctx=ast.Load()),
                ops=[ast.Gt()],
                comparators=[ast.Constant(value=0.0)],
            ),
            first_derisk.test,
        ],
    )
    ast.fix_missing_locations(changed)

    program = _compile(method=changed)
    first_action = program["source_order"][0]

    bindings = {binding["name"]: binding for binding in first_action["bindings"]}
    assert bindings["derisk_enable"] == {
        "name": "derisk_enable",
        "kind": "derisk-enabled-global",
    }
    assert bindings["derisk_1_enable"] == {
        "name": "derisk_1_enable",
        "kind": "derisk-enabled",
        "level": 1,
    }
    assert bindings["derisk_1_stake"] == {
        "name": "derisk_1_stake",
        "kind": "derisk-stake",
        "level": 1,
    }
    assert bindings["derisk_1_threshold"] == {
        "name": "derisk_1_threshold",
        "kind": "derisk-threshold",
        "level": 1,
    }


def test_long_adjustment_changed_source_order_fails_closed() -> None:
    methods, _constants = _inputs()
    changed = copy.deepcopy(methods["long_grind_adjust_trade_position_v3"])
    indexes = [
        index
        for index, node in enumerate(changed.body)
        if isinstance(node, ast.If)
        and any(
            isinstance(item, ast.Constant) and item.value in {"grind_1_entry", "grind_1_derisk"}
            for item in ast.walk(node)
        )
    ]
    assert len(indexes) == 2
    changed.body[indexes[0]], changed.body[indexes[1]] = (
        changed.body[indexes[1]],
        changed.body[indexes[0]],
    )

    with pytest.raises(StrategyAnalysisError, match="Grind source order changed"):
        _compile(method=changed)


def test_long_adjustment_runtime_hash_gates_are_retired() -> None:
    import nfi_backtest_engine.x7.trade_manager as trade_manager

    assert not hasattr(trade_manager, "_ADJUSTMENT_METHOD_SHA256")


def test_short_adjustment_is_compiled_from_its_independent_directional_ast() -> None:
    long_program = _compile()
    short_program = _compile_short()

    assert short_program["side"] == "short"
    assert len(short_program["source_order"]) == 18
    assert short_program["order_scan"]["entry_order_side"] == "sell"
    assert short_program["order_scan"]["exit_order_side"] == "buy"
    assert [record["level"] for record in short_program["order_scan"]["derisk_tags"]] == [1, 2, 3]
    assert [record["level"] for record in short_program["order_scan"]["grind_levels"]] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert [
        (
            record["maximum_profit_stake_key"],
            record["maximum_profit_rate_key"],
        )
        for record in short_program["order_scan"]["grind_levels"]
    ] == [
        (
            f"grind_{level}_cluster_max_profit_stake",
            f"grind_{level}_cluster_max_profit_rate",
        )
        for level in range(1, 6)
    ]
    assert short_program["fingerprint"] != long_program["fingerprint"]
    assert "BBL_20_2.0" in short_program["input_contract"]["indexed_fields"]["last_candle"]
    assert "BBU_20_2.0" not in short_program["input_contract"]["indexed_fields"]["last_candle"]


def test_short_exit_mutation_recompiles_without_changing_long_program() -> None:
    methods, _constants = _inputs()
    long_fingerprint = _compile()["fingerprint"]
    changed_exit = copy.deepcopy(methods["short_grind_exit_v3"])
    threshold = next(
        node
        for node in ast.walk(changed_exit)
        if isinstance(node, ast.Constant) and node.value == 1.0
    )
    threshold.value = 2.0

    original_short = _compile_short()
    mutated_short = _compile_short(exit_method=changed_exit)
    assert mutated_short["fingerprint"] != original_short["fingerprint"]
    assert _compile()["fingerprint"] == long_fingerprint
