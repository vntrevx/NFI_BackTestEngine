"""Pure AST recognition and static-value helpers for indicator compilation."""

from __future__ import annotations

import ast
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from functools import cache
from typing import Any, cast

from ._indicator_contract import IndicatorProgramCompileError

_NATIVE_HELPER_TEMPLATES = {
    "chaikin-money-flow": """
def helper(high, low, close, volume, timeperiod=20):
    hl_range = high - low
    mfm = np.zeros_like(close, dtype=np.float64)
    valid = hl_range != 0
    mfm[valid] = ((close[valid] - low[valid]) - (high[valid] - close[valid])) / hl_range[valid]
    mfv = mfm * volume
    stacked = np.vstack([mfv, volume])
    stacked_clean = np.nan_to_num(stacked, nan=0.0)
    csum = np.cumsum(stacked_clean, axis=1)
    csum[:, timeperiod:] -= csum[:, :-timeperiod]
    out = np.full(stacked.shape, np.nan, dtype=np.float64)
    out[:, timeperiod - 1:] = csum[:, timeperiod - 1:]
    mfv_sum, vol_sum = out[0], out[1]
    vol_sum = np.where(vol_sum == 0, np.nan, vol_sum)
    return mfv_sum / vol_sum
""",
    "chaikin-money-flow-legacy": """
def helper(high, low, close, volume, timeperiod=20):
    hl_range = high - low
    mfm = np.zeros_like(close, dtype=np.float64)
    valid = hl_range != 0
    mfm[valid] = ((close[valid] - low[valid]) - (high[valid] - close[valid])) / hl_range[valid]
    mfv = mfm * volume
    mfv_sum = __class__.rolling_sum(mfv, timeperiod)
    vol_sum = ta.SUM(volume, timeperiod=timeperiod)
    vol_sum = np.where(vol_sum == 0, np.nan, vol_sum)
    return mfv_sum / vol_sum
""",
    "safe-percent-change": """
def helper(arr):
    arr = np.asarray(arr, dtype=np.float64)
    out = np.full(arr.shape, np.nan, dtype=np.float64)
    prev = arr[:-1]
    np.divide((arr[1:] - prev), prev, out=out[1:], where=prev != 0)
    out[1:] *= 100.0
    return out
""",
}

_OPENING_RANGE_TEMPLATE = """
_or_day = df["date"].dt.floor("1D")
_or_first4h = df["date"].dt.hour < 4
_or_valid = df["date"].dt.hour >= 4
orange_h_col = (
    df["high"].where(_or_first4h).groupby(_or_day).transform("max").where(_or_valid).to_numpy()
)
orange_l_col = (
    df["low"].where(_or_first4h).groupby(_or_day).transform("min").where(_or_valid).to_numpy()
)
"""

_AGE_FILTER_TEMPLATE = """
df["bt_agefilter_ok"] = False
df.loc[df.index > (12 * 24 * self.bt_min_age_days), "bt_agefilter_ok"] = True
"""

_INSIDE_BAR_TEMPLATE = """
_ib_hr = df["date"].dt.floor("1h")
_ib_agg = df.groupby(_ib_hr).agg(_ib_hh=("high", "max"), _ib_ll=("low", "min"))
_ib_agg["_ib_flag"] = (_ib_agg["_ib_hh"] < _ib_agg["_ib_hh"].shift(1)) & (
    _ib_agg["_ib_ll"] > _ib_agg["_ib_ll"].shift(1)
)
_ib_agg["_ib_mh"] = _ib_agg["_ib_hh"].shift(1)
_ib_agg["_ib_ml"] = _ib_agg["_ib_ll"].shift(1)
_ib_prev_hr = _ib_hr - pd.Timedelta(hours=1)
ib_ready_col = _ib_prev_hr.map(_ib_agg["_ib_flag"]).eq(True).astype(float).to_numpy()
ib_mother_h_col = _ib_prev_hr.map(_ib_agg["_ib_mh"]).to_numpy()
ib_mother_l_col = _ib_prev_hr.map(_ib_agg["_ib_ml"]).to_numpy()
"""

