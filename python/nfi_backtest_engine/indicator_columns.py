"""Resolve final dataframe column names from the compiled indicator graph."""

from __future__ import annotations

from typing import Any

from .errors import StrategyAnalysisError

_OHLCV = frozenset({"date", "open", "high", "low", "close", "volume"})


def indicator_output_columns(program: dict[str, Any]) -> set[str]:
    """Follow dataframe mutations, helper arguments and informative suffixes.

    This is used only to retain optional callback mapping reads. Required reads
    still fail in the vector runtime when absent. Unknown dataframe operations
    fail closed instead of silently hiding an optional value from the callback.
    """
    nodes = {node["id"]: node for node in program["nodes"]}
    functions = {function["id"]: function for function in program["functions"]}

    def execute(
        name: str, arguments: list[frozenset[str] | None], depth: int = 0,
    ) -> frozenset[str]:
        if depth > 64:
            raise StrategyAnalysisError("indicator column helper depth exceeds 64")
        function = functions[name]
        scope = {parameter["node"]: columns for parameter, columns in
                 zip(function["parameters"], arguments, strict=True)}
        for identifier in function["node_ids"]:
            node = nodes[identifier]
            op = node["op"]
            if op == "parameter":
                continue
            if node["value_type"] != "dataframe":
                scope[identifier] = None
                continue
            inputs = [scope.get(key) for key in node["inputs"]]
            params = node["parameters"]
            if op == "frame-source":
                columns = _OHLCV
            elif op == "function-call":
                columns = execute(params["function"], inputs, depth + 1)
            else:
                if not inputs or inputs[0] is None:
                    raise StrategyAnalysisError(f"indicator column input is not a frame: {op}")
                columns = inputs[0]
                if op in {"return", "frame-nonempty", "fill"}:
                    pass
                elif op == "column-write":
                    columns = columns | {params["column"]}
                elif op == "frame-drop-if-present":
                    columns = columns - {params["column"]}
                elif op == "frame-project":
                    columns = columns - (set(params["drop_candidates"]) -
                                         set(params["keep"]) - set(params["always_keep"]))
                elif op == "informative-merge":
                    suffix = (params["informative_timeframe"] if params["append_timeframe"]
                              else params["suffix"])
                    if len(inputs) != 2 or inputs[1] is None or not suffix:
                        raise StrategyAnalysisError("indicator column merge cannot be resolved")
                    columns = columns | {f"{column}_{suffix}" for column in inputs[1]}
                else:
                    raise StrategyAnalysisError(f"indicator column operation is unsupported: {op}")
            scope[identifier] = columns
        result = scope[function["return_node"]]
        if result is None:
            raise StrategyAnalysisError("indicator column function does not return a frame")
        return result

    return set(execute(program["entrypoint"], [_OHLCV, None]))
