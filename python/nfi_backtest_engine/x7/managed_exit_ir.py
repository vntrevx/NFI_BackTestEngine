"""Compile the source-ordered, pure prefix of managed exit routes.

The existing X7 adapter still owns stop and profit-target state while the
generic lane is introduced in stages.  This compiler removes the first source
dependency from that adapter: tag dispatch and the ordered pure-decision
prefix are read from the supplied strategy AST instead of selected by a
reviewed wrapper hash.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from ..errors import StrategyAnalysisError
from ..trade_ir import compile_scalar_ast_program

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
    terminal_exits: Mapping[str, Mapping[str, Any]] | None = None,
    include_state_program: bool = True,
) -> ManagedExitCompilation:
    """Compile managed-long tag routes and their pure decision prefixes.

    A supported route must be a top-level ``custom_exit`` branch composed from
    bounded ``any``/``all`` tag predicates. Its wrapper must expose an ordered
    tuple-return decision prefix. Everything outside the returned AST masks
    remains covered by the legacy stateful-method identity gate.
    """

    terminal_exits = terminal_exits or {}
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
        route = {
            "id": spec.key,
            "source_order": len(routes),
            "match": matcher,
            "initial_profit_gate": profit_gate,
            "profit_basis": _decision_profit_basis(wrapper, programs),
            "mode_name": mode_name,
            "decision_program_order": programs,
            "terminal_exit": terminal_exits.get(spec.key),
            "location": _location(branch),
        }
        if include_state_program:
            route["state_program"] = _compile_state_policy(
                wrapper,
                constants,
                target_helper=methods.get("exit_profit_target"),
                side="long",
            )
        routes.append(route)
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
    ):
        return None
    comparator = generator.elt.comparators[0]
    if isinstance(comparator, ast.Name):
        local_name = comparator.id
    elif (
        isinstance(comparator, ast.Attribute)
        and isinstance(comparator.value, ast.Name)
        and comparator.value.id == "self"
    ):
        local_name = comparator.attr
    else:
        return None
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


def _compile_state_policy(
    wrapper: ast.FunctionDef,
    constants: Mapping[str, Any],
    *,
    target_helper: ast.FunctionDef | None = None,
    side: str | None = None,
) -> dict[str, Any]:
    inline_exit = _inline_exit_policy(wrapper)
    stop = _stop_policy(wrapper, constants)
    stateful_order = ["stop"]
    if inline_exit is not None:
        if inline_exit["position"] == "before-stop":
            stateful_order.insert(0, "inline-exit")
        else:
            stateful_order.append("inline-exit")
    stateful_order.extend(
        [
            "existing-target",
            "target-update",
            "final-filter",
            "terminal-exit",
        ]
    )
    return {
        "stateful_order": stateful_order,
        "inline_exit": inline_exit,
        "stop": stop,
        "target": {
            "u_e_raise_delta": _u_e_raise_delta(wrapper),
            "profit_raise_delta": _profit_raise_delta(wrapper),
            "max_target_floor": _max_target_floor(wrapper),
            "protected_reentry_guard": _has_protected_reentry_guard(wrapper),
            "suppress_protected_exit": _suppresses_protected_exit(wrapper),
            "pure_scalp_trailing": _uses_pure_scalp_trailing(wrapper),
            "pure_scalp_matcher": (
                _pure_scalp_matcher(target_helper, side, constants)
                if _uses_pure_scalp_trailing(wrapper)
                and target_helper is not None
                and side is not None
                else None
            ),
        },
    }


def _stop_policy(
    wrapper: ast.FunctionDef,
    constants: Mapping[str, Any],
) -> dict[str, Any]:
    aliases = _self_aliases(wrapper)
    common_calls = [
        node
        for node in ast.walk(wrapper)
        if isinstance(node, ast.Call)
        and _call_name(node, aliases) in {"long_exit_stoploss", "short_exit_stoploss"}
    ]
    if common_calls:
        helpers = {_call_name(node, aliases) for node in common_calls}
        if len(helpers) != 1:
            raise StrategyAnalysisError(
                f"NFI {wrapper.name} calls multiple managed stop helpers"
            )
        return {"kind": "source-helper", "helper": helpers.pop()}

    threshold_names = {
        node.attr
        for node in ast.walk(wrapper)
        if isinstance(node, ast.Attribute)
        and node.attr.startswith("system_v3_2_stop_threshold_")
    }
    futures_names = sorted(name for name in threshold_names if "_futures" in name)
    spot_names = sorted(name for name in threshold_names if "_spot" in name)
    if len(futures_names) != 1 or len(spot_names) != 1:
        raise StrategyAnalysisError(f"NFI {wrapper.name} stop policy cannot be represented")
    futures = constants.get(futures_names[0])
    spot = constants.get(spot_names[0])
    enabled = constants.get("system_v3_2_stops_enable")
    if (
        isinstance(futures, bool)
        or not isinstance(futures, int | float)
        or isinstance(spot, bool)
        or not isinstance(spot, int | float)
        or not isinstance(enabled, bool)
    ):
        raise StrategyAnalysisError(f"NFI {wrapper.name} stop constants are invalid")
    return {
        "kind": "stake-threshold",
        "enabled": enabled,
        "futures_threshold": float(futures),
        "spot_threshold": float(spot),
        "divide_by_leverage": _stop_divides_by_leverage(wrapper),
    }


def _stop_divides_by_leverage(wrapper: ast.FunctionDef) -> bool:
    for node in ast.walk(wrapper):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        divides_by_leverage = any(
            isinstance(item, ast.Name) and item.id in {"leverage", "trade_leverage"}
            for item in ast.walk(node.right)
        )
        reads_threshold = any(
            (isinstance(item, ast.Name) and "threshold" in item.id)
            or (isinstance(item, ast.Attribute) and "threshold" in item.attr)
            for item in ast.walk(node.left)
        )
        if divides_by_leverage and reads_threshold:
            return True
    return False


def _inline_exit_policy(wrapper: ast.FunctionDef) -> dict[str, Any] | None:
    suffix_family: str | None = None
    suffix_line: int | None = None
    for node in ast.walk(wrapper):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("q_"):
                suffix_family = "q"
                suffix_line = min(suffix_line or node.lineno, node.lineno)
        elif isinstance(node, ast.JoinedStr):
            fragments = "".join(
                str(value.value)
                for value in node.values
                if isinstance(value, ast.Constant)
            )
            if "_q_" in fragments:
                suffix_family = "q"
                suffix_line = min(suffix_line or node.lineno, node.lineno)
            elif "_rpd_" in fragments:
                suffix_family = "rpd"
                suffix_line = min(suffix_line or node.lineno, node.lineno)
    if suffix_family is None or suffix_line is None:
        return None

    bounds = _inline_profit_bounds(wrapper, suffix_line)
    stop_lines = [
        node.lineno
        for node in ast.walk(wrapper)
        if isinstance(node, ast.Call)
        and _call_name(node, {}) in {"long_exit_stoploss", "short_exit_stoploss"}
    ]
    if not stop_lines:
        stop_lines = [
            node.lineno
            for node in ast.walk(wrapper)
            if isinstance(node, ast.Compare)
            and any(
                isinstance(item, ast.Name) and item.id == "profit_stake"
                for item in ast.walk(node)
            )
        ]
    if not stop_lines:
        raise StrategyAnalysisError(f"NFI {wrapper.name} inline/stop order cannot be represented")
    return {
        "position": "before-stop" if suffix_line < min(stop_lines) else "after-stop",
        **bounds,
        "program": _compile_inline_exit_program(wrapper, suffix_family),
    }


class _InlineNameSubstitution(ast.NodeTransformer):
    def __init__(self, replacements: Mapping[str, ast.expr]) -> None:
        self.replacements = replacements

    def visit_Name(self, node: ast.Name) -> ast.expr:
        replacement = self.replacements.get(node.id)
        if replacement is None or not isinstance(node.ctx, ast.Load):
            return node
        return self.visit(copy.deepcopy(replacement))


def _compile_inline_exit_program(
    wrapper: ast.FunctionDef,
    suffix_family: str,
) -> dict[str, Any]:
    assignments = {
        target.id: statement
        for statement in ast.walk(wrapper)
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance((target := statement.targets[0]), ast.Name)
    }
    tuple_candidates: list[tuple[str, ast.Assign, ast.Tuple]] = []
    for name, statement in assignments.items():
        value = statement.value
        if not isinstance(value, ast.Tuple) or not value.elts:
            continue
        pairs = [item for item in value.elts if isinstance(item, ast.Tuple) and len(item.elts) == 2]
        if len(pairs) != len(value.elts):
            continue
        rendered = ast.unparse(value)
        if (suffix_family == "q" and "q_" in rendered) or (
            suffix_family == "rpd" and "_rpd_" in rendered
        ):
            tuple_candidates.append((name, statement, value))
    if not tuple_candidates:
        return _compile_direct_inline_exit_program(wrapper, suffix_family)
    if len(tuple_candidates) != 1:
        raise StrategyAnalysisError(f"NFI {wrapper.name} inline decisions cannot be represented")
    tuple_name, tuple_statement, decision_tuple = tuple_candidates[0]

    loop = next(
        (
            node
            for node in ast.walk(wrapper)
            if isinstance(node, ast.For)
            and isinstance(node.iter, ast.Name)
            and node.iter.id == tuple_name
            and isinstance(node.target, ast.Tuple)
            and len(node.target.elts) == 2
            and all(isinstance(item, ast.Name) for item in node.target.elts)
        ),
        None,
    )
    if loop is None:
        raise StrategyAnalysisError(f"NFI {wrapper.name} inline decision loop is missing")
    loop_target = loop.target
    if not isinstance(loop_target, ast.Tuple):
        raise StrategyAnalysisError(f"NFI {wrapper.name} inline decision target is invalid")
    condition_name = cast(ast.Name, loop_target.elts[0]).id
    reason_name = cast(ast.Name, loop_target.elts[1]).id
    reason_template = _inline_reason_template(loop)

    parents = {
        child: parent
        for parent in ast.walk(wrapper)
        for child in ast.iter_child_nodes(parent)
    }
    container = parents.get(tuple_statement)
    while container is not None and not (
        isinstance(container, ast.If) and tuple_statement in container.body
    ):
        container = parents.get(container)
    if not isinstance(container, ast.If):
        raise StrategyAnalysisError(f"NFI {wrapper.name} inline range is missing")
    range_test = _inline_range_test(container.test, assignments)

    local_assignments = [
        copy.deepcopy(statement)
        for statement in container.body
        if isinstance(statement, ast.Assign)
        and statement.lineno < tuple_statement.lineno
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id != "in_range"
    ]
    decisions: list[ast.stmt] = []
    for item in decision_tuple.elts:
        pair = cast(ast.Tuple, item)
        condition, reason = pair.elts
        replacements = {
            condition_name: condition,
            reason_name: reason,
            "mode": ast.Name(id="mode_name", ctx=ast.Load()),
        }
        compiled_reason = _InlineNameSubstitution(replacements).visit(
            copy.deepcopy(reason_template)
        )
        decisions.append(
            ast.If(
                test=copy.deepcopy(condition),
                body=[
                    ast.Return(
                        value=ast.Tuple(
                            elts=[ast.Constant(value=True), compiled_reason],
                            ctx=ast.Load(),
                        )
                    )
                ],
                orelse=[],
            )
        )
    fragment = ast.FunctionDef(
        name=f"__{wrapper.name}_inline",
        args=ast.arguments(
            posonlyargs=[],
            args=[
                ast.arg(arg="mode_name"),
                ast.arg(arg="profit_init_ratio"),
                ast.arg(arg="last_candle"),
            ],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=[
            ast.If(
                test=range_test,
                body=[*local_assignments, *decisions],
                orelse=[],
            ),
            ast.Return(
                value=ast.Tuple(
                    elts=[ast.Constant(value=False), ast.Constant(value=None)],
                    ctx=ast.Load(),
                )
            ),
        ],
        decorator_list=[],
        returns=None,
        type_comment=None,
        type_params=[],
    )
    ast.copy_location(fragment, wrapper)
    ast.fix_missing_locations(fragment)
    return compile_scalar_ast_program(fragment)


class _InlineDecisionReturn(ast.NodeTransformer):
    """Turn one source ``sell, signal_name = True, reason`` into a return."""

    def visit_Assign(self, node: ast.Assign) -> ast.stmt:
        if not _sell_signal_target(node.targets) or not isinstance(node.value, ast.Tuple):
            visited = self.generic_visit(node)
            if not isinstance(visited, ast.stmt):
                raise StrategyAnalysisError("NFI inline assignment lowering failed")
            return visited
        if (
            len(node.value.elts) != 2
            or not isinstance(node.value.elts[0], ast.Constant)
            or node.value.elts[0].value is not True
        ):
            raise StrategyAnalysisError("NFI inline decision assignment changed")
        return ast.copy_location(ast.Return(value=self.visit(node.value)), node)


def _compile_direct_inline_exit_program(
    wrapper: ast.FunctionDef,
    suffix_family: str,
) -> dict[str, Any]:
    """Compile short quick/rapid's direct source ``if``/``elif`` chain.

    These callbacks do not share long's tuple/loop spelling.  The emitted
    bytecode still comes from their own conditions and reason expressions;
    this is deliberately not a sign-flipped long policy.
    """

    suffix = "_q_" if suffix_family == "q" else "_rpd_"
    candidates: list[ast.If] = []
    parents = {
        child: parent
        for parent in ast.walk(wrapper)
        for child in ast.iter_child_nodes(parent)
    }
    for node in ast.walk(wrapper):
        if not isinstance(node, ast.Assign) or not _sell_signal_target(node.targets):
            continue
        if suffix not in ast.unparse(node.value):
            continue
        parent = parents.get(node)
        while parent is not None and not isinstance(parent, ast.If):
            parent = parents.get(parent)
        if isinstance(parent, ast.If):
            candidates.append(parent)
    if not candidates:
        raise StrategyAnalysisError(f"NFI {wrapper.name} inline decisions cannot be represented")

    candidate_ids = {id(node) for node in candidates}
    root = next(
        (
            node
            for node in candidates
            if not any(
                isinstance(parent, ast.If) and id(parent) in candidate_ids
                for parent in _parents(node, parents)
            )
        ),
        None,
    )
    if root is None:
        raise StrategyAnalysisError(f"NFI {wrapper.name} inline decision root is ambiguous")

    containing = parents.get(root)
    while containing is not None and not (
        isinstance(containing, ast.If) and root in containing.body
    ):
        containing = parents.get(containing)
    if not isinstance(containing, ast.If) or not _is_not_sell_test(containing.test):
        raise StrategyAnalysisError(f"NFI {wrapper.name} inline guard changed")

    local_assignments = [
        copy.deepcopy(statement)
        for statement in containing.body
        if isinstance(statement, ast.Assign)
        and statement.lineno < root.lineno
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and not any(
            isinstance(item, ast.Name)
            and item.id
            in {
                "filled_orders",
                "filled_entries",
                "filled_exits",
                "profit_stake",
                "profit_ratio",
                "profit_current_stake_ratio",
            }
            for item in ast.walk(statement.value)
        )
    ]
    decision = _InlineDecisionReturn().visit(copy.deepcopy(root))
    if not isinstance(decision, ast.If):
        raise StrategyAnalysisError(f"NFI {wrapper.name} inline decision is invalid")
    fragment = ast.FunctionDef(
        name=f"__{wrapper.name}_inline",
        args=ast.arguments(
            posonlyargs=[],
            args=[
                ast.arg(arg="mode_name"),
                ast.arg(arg="profit_init_ratio"),
                ast.arg(arg="last_candle"),
            ],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=[
            *local_assignments,
            decision,
            ast.Return(
                value=ast.Tuple(
                    elts=[ast.Constant(value=False), ast.Constant(value=None)],
                    ctx=ast.Load(),
                )
            ),
        ],
        decorator_list=[],
        returns=None,
        type_comment=None,
        type_params=[],
    )
    ast.copy_location(fragment, wrapper)
    ast.fix_missing_locations(fragment)
    return compile_scalar_ast_program(fragment)


def _parents(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> Iterable[ast.AST]:
    parent = parents.get(node)
    while parent is not None:
        yield parent
        parent = parents.get(parent)


def _inline_reason_template(loop: ast.For) -> ast.expr:
    for node in ast.walk(loop):
        if not isinstance(node, ast.Assign):
            continue
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "signal_name"
        ):
            return node.value
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Tuple)
            and len(node.targets[0].elts) == 2
            and isinstance(node.value, ast.Tuple)
            and len(node.value.elts) == 2
            and isinstance(node.targets[0].elts[1], ast.Name)
            and node.targets[0].elts[1].id == "signal_name"
        ):
            return node.value.elts[1]
    raise StrategyAnalysisError("NFI inline decision reason cannot be represented")


def _inline_range_test(
    test: ast.expr,
    assignments: Mapping[str, ast.Assign],
) -> ast.expr:
    if isinstance(test, ast.Name) and test.id in assignments:
        return copy.deepcopy(assignments[test.id].value)
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        values = [
            value
            for value in test.values
            if not any(
                isinstance(item, ast.Name) and item.id == "sell"
                for item in ast.walk(value)
            )
        ]
        if len(values) == 1:
            return copy.deepcopy(values[0])
    raise StrategyAnalysisError("NFI inline profit guard cannot be represented")


def _inline_profit_bounds(wrapper: ast.FunctionDef, suffix_line: int) -> dict[str, Any]:
    assignments = {
        target.id: statement.value
        for statement in ast.walk(wrapper)
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance((target := statement.targets[0]), ast.Name)
    }
    candidates: list[tuple[int, dict[str, Any]]] = []
    for node in ast.walk(wrapper):
        if not isinstance(node, ast.If) or node.end_lineno is None:
            continue
        if not (node.lineno <= suffix_line <= node.end_lineno):
            continue
        test = (
            assignments.get(node.test.id, node.test)
            if isinstance(node.test, ast.Name)
            else node.test
        )
        for compare in ast.walk(test):
            if not isinstance(compare, ast.Compare):
                continue
            bounds = _profit_range_bounds(compare)
            if bounds is not None:
                candidates.append((node.end_lineno - node.lineno, bounds))
    if not candidates:
        raise StrategyAnalysisError(f"NFI {wrapper.name} inline profit range cannot be represented")
    return min(candidates, key=lambda item: item[0])[1]


def _profit_range_bounds(compare: ast.Compare) -> dict[str, Any] | None:
    operands = [compare.left, *compare.comparators]
    profit_positions = [
        index
        for index, operand in enumerate(operands)
        if isinstance(operand, ast.Name) and operand.id == "profit_init_ratio"
    ]
    if len(profit_positions) != 1:
        return None
    profit_index = profit_positions[0]
    bounds: dict[str, tuple[float, bool]] = {}
    for index, operator in enumerate(compare.ops):
        left = operands[index]
        right = operands[index + 1]
        if index + 1 == profit_index:
            value = _number(left)
            if value is not None and isinstance(operator, ast.Lt | ast.LtE):
                bounds["minimum"] = (value, isinstance(operator, ast.LtE))
            elif value is not None and isinstance(operator, ast.Gt | ast.GtE):
                bounds["maximum"] = (value, isinstance(operator, ast.GtE))
        elif index == profit_index:
            value = _number(right)
            if value is not None and isinstance(operator, ast.Gt | ast.GtE):
                bounds["minimum"] = (value, isinstance(operator, ast.GtE))
            elif value is not None and isinstance(operator, ast.Lt | ast.LtE):
                bounds["maximum"] = (value, isinstance(operator, ast.LtE))
    if set(bounds) != {"minimum", "maximum"}:
        return None
    minimum, minimum_inclusive = bounds["minimum"]
    maximum, maximum_inclusive = bounds["maximum"]
    return {
        "minimum_profit": minimum,
        "minimum_inclusive": minimum_inclusive,
        "maximum_profit": maximum,
        "maximum_inclusive": maximum_inclusive,
    }


def _u_e_raise_delta(wrapper: ast.FunctionDef) -> float:
    values: list[float] = []
    for node in ast.walk(wrapper):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "profit_ratio"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Gt)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.BinOp)
            and isinstance(node.comparators[0].op, ast.Add)
            and isinstance(node.comparators[0].left, ast.Name)
            and node.comparators[0].left.id == "previous_profit"
        ):
            value = _number(node.comparators[0].right)
            if value is not None:
                values.append(value)
    if len(set(values)) != 1:
        raise StrategyAnalysisError(f"NFI {wrapper.name} target raise delta cannot be represented")
    return values[0]


def _profit_raise_delta(wrapper: ast.FunctionDef) -> float:
    values: list[float] = []
    for node in ast.walk(wrapper):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "profit_init_ratio"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Gt)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.BinOp)
            and isinstance(node.comparators[0].op, ast.Add)
            and isinstance(node.comparators[0].left, ast.Name)
            and node.comparators[0].left.id == "previous_profit"
        ):
            value = _number(node.comparators[0].right)
            if value is not None:
                values.append(value)
    if len(set(values)) != 1:
        raise StrategyAnalysisError(f"NFI {wrapper.name} profit raise delta cannot be represented")
    return values[0]


def _max_target_floor(wrapper: ast.FunctionDef) -> float:
    aliases = _self_aliases(wrapper)
    assignments = {
        target.id: statement.value
        for statement in ast.walk(wrapper)
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance((target := statement.targets[0]), ast.Name)
    }
    parents = {
        child: parent
        for parent in ast.walk(wrapper)
        for child in ast.iter_child_nodes(parent)
    }
    values: list[float] = []
    for call in ast.walk(wrapper):
        if (
            not isinstance(call, ast.Call)
            or _call_name(call, aliases) != "_set_profit_target"
            or len(call.args) < 2
        ):
            continue
        reason = call.args[1]
        if isinstance(reason, ast.Name):
            reason = assignments.get(reason.id, reason)
        if "_max" not in ast.unparse(reason):
            continue
        ancestor = parents.get(call)
        while ancestor is not None:
            if isinstance(ancestor, ast.If):
                for compare in ast.walk(ancestor.test):
                    if not (
                        isinstance(compare, ast.Compare)
                        and any(isinstance(operator, ast.GtE) for operator in compare.ops)
                        and any(
                            isinstance(item, ast.Name) and item.id == "profit_init_ratio"
                            for item in ast.walk(compare)
                        )
                    ):
                        continue
                    values.extend(
                        value
                        for operand in (compare.left, *compare.comparators)
                        if (value := _number(operand)) is not None
                    )
                if values:
                    break
            ancestor = parents.get(ancestor)
    if len(set(values)) != 1:
        raise StrategyAnalysisError(f"NFI {wrapper.name} max target floor cannot be represented")
    return values[0]


def _has_protected_reentry_guard(wrapper: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "previous_sell_reason"
        and any(isinstance(operator, ast.NotIn) for operator in node.ops)
        for node in ast.walk(wrapper)
    )


def _suppresses_protected_exit(wrapper: ast.FunctionDef) -> bool:
    assignments = {
        target.id: statement.value
        for statement in ast.walk(wrapper)
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance((target := statement.targets[0]), ast.Name)
    }
    candidates: list[tuple[int, ast.AST]] = []
    for node in ast.walk(wrapper):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "signal_name"
            and any(isinstance(operator, ast.NotIn) for operator in node.ops)
            and len(node.comparators) == 1
        ):
            comparator = node.comparators[0]
            if isinstance(comparator, ast.Name):
                comparator = assignments.get(comparator.id, comparator)
            candidates.append((node.lineno, comparator))
    if not candidates:
        raise StrategyAnalysisError(f"NFI {wrapper.name} final signal filter is missing")
    comparator = max(candidates, key=lambda item: item[0])[1]
    names = {
        node.id
        for node in ast.walk(comparator)
        if isinstance(node, ast.Name)
    }
    rendered = ast.unparse(comparator)
    return "stoploss" in rendered or any("stoploss" in name for name in names)


def _uses_pure_scalp_trailing(wrapper: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Attribute)
        and "stop_threshold_scalp" in node.attr
        for node in ast.walk(wrapper)
    )


def _pure_scalp_matcher(
    helper: ast.FunctionDef,
    side: str,
    constants: Mapping[str, Any],
) -> dict[str, Any]:
    if side not in {"long", "short"}:
        raise StrategyAnalysisError("NFI pure-scalp side is invalid")
    parents = {
        child: parent
        for parent in ast.walk(helper)
        for child in ast.iter_child_nodes(parent)
    }
    aliases = _self_aliases(helper)
    candidates: list[dict[str, Any]] = []
    for node in ast.walk(helper):
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
            or node.targets[0].id != "is_scalp_mode"
        ):
            continue
        branch_side = _enclosing_trade_side(node, parents)
        if branch_side != side:
            continue
        matcher = _compile_tag_matcher(node.value, aliases, constants)
        if matcher is None:
            raise StrategyAnalysisError(
                f"NFI {side} pure-scalp target matcher cannot be represented"
            )
        candidates.append(matcher)
    if len(candidates) != 1:
        raise StrategyAnalysisError(
            f"NFI {side} pure-scalp target matcher is missing or ambiguous"
        )
    return candidates[0]


def _enclosing_trade_side(
    node: ast.AST,
    parents: Mapping[ast.AST, ast.AST],
) -> str | None:
    child = node
    parent = parents.get(child)
    while parent is not None:
        if isinstance(parent, ast.If) and _trade_is_short_test(parent.test):
            if child in parent.body:
                return "short"
            if child in parent.orelse:
                return "long"
            return None
        child = parent
        parent = parents.get(parent)
    return None


def _trade_is_short_test(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "is_short"
        and isinstance(node.value, ast.Name)
        and node.value.id == "trade"
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