_FIRST_FIRE_TEMPLATE = """
_ph_cross = ((close_np > _ph_prev_max) & (np.roll(close_np, 1) <= _ph_prev_max)).astype(float)
_ph_cross[0] = 0.0
ph_cross_cnt12_col = pd.Series(_ph_cross).rolling(12).sum().to_numpy()
"""


@cache
def _template_function(source: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    return function


@cache
def _template_statements(source: str) -> tuple[ast.stmt, ...]:
    return tuple(ast.parse(source).body)


def _helper_body(function: ast.FunctionDef) -> Sequence[ast.stmt]:
    body = function.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _helper_bodies_equal(left: ast.FunctionDef, right: ast.FunctionDef) -> bool:
    left_body = _helper_body(left)
    right_body = _helper_body(right)
    if len(left_body) != len(right_body):
        return False
    return _ast_equal(
        ast.Module(body=list(left_body), type_ignores=[]),
        ast.Module(body=list(right_body), type_ignores=[]),
    )


def _recognized_tag_appender(function: ast.FunctionDef) -> bool:
    parameters = [*function.args.posonlyargs, *function.args.args]
    if len(parameters) != 3:
        return False
    target_name, _, tag_name = (parameter.arg for parameter in parameters)
    for statement in ast.walk(function):
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Subscript)
            and isinstance(statement.targets[0].value, ast.Name)
            and statement.targets[0].value.id == target_name
            and isinstance(statement.value, ast.BinOp)
            and isinstance(statement.value.op, ast.Add)
            and isinstance(statement.value.right, ast.Name)
            and statement.value.right.id == tag_name
        ):
            continue
        return True
    return False


def _is_shift_slice_assignment(
    statement: ast.stmt,
    *,
    output_name: str,
    source_name: str,
    periods_name: str,
    warmup: bool,
) -> bool:
    if not (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Subscript)
        and isinstance(statement.targets[0].value, ast.Name)
        and statement.targets[0].value.id == output_name
        and isinstance(statement.targets[0].slice, ast.Slice)
    ):
        return False
    target_slice = statement.targets[0].slice
    if warmup:
        return (
            target_slice.lower is None
            and isinstance(target_slice.upper, ast.Name)
            and target_slice.upper.id == periods_name
            and target_slice.step is None
            and isinstance(statement.value, ast.Attribute)
            and _qualified_name(statement.value) == "np.nan"
        )
    if not (
        isinstance(target_slice.lower, ast.Name)
        and target_slice.lower.id == periods_name
        and target_slice.upper is None
        and target_slice.step is None
        and isinstance(statement.value, ast.Subscript)
        and isinstance(statement.value.value, ast.Name)
        and statement.value.value.id == source_name
        and isinstance(statement.value.slice, ast.Slice)
    ):
        return False
    source_slice = statement.value.slice
    return (
        source_slice.lower is None
        and isinstance(source_slice.upper, ast.UnaryOp)
        and isinstance(source_slice.upper.op, ast.USub)
        and isinstance(source_slice.upper.operand, ast.Name)
        and source_slice.upper.operand.id == periods_name
        and source_slice.step is None
    )


def _is_array_index_nan_write(statement: ast.stmt, name: str, index: int) -> bool:
    return (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Subscript)
        and isinstance(statement.targets[0].value, ast.Name)
        and statement.targets[0].value.id == name
        and isinstance(statement.targets[0].slice, ast.Constant)
        and statement.targets[0].slice.value == index
        and isinstance(statement.value, ast.Attribute)
        and _qualified_name(statement.value) == "np.nan"
    )


