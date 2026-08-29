"""Window parameter validation and causal window lowering."""

from __future__ import annotations

import ast
from typing import Any

from ._indicator_ast import (
    _cast_target,
    _is_json_value,
    _safe_expression,
)
from ._indicator_contract import (
    _add_finite_lookback,
    _cast_result_type,
)
from .indicator_compiler_protocol import CompilerProtocol

_WINDOW_REDUCERS = {"max", "mean", "min", "std", "sum"}


def _window_parameters(
    kind: str,
    node: ast.Call,
    compiler: CompilerProtocol,
) -> dict[str, Any]:
    values = _keyword_value_map(node, compiler)
    if node.args:
        if kind == "rolling":
            values.setdefault("window", _required_static(node.args[0], compiler))
        elif kind == "ewm":
            values.setdefault("com", _required_static(node.args[0], compiler))
    allowed = {
        "rolling": {"window", "min_periods", "center", "closed"},
        "ewm": {"com", "span", "halflife", "alpha", "min_periods", "adjust", "ignore_na"},
        "expanding": {"min_periods"},
    }[kind]
    unknown = set(values) - allowed
    if unknown:
        compiler.unsupported(node, f"unsupported {kind} parameters: {sorted(unknown)}")
    if kind == "rolling":
        values.setdefault("center", False)
        values.setdefault("min_periods", None)
    if kind == "ewm":
        values.setdefault("adjust", True)
        values.setdefault("ignore_na", False)
        values.setdefault("min_periods", 0)
    if kind == "expanding":
        values.setdefault("min_periods", 1)
    return values


def _literal_keyword_arguments(
    node: ast.Call,
    compiler: CompilerProtocol,
) -> dict[str, Any]:
    return _keyword_value_map(node, compiler)


def _keyword_value_map(
    node: ast.Call,
    compiler: CompilerProtocol,
) -> dict[str, Any]:
    result = {}
    for keyword in node.keywords:
        if keyword.arg is None:
            compiler.unsupported(keyword.value, "expanded indicator keyword arguments")
        result[keyword.arg] = _required_static(keyword.value, compiler)
    return result


def _integer_argument(
    node: ast.Call,
    name: str,
    *,
    default: int,
    compiler: CompilerProtocol,
) -> int:
    value: Any = default
    if node.args:
        value = _required_static(node.args[0], compiler)
    for keyword in node.keywords:
        if keyword.arg == name:
            value = _required_static(keyword.value, compiler)
        elif keyword.arg is not None:
            compiler.unsupported(keyword.value, f"unsupported {name} keyword {keyword.arg}")
    if not isinstance(value, int) or isinstance(value, bool):
        compiler.unsupported(node, f"{name} must be a static integer")
    return value


def _required_static(
    node: ast.expr,
    compiler: CompilerProtocol,
) -> Any:
    value = compiler.static_value(node)
    if value is None and not (isinstance(node, ast.Constant) and node.value is None):
        compiler.unsupported(node, "dynamic indicator parameter")
    if not _is_json_value(value):
        compiler.unsupported(node, "non-JSON indicator parameter")
    return value


_WINDOW_REDUCERS = {"max", "mean", "min", "std", "sum"}


class WindowsLoweringMixin:
    def method_call(self: CompilerProtocol, node: ast.Call, callable_name: str) -> str:
        if not isinstance(node.func, ast.Attribute):  # pragma: no cover - caller guards it
            self.unsupported(node, "indicator method call")
        method = node.func.attr
        base = self.expression(node.func.value)
        if method == "astype":
            if len(node.args) != 1 or node.keywords:
                self.unsupported(node, "astype signature")
            target = _cast_target(node.args[0])
            if target is None:
                self.unsupported(node.args[0], "dynamic astype target")
            return self.emit(
                node,
                "cast",
                _cast_result_type(self.node_types[base], target),
                inputs=[base],
                parameters={"target": target},
                lookback=self.lookback(base),
            )
        if method == "shift":
            periods = _integer_argument(node, "periods", default=1, compiler=self)
            if periods < 0:
                self.unsupported(node, "negative shift would look ahead")
            return self.emit(
                node,
                "shift",
                self.node_types[base],
                inputs=[base],
                parameters={"periods": periods},
                lookback=_add_finite_lookback(self.lookback(base), periods),
            )
        if method == "fillna":
            if len(node.args) != 1 or node.keywords:
                self.unsupported(node, "fillna signature")
            if self.node_types[base] != "f64-column":
                self.unsupported(node.func.value, "fillna source type")
            fill = self.expression(node.args[0])
            if self.node_types[fill] not in {"int-scalar", "f64-scalar"}:
                self.unsupported(node.args[0], "fillna value type")
            return self.emit(
                node,
                "array-call",
                "f64-column",
                inputs=[base, fill],
                parameters={"family": "numpy", "name": "fill-missing", "arguments": {}},
                lookback=self.merged_lookback([base, fill]),
            )
        if method == "ffill":
            if node.args or node.keywords:
                self.unsupported(node, "parameterized forward fill")
            return self.emit(
                node,
                "fill",
                self.node_types[base],
                inputs=[base],
                parameters={"direction": "forward"},
                lookback={
                    "kind": "recursive",
                    "candles": None,
                    "expression": _safe_expression(node),
                    "causal": bool(self.lookback(base)["causal"]),
                },
            )
        if method in {"bfill", "backfill"}:
            self.unsupported(node, "backward fill would look ahead")
        if method == "to_numpy":
            parameters = _literal_keyword_arguments(node, self)
            return self.emit(
                node,
                "cast",
                self.node_types[base],
                inputs=[base],
                parameters={"target": "array", "arguments": parameters},
                lookback=self.lookback(base),
            )
        self.unsupported(node, f"indicator method call {callable_name}")

    def window_call(self: CompilerProtocol, node: ast.Call) -> str | None:
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in _WINDOW_REDUCERS:
            return None
        window_call = node.func.value
        if not isinstance(window_call, ast.Call) or not isinstance(window_call.func, ast.Attribute):
            return None
        kind = window_call.func.attr
        if kind not in {"rolling", "ewm", "expanding"}:
            return None
        if node.args or node.keywords:
            self.unsupported(node, "window reducer arguments")
        base = self.expression(window_call.func.value)
        parameters = _window_parameters(kind, window_call, self)
        if parameters.get("center") is True:
            self.unsupported(window_call, "centered rolling window would look ahead")
        if kind == "rolling":
            window = parameters.get("window")
            if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
                self.unsupported(window_call, "rolling window must be a positive static integer")
            lookback = _add_finite_lookback(self.lookback(base), window - 1)
        else:
            lookback = {
                "kind": "recursive",
                "candles": None,
                "expression": _safe_expression(window_call),
                "causal": True,
            }
        return self.emit(
            node,
            "window",
            self.node_types[base],
            inputs=[base],
            parameters={"kind": kind, "reducer": node.func.attr, **parameters},
            lookback=lookback,
        )
