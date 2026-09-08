"""Prove strategy-only fee selection independently of exchange accounting."""

from __future__ import annotations

import ast
import math
from collections.abc import Mapping
from typing import Any

from ..errors import StrategyAnalysisError

_FEE_NAMES = {"custom_fee_open_rate", "custom_fee_close_rate"}
_PROFIT_BODY = """
custom_fee_open_rate = self.custom_fee_open_rate
custom_fee_close_rate = self.custom_fee_close_rate
is_futures_mode = self.is_futures_mode
fee_open_rate = trade.fee_open if custom_fee_open_rate is None else custom_fee_open_rate
fee_close_rate = trade.fee_close if custom_fee_close_rate is None else custom_fee_close_rate
total_amount = 0.0
total_stake = 0.0
total_profit = 0.0
if trade.is_short:
    fee_open_multiplier = 1 - fee_open_rate
    fee_close_multiplier = 1 + fee_close_rate
    for entry_order in filled_entries:
        filled = entry_order.safe_filled
        entry_stake = filled * entry_order.safe_price * fee_open_multiplier
        total_amount += filled
        total_stake += entry_stake
        total_profit += entry_stake
    for exit_order in filled_exits:
        filled = exit_order.safe_filled
        exit_stake = filled * exit_order.safe_price * fee_close_multiplier
        total_amount -= filled
        total_profit -= exit_stake
    current_stake = total_amount * exit_rate * fee_close_multiplier
    total_profit -= current_stake
else:
    fee_open_multiplier = 1 + fee_open_rate
    fee_close_multiplier = 1 - fee_close_rate
    for entry_order in filled_entries:
        filled = entry_order.safe_filled
        entry_stake = filled * entry_order.safe_price * fee_open_multiplier
        total_amount += filled
        total_stake += entry_stake
        total_profit -= entry_stake
    for exit_order in filled_exits:
        filled = exit_order.safe_filled
        exit_stake = filled * exit_order.safe_price * fee_close_multiplier
        total_amount -= filled
        total_profit += exit_stake
    current_stake = total_amount * exit_rate * fee_close_multiplier
    total_profit += current_stake
if is_futures_mode and trade.funding_fees is not None:
    total_profit += trade.funding_fees
total_profit_ratio = total_profit / total_stake
current_profit_ratio = total_profit / current_stake
init_profit_ratio = total_profit / (filled_entries[0].safe_filled * filled_entries[0].safe_price)
return (total_profit, total_profit_ratio, current_profit_ratio, init_profit_ratio)
"""


def compile_virtual_fee_policy(
    methods: Mapping[str, ast.FunctionDef],
    constants: Mapping[str, Any],
    *,
    require_source_snapshot: bool = False,
) -> dict[str, Any] | None:
    rates = {side: constants.get(f"custom_fee_{side}_rate") for side in ("open", "close")}
    if not require_source_snapshot and all(value is None for value in rates.values()):
        return None
    for value in rates.values():
        if value is not None and (
            type(value) not in (int, float) or not 0 <= value < 1 or not math.isfinite(value)
        ):
            raise StrategyAnalysisError("source virtual fee must be a finite number in [0, 1)")
    profit = methods.get("calc_total_profit")
    if profit is None:
        raise StrategyAnalysisError("source virtual fees require the profit helper")
    body = list(profit.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if (
        _dump(ast.Module(body=body, type_ignores=[])) != _dump(ast.parse(_PROFIT_BODY))
        or [arg.arg for arg in profit.args.args]
        != ["self", "trade", "filled_entries", "filled_exits", "exit_rate"]
        or profit.decorator_list
        or profit.args.defaults
        or profit.args.kwonlyargs
        or profit.args.posonlyargs
        or profit.args.vararg
        or profit.args.kwarg
    ):
        raise StrategyAnalysisError("source virtual-fee profit arithmetic changed")
    for name, method in methods.items():
        if method is profit:
            continue
        nodes = list(ast.walk(method))
        if name != "__init__" and any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"setattr", "delattr"}
            for node in nodes
        ):
            raise StrategyAnalysisError("source virtual fees have an unreviewed dynamic write")
        reads = [
            node for node in nodes if isinstance(node, ast.Attribute) and node.attr in _FEE_NAMES
        ]
        has_bindings = any(
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id in {"trade_fee_open", "trade_fee_close", "fee_open_rate", "fee_close_rate"}
            for node in nodes
        )
        if not reads and not has_bindings:
            continue
        proven: set[ast.AST] = set()
        for side in ("open", "close"):
            alias = ast.parse(f"trade_fee_{side} = trade.fee_{side}").body[0]
            selection = ast.parse(
                f"fee_{side}_rate = trade_fee_{side} if self.custom_fee_{side}_rate is None "
                f"else self.custom_fee_{side}_rate"
            ).body[0]
            indices = []
            for expected in (alias, selection):
                found = [i for i, node in enumerate(method.body) if _dump(node) == _dump(expected)]
                if len(found) != 1:
                    raise StrategyAnalysisError("source virtual-fee selection changed")
                indices.append(found[0])
                proven.update(ast.walk(method.body[found[0]]))
            if indices[0] >= indices[1]:
                raise StrategyAnalysisError("source virtual-fee binding order changed")
            for variable in (f"trade_fee_{side}", f"fee_{side}_rate"):
                if (
                    sum(
                        isinstance(node, ast.Name)
                        and node.id == variable
                        and not isinstance(node.ctx, ast.Load)
                        for node in nodes
                    )
                    != 1
                ):
                    raise StrategyAnalysisError("source virtual-fee binding is reassigned")
        if any(node not in proven for node in reads):
            raise StrategyAnalysisError("source virtual fee has an unreviewed use")
        _prove_cluster_fees(method)
    policy = {"open_rate": rates["open"], "close_rate": rates["close"]}
    if "short_grind_adjust_trade_position" in methods:
        policy["legacy_short_close_fee_additive"] = True
    return policy


def _prove_cluster_fees(method: ast.FunctionDef) -> None:
    # Source cluster P&L uses the actual close fee, even when total-profit
    # snapshots and action thresholds use a custom fee. These derived values
    # are supplied by the native order-state reconstruction.
    proven = set()
    # These legacy short helpers value a buy-to-close with 1 + the actual fee.
    # System-v3/v4 helpers deliberately retain the donor's 1 - fee expression.
    sign = (
        "+"
        if method.name
        in {
            "short_grind_adjust_trade_position",
            "short_adjust_trade_position_no_derisk",
        }
        else "-"
    )
    for node in ast.walk(method):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.endswith("_current_grind_stake"):
            continue
        proven.add(target)
        if isinstance(node.value, ast.Constant) and node.value.value == 0.0:
            continue
        prefix = target.id.removesuffix("_current_grind_stake")
        expected = ast.parse(
            f"{prefix}_total_amount * exit_rate * (1 {sign} trade_fee_close)", mode="eval"
        ).body
        if _dump(node.value) != _dump(expected):
            raise StrategyAnalysisError("source virtual-fee cluster accounting changed")
    if any(
        isinstance(node, ast.Name)
        and node.id.endswith("_current_grind_stake")
        and not isinstance(node.ctx, ast.Load)
        and node not in proven
        for node in ast.walk(method)
    ):
        raise StrategyAnalysisError("source virtual-fee cluster accounting has additional writes")


def _dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)