def _is_absolute_difference_write(
    statement: ast.stmt,
    output_name: str,
    source_node: ast.expr,
) -> bool:
    if not (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Subscript)
        and isinstance(statement.targets[0].value, ast.Name)
        and statement.targets[0].value.id == output_name
        and isinstance(statement.targets[0].slice, ast.Slice)
        and isinstance(statement.targets[0].slice.lower, ast.Constant)
        and statement.targets[0].slice.lower.value == 1
        and statement.targets[0].slice.upper is None
        and statement.targets[0].slice.step is None
        and isinstance(statement.value, ast.Call)
        and _qualified_name(statement.value.func) == "np.abs"
        and len(statement.value.args) == 1
        and not statement.value.keywords
        and isinstance(statement.value.args[0], ast.Call)
        and _qualified_name(statement.value.args[0].func) == "np.diff"
        and len(statement.value.args[0].args) == 1
        and not statement.value.args[0].keywords
    ):
        return False
    return _ast_equal(statement.value.args[0].args[0], source_node)


def _static_value(node: ast.expr, constants: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in constants
    ):
        return constants[node.attr]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _static_value(node.operand, constants)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return -value
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, MemoryError, RecursionError):
        return None
    return value if _is_json_value(value) else None


def _declared_class_constants(
    class_node: ast.ClassDef,
    analyzed: Mapping[str, Any],
) -> dict[str, Any]:
    """Recover deterministic containers omitted by the JSON-only analyzer."""
    constants = {name: _normalized_static_value(value) for name, value in analyzed.items()}
    for statement in class_node.body:
        target: ast.expr | None = None
        value_node: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            value_node = statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target = statement.target
            value_node = statement.value
        if not isinstance(target, ast.Name) or value_node is None:
            continue
        value = _class_static_value(value_node, constants)
        if value is not _STATIC_MISSING:
            constants[target.id] = _normalized_static_value(value)
    return constants


_STATIC_MISSING = object()


def _class_static_value(node: ast.expr, constants: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        function = {
            "frozenset": frozenset,
            "list": list,
            "set": set,
            "tuple": tuple,
        }.get(node.func.id)
        if function is None or len(node.args) != 1 or node.keywords:
            return _STATIC_MISSING
        argument = _class_static_value(node.args[0], constants)
        if argument is _STATIC_MISSING:
            return _STATIC_MISSING
        try:
            return function(argument)
        except (TypeError, ValueError):
            return _STATIC_MISSING
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, MemoryError, RecursionError):
        return _STATIC_MISSING


