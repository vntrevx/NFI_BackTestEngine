"""Compile the source-ordered, pure prefix of managed exit routes.

The existing X7 adapter still owns stop and profit-target state while the
generic lane is introduced in stages.  This compiler removes the first source
dependency from that adapter: tag dispatch and the ordered pure-decision
prefix are read from the supplied strategy AST instead of selected by a
reviewed wrapper hash.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from ..errors import StrategyAnalysisError

MANAGED_EXIT_PROGRAM_VERSION = "managed-exit-program-v1"


class _RouteSpec(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def profile(self) -> str: ...

    @property
    def method(self) -> str: ...


@dataclass(frozen=True)
class ManagedExitCompilation:
    """Executable IR plus AST regions replaced by that IR."""

    program: dict[str, Any]
    long_route_order: tuple[str, ...]
    custom_exit_statement_indices: frozenset[int]
    wrapper_statement_indices: Mapping[str, frozenset[int]]


def compile_managed_exit_ir(
    methods: Mapping[str, ast.FunctionDef],
    constants: Mapping[str, Any],
    route_specs: Sequence[_RouteSpec],
    *,
    legacy_route_methods: Mapping[str, str],
) -> ManagedExitCompilation:
    """Compile managed-long tag routes and their pure decision prefixes.

    A supported route must be a top-level ``custom_exit`` branch composed from
    bounded ``any``/``all`` tag predicates. Its wrapper must expose an ordered
    tuple-return decision prefix. Everything outside the returned AST masks
    remains covered by the legacy stateful-method identity gate.
    """

    custom_exit = methods.get("custom_exit")
    if custom_exit is None:
        raise StrategyAnalysisError("managed exit IR requires custom_exit")
    route_key_by_method = {
        **{spec.method: spec.key for spec in route_specs},
        **legacy_route_methods,
    }
    aliases = _self_aliases(custom_exit)
    discovered: list[tuple[int, str, ast.If, dict[str, Any] | None]] = []
    unknown_long_routes: set[str] = set()
    seen_keys: set[str] = set()
    for index, statement in enumerate(custom_exit.body):
        if not isinstance(statement, ast.If):
            continue
        called = _assigned_exit_callbacks(statement, aliases)
        long_calls = [name for name in called if name.startswith("long_exit_")]
        for method_name in long_calls:
            key = route_key_by_method.get(method_name)
            if key is None:
                unknown_long_routes.add(method_name)
                continue
            # The fallback normal branch calls the same wrapper after all
            # explicit long routes.  Only the first occurrence is a route.
            if key in seen_keys:
                continue
            seen_keys.add(key)
            discovered.append(
                (
                    index,
                    key,
                    statement,
                    _compile_tag_matcher(statement.test, aliases, constants),
                )
            )
    if unknown_long_routes:
        raise StrategyAnalysisError(
            "NFI custom_exit contains unclassified long routes: "
            + ", ".join(sorted(unknown_long_routes))
        )

    long_route_order = tuple(record[1] for record in discovered)
    routes: list[dict[str, Any]] = []
    custom_masks: set[int] = set()
    wrapper_masks: dict[str, frozenset[int]] = {}
    by_key = {key: (index, statement, matcher) for index, key, statement, matcher in discovered}
    for spec in route_specs:
        found = by_key.get(spec.key)
        if found is None:
            raise StrategyAnalysisError(f"NFI managed exit route is missing: {spec.key}")
        custom_index, branch, matcher = found
        if matcher is None:
            raise StrategyAnalysisError(f"NFI {spec.key} tag matcher cannot be represented")

        wrapper = methods.get(spec.method)
        if wrapper is None:
            raise StrategyAnalysisError(f"NFI managed exit wrapper is missing: {spec.method}")
        _validate_dispatch_branch(branch, spec.method, aliases)
        programs, profit_gate, mask_indices = _compile_decision_prefix(wrapper)
        mode_name = _mode_name(wrapper, constants)
        routes.append(
            {
                "id": spec.key,
                "source_order": len(routes),
                "match": matcher,
                "initial_profit_gate": profit_gate,
                "profit_basis": _decision_profit_basis(wrapper, programs),
                "mode_name": mode_name,
                "decision_program_order": programs,
                "location": _location(branch),
            }
        )
        custom_masks.add(custom_index)
        wrapper_masks[spec.method] = mask_indices

    basic_ids = {route["id"] for route in routes}
    expected_basic_order = [key for key in long_route_order if key in basic_ids]
    actual_basic_order = [route["id"] for route in routes]
    if actual_basic_order != expected_basic_order:
        routes_by_id = {route["id"]: route for route in routes}
        routes = [routes_by_id[key] for key in expected_basic_order]
        for index, route in enumerate(routes):
            route["source_order"] = index

    program: dict[str, Any] = {
        "schema_version": MANAGED_EXIT_PROGRAM_VERSION,
        "execution_mode": "shadow",
        "routes": routes,
    }
    encoded = json.dumps(
        program,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    program["fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return ManagedExitCompilation(
        program=program,
        long_route_order=long_route_order,
        custom_exit_statement_indices=frozenset(custom_masks),
        wrapper_statement_indices=wrapper_masks,
    )


def _self_aliases(method: ast.FunctionDef) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for statement in method.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Attribute)
            and isinstance(statement.value.value, ast.Name)
            and statement.value.value.id == "self"
        ):
            aliases[statement.targets[0].id] = statement.value.attr
    return aliases


def _call_name(call: ast.Call, aliases: Mapping[str, str]) -> str | None:
    if (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
    ):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return aliases.get(call.func.id, call.func.id)
    return None


def _assigned_exit_callbacks(node: ast.AST, aliases: Mapping[str, str]) -> list[str]:
    result: list[str] = []
    for item in ast.walk(node):
        if not isinstance(item, ast.Assign) or not isinstance(item.value, ast.Call):
            continue
        if not _sell_signal_target(item.targets):
            continue
        name = _call_name(item.value, aliases)
        if name is not None:
            result.append(name)
    return result


def _sell_signal_target(targets: Sequence[ast.expr]) -> bool:
    if len(targets) != 1 or not isinstance(targets[0], ast.Tuple):
        return False
    names = [item.id for item in targets[0].elts if isinstance(item, ast.Name)]
    return names == ["sell", "signal_name"]


def _compile_tag_matcher(
    node: ast.AST,
    aliases: Mapping[str, str],
    constants: Mapping[str, Any],
) -> dict[str, Any] | None:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And | ast.Or):
        operands = [_compile_tag_matcher(value, aliases, constants) for value in node.values]
        if any(operand is None for operand in operands):
            return None
        return {
            "operator": "all-of" if isinstance(node.op, ast.And) else "any-of",
            "operands": [cast(dict[str, Any], operand) for operand in operands],
        }
    leaf = _tag_matcher_leaf(node, aliases)
    if leaf is None:
        return None
    operator, constant_name = leaf
    raw_tags = constants.get(constant_name)
    if (
        not isinstance(raw_tags, list | tuple)
        or not raw_tags
        or not all(isinstance(tag, str) and tag for tag in raw_tags)
    ):
        raise StrategyAnalysisError(
            f"NFI managed exit matcher {constant_name} is not a static string list"
        )
    tags = list(dict.fromkeys(cast(Sequence[str], raw_tags)))
    if len(tags) != len(raw_tags):
        raise StrategyAnalysisError(
            f"NFI managed exit matcher {constant_name} contains duplicate tags"
        )
    return {"operator": operator, "entry_tags": tags}


def _tag_matcher_leaf(
    node: ast.AST,
    aliases: Mapping[str, str],
) -> tuple[str, str] | None:
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or node.func.id not in {"any", "all"}
        or len(node.args) != 1
        or node.keywords
        or not isinstance(node.args[0], ast.GeneratorExp)
    ):
        return None
    generator = node.args[0]
    if len(generator.generators) != 1 or generator.generators[0].ifs:
        return None
    clause = generator.generators[0]
    if (
        not isinstance(clause.target, ast.Name)
        or not isinstance(clause.iter, ast.Name)
        or clause.iter.id != "enter_tags"
        or not isinstance(generator.elt, ast.Compare)
        or len(generator.elt.ops) != 1
        or not isinstance(generator.elt.ops[0], ast.In)
        or not isinstance(generator.elt.left, ast.Name)
        or generator.elt.left.id != clause.target.id
        or len(generator.elt.comparators) != 1
        or not isinstance(generator.elt.comparators[0], ast.Name)
    ):
        return None
    local_name = generator.elt.comparators[0].id
    return node.func.id, aliases.get(local_name, local_name)


def _compile_decision_prefix(
    wrapper: ast.FunctionDef,
) -> tuple[list[str], dict[str, Any] | None, frozenset[int]]:
    callable_tuples = _callable_tuples(wrapper)
    for index, statement in enumerate(wrapper.body):
        gate = _positive_profit_gate(statement)
        if gate is None:
            continue
        programs = _ordered_decision_calls(statement, callable_tuples)
        if programs:
            assert isinstance(statement, ast.If)
            _validate_decision_body(
                statement.body,
                callable_tuples,
                frozenset(programs),
            )
            return programs, gate, frozenset({index})
    for index, statement in enumerate(wrapper.body):
        if isinstance(statement, ast.For):
            programs = _for_programs(statement, callable_tuples)
            if programs and _loop_assigns_sell_signal(statement):
                _validate_decision_body(
                    [statement],
                    callable_tuples,
                    frozenset(programs),
                )
                return programs, None, frozenset({index})
    programs, indices = _top_level_decision_prefix(wrapper, callable_tuples)
    if programs:
        return programs, None, indices
    raise StrategyAnalysisError(
        f"NFI {wrapper.name} pure decision prefix cannot be represented"
    )


def _top_level_decision_prefix(
    wrapper: ast.FunctionDef,
    callable_tuples: Mapping[str, list[str]],
) -> tuple[list[str], frozenset[int]]:
    programs: list[str] = []
    indices: set[int] = set()
    for index, statement in enumerate(wrapper.body):
        call = _guarded_sell_signal_call(statement)
        if call is None:
            if programs:
                break
            continue
        name = _call_name(call, {})
        if name is None or _uses_order_or_stake_state(call):
            break
        _validate_decision_body([statement], callable_tuples, frozenset({name}))
        programs.append(name)
        indices.add(index)
    return _unique_in_order(programs), frozenset(indices)


def _guarded_sell_signal_call(statement: ast.stmt) -> ast.Call | None:
    assignment: ast.stmt = statement
    if isinstance(statement, ast.If):
        if (
            not _is_not_sell_test(statement.test)
            or statement.orelse
            or len(statement.body) != 1
        ):
            return None
        assignment = statement.body[0]
    if (
        isinstance(assignment, ast.Assign)
        and _sell_signal_target(assignment.targets)
        and isinstance(assignment.value, ast.Call)
    ):
        return assignment.value
    return None


def _uses_order_or_stake_state(call: ast.Call) -> bool:
    return any(
        isinstance(node, ast.Name)
        and node.id
        in {
            "filled_orders",
            "filled_entries",
            "filled_exits",
            "profit_stake",
            "profit_ratio",
            "profit_current_stake_ratio",
        }
        for node in ast.walk(call)
    )


def _validate_dispatch_branch(
    branch: ast.If,
    wrapper_method: str,
    aliases: Mapping[str, str],
) -> None:
    if len(branch.body) != 2 or branch.orelse:
        raise StrategyAnalysisError(f"NFI {wrapper_method} dispatch body changed")
    assignment, guard = branch.body
    if (
        not isinstance(assignment, ast.Assign)
        or not _sell_signal_target(assignment.targets)
        or not isinstance(assignment.value, ast.Call)
        or _call_name(assignment.value, aliases) != wrapper_method
        or not isinstance(guard, ast.If)
        or guard.orelse
        or len(guard.body) != 1
        or not _sell_reason_guard(guard.test)
        or not isinstance(guard.body[0], ast.Return)
        or not _exit_reason_template(guard.body[0].value)
    ):
        raise StrategyAnalysisError(f"NFI {wrapper_method} dispatch body changed")


def _sell_reason_guard(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.And)
        and len(node.values) == 2
        and isinstance(node.values[0], ast.Name)
        and node.values[0].id == "sell"
        and isinstance(node.values[1], ast.Compare)
        and isinstance(node.values[1].left, ast.Name)
        and node.values[1].left.id == "signal_name"
        and len(node.values[1].ops) == 1
        and isinstance(node.values[1].ops[0], ast.IsNot)
        and len(node.values[1].comparators) == 1
        and isinstance(node.values[1].comparators[0], ast.Constant)
        and node.values[1].comparators[0].value is None
    )


def _exit_reason_template(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.JoinedStr) or len(node.values) != 4:
        return False
    signal, opening, entry_tag, closing = node.values
    return (
        isinstance(signal, ast.FormattedValue)
        and isinstance(signal.value, ast.Name)
        and signal.value.id == "signal_name"
        and isinstance(opening, ast.Constant)
        and opening.value == " ( "
        and isinstance(entry_tag, ast.FormattedValue)
        and isinstance(entry_tag.value, ast.Name)
        and entry_tag.value.id == "enter_tag"
        and isinstance(closing, ast.Constant)
        and closing.value == ")"
    )


def _validate_decision_body(
    statements: Sequence[ast.stmt],
    callable_tuples: Mapping[str, list[str]],
    programs: frozenset[str],
    *,
    loop_variables: frozenset[str] = frozenset(),
) -> None:
    for statement in statements:
        if isinstance(statement, ast.Assign):
            if (
                len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and statement.targets[0].id in callable_tuples
                and isinstance(statement.value, ast.Tuple)
            ):
                continue
            if (
                _sell_signal_target(statement.targets)
                and isinstance(statement.value, ast.Call)
                and _call_name(statement.value, {}) in programs | loop_variables
            ):
                continue
        elif isinstance(statement, ast.If) and not statement.orelse:
            if _is_sell_test(statement.test) and all(
                isinstance(child, ast.Break) for child in statement.body
            ):
                continue
            if _is_not_sell_test(statement.test):
                _validate_decision_body(
                    statement.body,
                    callable_tuples,
                    programs,
                    loop_variables=loop_variables,
                )
                continue
        elif (
            isinstance(statement, ast.For)
            and isinstance(statement.target, ast.Name)
            and not statement.orelse
            and _for_programs(statement, callable_tuples)
        ):
            _validate_decision_body(
                statement.body,
                callable_tuples,
                programs,
                loop_variables=loop_variables | {statement.target.id},
            )
            continue
        raise StrategyAnalysisError("NFI managed exit decision prefix contains new state")


def _is_sell_test(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "sell"


def _is_not_sell_test(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and _is_sell_test(node.operand)
    )


def _positive_profit_gate(node: ast.stmt) -> dict[str, Any] | None:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return None
    compare = node.test
    if (
        not isinstance(compare.left, ast.Name)
        or compare.left.id != "profit_init_ratio"
        or len(compare.ops) != 1
        or not isinstance(compare.ops[0], ast.Gt)
        or len(compare.comparators) != 1
    ):
        return None
    value = _number(compare.comparators[0])
    if value is None:
        return None
    return {"operator": "greater-than", "value": value}


def _callable_tuples(method: ast.FunctionDef) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    aliases = _self_aliases(method)
    for statement in ast.walk(method):
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Tuple)
        ):
            values = [_callable_reference(item, aliases) for item in statement.value.elts]
            if values and all(value is not None for value in values):
                result[statement.targets[0].id] = [cast(str, value) for value in values]
    return result


def _callable_reference(node: ast.AST, aliases: Mapping[str, str]) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    return None


def _ordered_decision_calls(
    node: ast.AST,
    callable_tuples: Mapping[str, list[str]],
) -> list[str]:
    direct: list[tuple[int, str]] = []
    for item in ast.walk(node):
        if (
            isinstance(item, ast.Assign)
            and isinstance(item.value, ast.Call)
            and _sell_signal_target(item.targets)
        ):
            name = _call_name(item.value, {})
            if name is not None and name not in {"exit_check", "exit_func"}:
                direct.append((item.lineno, name))
        elif isinstance(item, ast.For) and _loop_assigns_sell_signal(item):
            for order, name in enumerate(_for_programs(item, callable_tuples)):
                direct.append((item.lineno * 1_000 + order, name))
    return _unique_in_order(name for _, name in sorted(direct))


def _for_programs(
    node: ast.For,
    callable_tuples: Mapping[str, list[str]],
) -> list[str]:
    if isinstance(node.iter, ast.Name):
        return list(callable_tuples.get(node.iter.id, []))
    if isinstance(node.iter, ast.Tuple):
        values = [_callable_reference(item, {}) for item in node.iter.elts]
        if values and all(value is not None for value in values):
            return [cast(str, value) for value in values]
    return []


def _loop_assigns_sell_signal(node: ast.For) -> bool:
    return any(
        isinstance(item, ast.Assign)
        and isinstance(item.value, ast.Call)
        and _sell_signal_target(item.targets)
        for item in ast.walk(node)
    )


def _decision_profit_basis(wrapper: ast.FunctionDef, programs: Sequence[str]) -> str:
    tuple_values: dict[str, ast.Tuple] = {}
    for statement in wrapper.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Tuple)
        ):
            tuple_values[statement.targets[0].id] = statement.value

    bases: set[str] = set()
    for node in ast.walk(wrapper):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node, {})
        if name not in programs and name not in {"exit_check", "exit_func"}:
            continue
        expanded_arguments: list[ast.AST] = []
        for argument in node.args:
            if isinstance(argument, ast.Starred) and isinstance(argument.value, ast.Name):
                tuple_node = tuple_values.get(argument.value.id)
                if tuple_node is None:
                    continue
                expanded_arguments.extend(tuple_node.elts)
            else:
                expanded_arguments.append(argument)
        arguments: Iterable[ast.AST] = expanded_arguments
        for argument in arguments:
            for item in ast.walk(argument):
                if isinstance(item, ast.Name) and item.id == "profit_init_ratio":
                    bases.add("initial-stake")
                elif (
                    isinstance(item, ast.Name)
                    and item.id == "profit_current_stake_ratio"
                ):
                    bases.add("current-stake")
        if bases:
            break
    if len(bases) != 1:
        raise StrategyAnalysisError(
            f"NFI {wrapper.name} decision profit basis cannot be represented"
        )
    return bases.pop()


def _mode_name(wrapper: ast.FunctionDef, constants: Mapping[str, Any]) -> str:
    for statement in wrapper.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id in {"mode", "mode_name"}
            and isinstance(statement.value, ast.Attribute)
            and isinstance(statement.value.value, ast.Name)
            and statement.value.value.id == "self"
        ):
            value = constants.get(statement.value.attr)
            if isinstance(value, str) and value:
                return value
    raise StrategyAnalysisError(f"NFI {wrapper.name} mode name is not a static string")


def _number(node: ast.AST) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        value = _number(node.operand)
        if value is not None:
            return -value if isinstance(node.op, ast.USub) else value
    return None


def _unique_in_order(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _location(node: ast.AST) -> dict[str, int]:
    return {
        "line": getattr(node, "lineno", 1),
        "column": getattr(node, "col_offset", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "end_column": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    }