def _normalized_static_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise IndicatorProgramCompileError("static mapping keys must be strings")
        return {str(key): _normalized_static_value(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        normalized = [_normalized_static_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, list | tuple):
        return [_normalized_static_value(item) for item in value]
    raise IndicatorProgramCompileError(
        f"static value is not JSON-compatible: {type(value).__name__}"
    )


def _effective_backtest_config(config: Mapping[str, Any]) -> dict[str, Any]:
    effective = dict(_normalized_static_value(config))
    runmode = effective.get("runmode")
    if runmode is None:
        effective["runmode"] = {"value": "backtest"}
    elif not isinstance(runmode, Mapping) or runmode.get("value") != "backtest":
        raise IndicatorProgramCompileError(
            "compiler run mode differs from the supplied configuration"
        )
    return effective


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, bool | int | float | str):
        return True
    if isinstance(value, list | tuple):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _literal_string(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


@cache
def _indicator_output_names(callable_name: str) -> tuple[str, ...] | None:
    if not callable_name.startswith("ta."):
        return None
    from talib import abstract

    try:
        function = abstract.Function(callable_name.removeprefix("ta."))
    except Exception:  # TA-Lib exposes invalid-function errors through a generic wrapper exception.
        return None
    names = cast(Sequence[object], function.output_names)
    return tuple(str(name) for name in names)


@cache
def _indicator_signature(callable_name: str) -> tuple[int, tuple[str, ...]] | None:
    """Return TA-Lib array arity and its source-ordered static parameters."""
    if not callable_name.startswith("ta."):
        return None
    from talib import abstract

    try:
        function = abstract.Function(callable_name.removeprefix("ta."))
    except Exception:  # See the matching output-name probe above.
        return None
    input_count = 0
    for value in function.input_names.values():
        if isinstance(value, str):
            input_count += 1
        elif isinstance(value, Sequence):
            input_count += len(value)
        else:  # pragma: no cover - pinned TA-Lib exposes only these two shapes.
            return None
    return input_count, tuple(str(name) for name in function.parameters)


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _parameter_type(name: str) -> str:
    lowered = name.lower()
    if lowered in {"df", "dataframe", "informative", "frame", "info"} or "dataframe" in lowered:
        return "dataframe"
    if lowered == "metadata":
        return "metadata"
    return "dynamic"


def _cast_target(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name) and node.id in {"bool", "float", "int"}:
        return node.id
    qualified = _qualified_name(node)
    if qualified in {"np.bool_", "np.bool"}:
        return "bool"
    if qualified in {"np.float32", "np.float64"}:
        return "float"
    if qualified in {"np.int32", "np.int64"}:
        return "int"
    return None


def _flatten_binary_values(
    node: ast.BinOp,
    operator_type: type[ast.operator],
) -> list[ast.expr]:
    values: list[ast.expr] = []
    stack: list[ast.expr] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.BinOp) and isinstance(current.op, operator_type):
            stack.append(current.right)
            stack.append(current.left)
        else:
            values.append(current)
    return values


def _ast_equal(left: Any, right: Any) -> bool:
    """Compare AST structure without recursing through deeply associated expressions."""
    pending: list[tuple[Any, Any]] = [(left, right)]
    while pending:
        current_left, current_right = pending.pop()
        if isinstance(current_left, ast.AST) or isinstance(current_right, ast.AST):
            if not isinstance(current_left, ast.AST) or type(current_left) is not type(
                current_right
            ):
                return False
            for field in current_left._fields:
                pending.append((getattr(current_left, field), getattr(current_right, field)))
            continue
        if isinstance(current_left, list | tuple) or isinstance(current_right, list | tuple):
            if not isinstance(current_left, list | tuple) or not isinstance(
                current_right, list | tuple
            ):
                return False
            if len(current_left) != len(current_right):
                return False
            pending.extend(zip(current_left, current_right, strict=True))
            continue
        if (
            isinstance(current_left, float)
            and isinstance(current_right, float)
            and math.isnan(current_left)
            and math.isnan(current_right)
        ):
            continue
        if current_left != current_right:
            return False
    return True


def _small_static_candidate(node: ast.expr) -> bool:
    return isinstance(
        node,
        ast.Constant
        | ast.Name
        | ast.Attribute
        | ast.Subscript
        | ast.Call
        | ast.JoinedStr
        | ast.List
        | ast.Tuple
        | ast.Set
        | ast.Dict,
    )


def _assigned_name(statement: ast.stmt) -> str | None:
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    ):
        return statement.targets[0].id
    return None


def _dataframe_column_target(node: ast.expr) -> tuple[str, str] | None:
    if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Name):
        return None
    column = _literal_string(node.slice)
    if column is None:
        return None
    return node.value.id, column


def _loop_target_names(target: ast.expr) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, ast.Tuple | ast.List) and all(
        isinstance(item, ast.Name) for item in target.elts
    ):
        return tuple(item.id for item in target.elts if isinstance(item, ast.Name))
    return ()


def _safe_expression(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except RecursionError:
        return f"<ast-sha256:{_iterative_ast_sha256(node)}>"


def _iterative_ast_sha256(node: ast.AST) -> str:
    digest = hashlib.sha256()
    stack: list[tuple[str, Any]] = [("node", node)]
    while stack:
        kind, value = stack.pop()
        digest.update(kind.encode())
        digest.update(b"\0")
        if isinstance(value, ast.AST):
            digest.update(type(value).__name__.encode())
            for name, child in reversed(list(ast.iter_fields(value))):
                stack.append(("field", name))
                stack.append(("value", child))
        elif isinstance(value, list):
            digest.update(str(len(value)).encode())
            for child in reversed(value):
                stack.append(("value", child))
        else:
            digest.update(repr(value).encode())
        digest.update(b"\0")
    return digest.hexdigest()
