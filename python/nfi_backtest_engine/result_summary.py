"""Pure, deterministic calculations for human-facing backtest summaries.

The exact trade surface remains the source of truth for parity.  This module only
derives presentation metrics from that surface; none of its values participate in
the engine/Freqtrade equality contract.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .canonical import canonical_decimal
from .reporting.tags import signal_tag_tokens, summarize_grind_tags

RESULT_SUMMARY_VERSION = "2.0.0"
MAX_EQUITY_POINTS = 1_000


@dataclass
class _Aggregate:
    """Mutable accumulator used while grouping an immutable trade surface."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    profit_abs: Decimal = Decimal(0)
    profit_ratio_sum: Decimal = Decimal(0)
    duration_minutes: int = 0

    def add(self, trade: Mapping[str, Any]) -> None:
        profit = _decimal(_mapping(trade, "profit").get("absolute"))
        ratio = _decimal(_mapping(trade, "profit").get("ratio"))
        self.trades += 1
        self.profit_abs += profit
        self.profit_ratio_sum += ratio
        self.duration_minutes += _integer(trade.get("duration_minutes"))
        if profit > 0:
            self.wins += 1
        elif profit < 0:
            self.losses += 1
        else:
            self.draws += 1

    def export(self, key_name: str, key: str) -> dict[str, Any]:
        return {
            key_name: key,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "draws": self.draws,
            "win_rate": _ratio(self.wins, self.trades),
            "profit_abs": _number(self.profit_abs),
            "profit_ratio_sum": _number(self.profit_ratio_sum),
            "average_profit_ratio": _number(
                self.profit_ratio_sum / self.trades if self.trades else Decimal(0)
            ),
            "average_duration_minutes": _ratio(
                self.duration_minutes,
                self.trades,
            ),
        }


def build_result_summary(
    run_report: Mapping[str, Any],
    trade_surface: Mapping[str, Any] | None,
    *,
    verification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable ``summary.json`` document for any research-run outcome."""

    status = str(run_report.get("status", "unknown"))
    inputs = _mapping(run_report, "inputs")
    strategy_input = _mapping(inputs, "strategy")
    vectors = _mapping(run_report, "vectors")
    execution = _mapping(run_report, "execution")
    timings = _mapping(run_report, "timings")
    context = (
        dict(_mapping(trade_surface, "context"))
        if trade_surface is not None
        else {
            "trading_mode": None,
            "margin_mode": None,
            "timeframe": None,
            "timeframe_detail": None,
            "timerange": inputs.get("timerange"),
        }
    )
    trades = _sequence(trade_surface, "trades") if trade_surface is not None else []
    pair_count = _optional_integer(vectors.get("pair_count"))
    if pair_count is None:
        pair_count = len({str(trade.get("pair", "")) for trade in trades})

    summary: dict[str, Any] = {
        "schema_version": RESULT_SUMMARY_VERSION,
        "run": {
            "id": run_report.get("run_id"),
            "status": status,
            "complete": bool(run_report.get("complete", status == "complete")),
            "native_status": run_report.get("native_status", status),
            "selected_status": run_report.get("selected_status", status),
            "execution_lane": run_report.get("selected_lane") or execution.get("lane") or "native",
            "created_at": run_report.get("created_at"),
            "strategy": strategy_input.get("class_name")
            or (trade_surface.get("strategy") if trade_surface else None),
            "strategy_sha256": strategy_input.get("file_sha256"),
            "context": context,
            "pair_count": pair_count,
        },
        "verification": _verification_summary(run_report, verification),
        "execution": {
            "wall_time_seconds": _optional_number(timings.get("pipeline_wall_time_seconds")),
            "indicator_workers": _optional_integer(execution.get("indicator_workers")),
            "cpu_process_limit": _optional_integer(execution.get("cpu_process_limit")),
            "portfolio_simulator_threads": _optional_integer(
                execution.get("portfolio_simulator_threads")
            ),
            "memory_budget_bytes": _optional_integer(execution.get("working_memory_bytes")),
            "peak_rss_bytes": _peak_rss_bytes(run_report),
            "resumed_stages": list(
                value for value in run_report.get("resumed_stages", []) if isinstance(value, str)
            ),
        },
        "artifacts": {
            "run": "run.json",
            "source_surface": "trade-surface.json" if trade_surface else None,
            "summary": "summary.json",
            "trades_csv": "trades.csv",
            "orders_csv": "orders.csv",
            "equity_csv": "equity.csv",
            "verification": "verification.json",
            "evidence_index": "evidence/index.json",
            "markdown_report": "report.md",
        },
        "blockers": _blocker_summary(run_report),
        "performance": None,
        "risk": None,
        "futures": None,
        "activity": {
            "pairs": pair_count,
            "trades": len(trades),
            "locks": (len(_sequence(trade_surface, "locks")) if trade_surface is not None else 0),
        },
        "breakdowns": {
            "by_pair": [],
            "by_entry_tag": [],
            "by_exit_reason": [],
            "by_direction": [],
            "by_year": [],
            "by_month": [],
        },
        "tag_analysis": {
            "signal": {
                "source": "entry_tag",
                "multi_label": True,
                "rows": [],
            },
            "grind": summarize_grind_tags([]),
        },
        "equity_curve": {
            "source": "closed_trade_profit",
            "raw_point_count": 0,
            "sampled": False,
            "points": [],
        },
        "notices": [
            (
                "Maximum drawdown is reconstructed from closed-trade equity. "
                "The exact trade surface remains the parity authority."
            )
        ],
    }
    if trade_surface is None:
        return summary

    surface_summary = _mapping(trade_surface, "summary")
    starting_balance = _decimal(surface_summary.get("starting_balance"))
    final_balance = _decimal(surface_summary.get("final_balance"))
    profit_total_abs = _decimal(surface_summary.get("profit_total_abs"))
    profits = [_decimal(_mapping(trade, "profit").get("absolute")) for trade in trades]
    ratios = [_decimal(_mapping(trade, "profit").get("ratio")) for trade in trades]
    wins = sum(1 for value in profits if value > 0)
    losses = sum(1 for value in profits if value < 0)
    draws = len(profits) - wins - losses
    gross_profit = sum((value for value in profits if value > 0), Decimal(0))
    gross_loss = -sum((value for value in profits if value < 0), Decimal(0))
    return_ratio = profit_total_abs / starting_balance if starting_balance != 0 else None
    cagr_ratio = _cagr(
        starting_balance,
        final_balance,
        str(context.get("timerange") or ""),
    )
    durations = [_integer(trade.get("duration_minutes")) for trade in trades]
    ordered = sorted(
        trades,
        key=lambda trade: (
            _integer(trade.get("close_timestamp_ms")),
            _integer(trade.get("sequence")),
        ),
    )
    equity = _equity_curve(
        starting_balance,
        final_balance,
        ordered,
        str(context.get("timerange") or ""),
    )
    closed_trade_risk = _closed_trade_risk_metrics(equity["rows"])
    consecutive_wins, consecutive_losses = _consecutive_outcomes(ordered)

    summary["performance"] = {
        "starting_balance": _number(starting_balance),
        "final_balance": _number(final_balance),
        "profit_total_abs": _number(profit_total_abs),
        "return_ratio": _optional_decimal_number(return_ratio),
        "cagr_ratio": cagr_ratio,
        "gross_profit_abs": _number(gross_profit),
        "gross_loss_abs": _number(gross_loss),
        "profit_factor": (_number(gross_profit / gross_loss) if gross_loss != 0 else None),
        "expectancy_abs": _mean(profits),
        "average_profit_ratio": _mean(ratios),
        "median_profit_ratio": _median(ratios),
        "best_trade": _trade_snapshot(max(trades, key=_trade_profit)) if trades else None,
        "worst_trade": _trade_snapshot(min(trades, key=_trade_profit)) if trades else None,
    }
    summary["risk"] = {
        "max_closed_trade_drawdown_abs": equity["max_drawdown_abs"],
        "max_closed_trade_drawdown_ratio": equity["max_drawdown_ratio"],
        "max_drawdown_peak_timestamp_ms": equity["peak_timestamp_ms"],
        "max_drawdown_trough_timestamp_ms": equity["trough_timestamp_ms"],
        "maximum_consecutive_wins": consecutive_wins,
        "maximum_consecutive_losses": consecutive_losses,
        "closed_trade_sharpe": closed_trade_risk["sharpe"],
        "closed_trade_sortino": closed_trade_risk["sortino"],
        "closed_trade_return_observations": closed_trade_risk["observations"],
        "closed_trade_risk_free_rate": 0.0,
        "closed_trade_annualized": False,
        "closed_trade_return_definition": (
            "profit_abs divided by equity immediately before each trade-close event"
        ),
    }
    if context.get("trading_mode") == "futures":
        summary["futures"] = _futures_summary(
            trades,
            lock_count=len(_sequence(trade_surface, "locks")),
            margin_mode=context.get("margin_mode"),
        )
    summary["activity"] = {
        "pairs": pair_count,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "win_rate": _ratio(wins, len(trades)),
        "average_duration_minutes": _mean(durations),
        "median_duration_minutes": _median(durations),
        "open_trades": sum(bool(trade.get("is_open")) for trade in trades),
        "rejected_signals": _optional_integer(surface_summary.get("rejected_signals")),
        "max_open_trades": _optional_integer(surface_summary.get("max_open_trades")),
        "locks": len(_sequence(trade_surface, "locks")),
        "total_volume": _optional_decimal_number(
            _optional_decimal(surface_summary.get("total_volume"))
        ),
    }
    by_pair = _group(trades, "pair", "pair")
    grouped_pairs = {str(row["pair"]) for row in by_pair}
    for pair in _configured_pair_names(run_report):
        if pair not in grouped_pairs:
            by_pair.append(_Aggregate().export("pair", pair))
    by_entry_tag = _group(
        trades,
        "entry_tag",
        "entry_tag",
        normalize=lambda value: value.strip() or "(untagged)",
    )
    by_exit_reason = _group(trades, "exit_reason", "exit_reason")
    by_direction = _group(trades, "direction", "direction")
    by_year = _group_by_period(trades, "%Y", "year", starting_balance)
    by_month = _group_by_period(trades, "%Y-%m", "month", starting_balance)
    by_signal_tag = _group_by_signal_tag(trades)
    summary["breakdowns"] = {
        "by_pair": sorted(
            by_pair,
            key=lambda item: (-float(item["profit_abs"]), str(item["pair"])),
        ),
        "by_entry_tag": sorted(
            by_entry_tag,
            key=lambda item: (
                -float(item["profit_abs"]),
                str(item["entry_tag"]),
            ),
        ),
        "by_exit_reason": sorted(
            by_exit_reason,
            key=lambda item: (
                -float(item["profit_abs"]),
                str(item["exit_reason"]),
            ),
        ),
        "by_direction": sorted(
            by_direction,
            key=lambda item: str(item["direction"]),
        ),
        "by_year": by_year,
        "by_month": by_month,
    }
    summary["tag_analysis"] = {
        "signal": {
            "source": "entry_tag",
            "multi_label": True,
            "rows": sorted(
                by_signal_tag,
                key=lambda item: (
                    -float(item["profit_abs"]),
                    str(item["signal_tag"]),
                ),
            ),
        },
        "grind": summarize_grind_tags(trades),
    }
    points = equity["points"]
    sampled_points = _sample_equity_points(
        points,
        required_index=equity["max_drawdown_index"],
    )
    summary["equity_curve"] = {
        "source": "closed_trade_profit",
        "raw_point_count": len(points),
        "sampled": len(sampled_points) != len(points),
        "points": sampled_points,
        "source_final_balance": equity["source_final_balance"],
        "reconciliation_delta": equity["reconciliation_delta"],
    }
    return summary


def _group_by_signal_tag(
    trades: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Group individual entry-tag tokens without treating groups as additive."""

    groups: dict[str, _Aggregate] = {}
    for trade in trades:
        for signal_tag in signal_tag_tokens(trade.get("entry_tag")):
            groups.setdefault(signal_tag, _Aggregate()).add(trade)
    return [aggregate.export("signal_tag", signal_tag) for signal_tag, aggregate in groups.items()]


def _futures_summary(
    trades: Sequence[Mapping[str, Any]],
    *,
    lock_count: int,
    margin_mode: Any,
) -> dict[str, Any]:
    """Derive futures-only metrics from fields already sealed for parity.

    Funding is reported as the exact signed value exported by the trade surface.
    We deliberately count liquidation only from ``exit_reason``; an internal or
    reconstructed liquidation price is not promoted into release evidence.
    """

    funding_values = [_decimal(_mapping(trade, "fees").get("funding")) for trade in trades]
    leverages = [
        _decimal(trade.get("leverage")) for trade in trades if trade.get("leverage") is not None
    ]
    leverage_rows = _group(trades, "leverage", "leverage")
    return {
        "margin_mode": margin_mode,
        "long_trades": sum(trade.get("direction") == "long" for trade in trades),
        "short_trades": sum(trade.get("direction") == "short" for trade in trades),
        "funded_trades": sum(value != 0 for value in funding_values),
        "funding_total": _number(sum(funding_values, Decimal(0))),
        "liquidation_exits": sum(trade.get("exit_reason") == "liquidation" for trade in trades),
        "protection_locks": lock_count,
        "distinct_leverages": len(set(leverages)),
        "minimum_leverage": _number(min(leverages)) if leverages else None,
        "maximum_leverage": _number(max(leverages)) if leverages else None,
        "by_leverage": sorted(
            leverage_rows,
            key=lambda row: _decimal(row["leverage"]),
        ),
    }


def _verification_summary(
    run_report: Mapping[str, Any],
    verification: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if verification is None:
        existing = _mapping(run_report, "official_confirmation")
        status = str(existing.get("status", "not_run"))
        return {
            "status": status,
            "exact": True if status in {"exact_match", "confirmed_exact"} else None,
            "source": existing.get("report"),
            "source_sha256": existing.get("report_sha256"),
            "difference": existing.get("difference"),
        }
    is_reference = "exact_parity" in verification and "equal" not in verification
    is_official_fallback = verification.get("purpose") == "fallback"
    reference_complete = verification.get("complete")
    exact_value = verification.get("equal", verification.get("exact_parity"))
    exact = exact_value if isinstance(exact_value, bool) else None
    status = (
        "reference_incomplete"
        if is_reference and reference_complete is not True
        else "official_only"
        if is_official_fallback and reference_complete is True
        else "exact_match"
        if exact is True
        else "mismatch"
        if exact is False
        else "not_run"
    )
    return {
        "status": status,
        "exact": exact if status != "reference_incomplete" else None,
        "source": verification.get("report_path")
        or verification.get("source")
        or verification.get("path"),
        "source_sha256": verification.get("report_sha256") or verification.get("source_sha256"),
        "difference": verification.get("difference"),
    }


def _blocker_summary(run_report: Mapping[str, Any]) -> list[dict[str, Any]]:
    capability = _mapping(run_report, "capability")
    blockers = capability.get("blockers")
    if not isinstance(blockers, list):
        return []
    return [
        {
            "code": blocker.get("code"),
            "callback": blocker.get("callback"),
            "message": blocker.get("message"),
        }
        for blocker in blockers
        if isinstance(blocker, Mapping)
    ]


def _peak_rss_bytes(run_report: Mapping[str, Any]) -> int | None:
    """Read observed memory only when a surrounding runner actually measured it."""

    candidates = (
        _mapping(run_report, "resource_usage").get("peak_rss_bytes"),
        _mapping(run_report, "measurement").get("peak_rss_bytes"),
        _mapping(run_report, "container_memory").get("peak_bytes"),
    )
    for candidate in candidates:
        value = _optional_integer(candidate)
        if value is not None and value >= 0:
            return value
    return None


def _equity_curve(
    starting_balance: Decimal,
    final_balance: Decimal,
    trades: Sequence[Mapping[str, Any]],
    timerange: str,
) -> dict[str, Any]:
    rows = _closed_trade_equity_rows(
        starting_balance,
        final_balance,
        trades,
        timerange,
    )
    points = [
        {
            "timestamp_ms": row["timestamp_ms"],
            "equity": _number(_decimal(row["equity"])),
            "drawdown_ratio": _number(_decimal(row["drawdown_ratio"])),
        }
        for row in rows
    ]
    maximum_index = 0
    if rows:
        maximum_index = max(
            range(len(rows)),
            key=lambda index: _decimal(rows[index]["drawdown_ratio"]),
        )
        maximum = rows[maximum_index]
        maximum_drawdown = _decimal(maximum["drawdown_abs"])
        maximum_drawdown_ratio = _decimal(maximum["drawdown_ratio"])
        maximum_peak_timestamp = _optional_integer(maximum.get("peak_timestamp_ms"))
        maximum_trough_timestamp = _optional_integer(maximum.get("timestamp_ms"))
    else:
        maximum_drawdown = Decimal(0)
        maximum_drawdown_ratio = Decimal(0)
        maximum_peak_timestamp = None
        maximum_trough_timestamp = None
    return {
        "rows": rows,
        "points": points,
        "max_drawdown_abs": _number(maximum_drawdown),
        "max_drawdown_ratio": _number(maximum_drawdown_ratio),
        "peak_timestamp_ms": maximum_peak_timestamp,
        "trough_timestamp_ms": maximum_trough_timestamp,
        "max_drawdown_index": maximum_index,
        "source_final_balance": _number(final_balance),
        "reconciliation_delta": (
            _number(_decimal(rows[-1]["reconciliation_delta"])) if rows else 0.0
        ),
    }


def build_closed_trade_equity_rows(
    trade_surface: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return unsampled equity events derived only from sealed closed trades.

    This intentionally does not interpolate candle-level equity. The first row is
    the starting balance, followed by exactly one row per closed trade in stable
    close-time/sequence order.
    """

    if trade_surface is None:
        return []
    surface_summary = _mapping(trade_surface, "summary")
    context = _mapping(trade_surface, "context")
    starting_balance = _decimal(surface_summary.get("starting_balance"))
    final_balance = _decimal(surface_summary.get("final_balance"))
    trades = sorted(
        _sequence(trade_surface, "trades"),
        key=lambda trade: (
            _integer(trade.get("close_timestamp_ms")),
            _integer(trade.get("sequence")),
        ),
    )
    return _closed_trade_equity_rows(
        starting_balance,
        final_balance,
        trades,
        str(context.get("timerange") or ""),
    )


def _closed_trade_equity_rows(
    starting_balance: Decimal,
    final_balance: Decimal,
    trades: Sequence[Mapping[str, Any]],
    timerange: str,
) -> list[dict[str, Any]]:
    start_timestamp = _timerange_start_ms(timerange)
    if start_timestamp is None and trades:
        start_timestamp = _integer(trades[0].get("open_timestamp_ms"))
    equity = starting_balance
    peak = starting_balance
    peak_timestamp = start_timestamp
    rows: list[dict[str, Any]] = []
    if start_timestamp is not None:
        rows.append(
            {
                "event_sequence": 0,
                "event": "start",
                "timestamp_ms": start_timestamp,
                "trade_sequence": None,
                "pair": None,
                "direction": None,
                "profit_abs": "0",
                "equity": _canonical_decimal(equity),
                "peak_equity": _canonical_decimal(peak),
                "peak_timestamp_ms": peak_timestamp,
                "drawdown_abs": "0",
                "drawdown_ratio": "0",
                "source_final_balance": None,
                "reconciliation_delta": None,
            }
        )
    for event_sequence, trade in enumerate(trades, start=1):
        timestamp = _integer(trade.get("close_timestamp_ms"))
        profit = _trade_profit(trade)
        equity += profit
        if equity > peak:
            peak = equity
            peak_timestamp = timestamp
        drawdown = peak - equity
        drawdown_ratio = drawdown / peak if peak > 0 else Decimal(0)
        rows.append(
            {
                "event_sequence": event_sequence,
                "event": "trade_close",
                "timestamp_ms": timestamp,
                "trade_sequence": _integer(trade.get("sequence")),
                "pair": trade.get("pair"),
                "direction": trade.get("direction"),
                "profit_abs": _canonical_decimal(profit),
                "equity": _canonical_decimal(equity),
                "peak_equity": _canonical_decimal(peak),
                "peak_timestamp_ms": peak_timestamp,
                "drawdown_abs": _canonical_decimal(drawdown),
                "drawdown_ratio": _canonical_decimal(drawdown_ratio),
                "source_final_balance": None,
                "reconciliation_delta": None,
            }
        )
    if rows:
        rows[-1]["source_final_balance"] = _canonical_decimal(final_balance)
        rows[-1]["reconciliation_delta"] = _canonical_decimal(final_balance - equity)
    return rows


def _closed_trade_risk_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    returns: list[Decimal] = []
    previous_equity: Decimal | None = None
    for row in rows:
        equity = _decimal(row.get("equity"))
        if (
            row.get("event") == "trade_close"
            and previous_equity is not None
            and previous_equity != 0
        ):
            returns.append(_decimal(row.get("profit_abs")) / previous_equity)
        previous_equity = equity
    if not returns:
        return {"sharpe": None, "sortino": None, "observations": 0}

    mean = sum(returns, Decimal(0)) / len(returns)
    sharpe = None
    if len(returns) >= 2:
        variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / (len(returns) - 1)
        if variance > 0:
            sharpe = _rounded(float(mean / variance.sqrt()))

    downside_variance = sum(
        (min(value, Decimal(0)) ** 2 for value in returns),
        Decimal(0),
    ) / len(returns)
    sortino = _rounded(float(mean / downside_variance.sqrt())) if downside_variance > 0 else None
    return {
        "sharpe": sharpe,
        "sortino": sortino,
        "observations": len(returns),
    }


def _canonical_decimal(value: Decimal) -> str:
    result = canonical_decimal(value, path="$")
    if result is None:  # pragma: no cover - non-null Decimal contract
        raise AssertionError("canonical decimal unexpectedly returned null")
    return result


def _sample_equity_points(
    points: Sequence[dict[str, Any]],
    *,
    required_index: int,
) -> list[dict[str, Any]]:
    if len(points) <= MAX_EQUITY_POINTS:
        return list(points)
    step = (len(points) - 1) / (MAX_EQUITY_POINTS - 1)
    selected = {0, len(points) - 1, required_index}
    selected.update(round(index * step) for index in range(MAX_EQUITY_POINTS))
    return [points[index] for index in sorted(selected)]


def _group(
    trades: Sequence[Mapping[str, Any]],
    source_key: str,
    output_key: str,
    *,
    normalize: Any | None = None,
) -> list[dict[str, Any]]:
    buckets: dict[str, _Aggregate] = {}
    for trade in trades:
        raw = trade.get(source_key)
        key = str(raw) if raw is not None else "(none)"
        if normalize is not None:
            key = str(normalize(key))
        buckets.setdefault(key, _Aggregate()).add(trade)
    return [aggregate.export(output_key, key) for key, aggregate in buckets.items()]


def _group_by_period(
    trades: Sequence[Mapping[str, Any]],
    date_format: str,
    output_key: str,
    starting_balance: Decimal,
) -> list[dict[str, Any]]:
    buckets: dict[str, _Aggregate] = {}
    for trade in sorted(
        trades,
        key=lambda item: (
            _integer(item.get("close_timestamp_ms")),
            _integer(item.get("sequence")),
        ),
    ):
        closed = datetime.fromtimestamp(
            _integer(trade.get("close_timestamp_ms")) / 1_000,
            tz=UTC,
        )
        key = closed.strftime(date_format)
        buckets.setdefault(key, _Aggregate()).add(trade)

    equity = starting_balance
    result: list[dict[str, Any]] = []
    for key in sorted(buckets):
        aggregate = buckets[key]
        row = aggregate.export(output_key, key)
        row["starting_equity"] = _number(equity)
        row["return_ratio"] = _number(aggregate.profit_abs / equity) if equity != 0 else None
        equity += aggregate.profit_abs
        row["ending_equity"] = _number(equity)
        result.append(row)
    return result


def _configured_pair_names(run_report: Mapping[str, Any]) -> list[str]:
    """Read the sealed workload pair order without depending on candle files."""

    execution = _mapping(run_report, "execution")
    calibration = _mapping(execution, "workload_calibration")
    identity = _mapping(calibration, "identity")
    raw_pairs = identity.get("pairs")
    if not isinstance(raw_pairs, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_pairs:
        if isinstance(item, Mapping):
            pair = str(item.get("pair", "")).strip()
        elif isinstance(item, str):
            pair = item.strip()
        else:
            continue
        if pair and pair not in seen:
            seen.add(pair)
            result.append(pair)
    return result


def _consecutive_outcomes(
    trades: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    maximum_wins = 0
    maximum_losses = 0
    current_wins = 0
    current_losses = 0
    for trade in trades:
        profit = _trade_profit(trade)
        if profit > 0:
            current_wins += 1
            current_losses = 0
        elif profit < 0:
            current_losses += 1
            current_wins = 0
        else:
            current_wins = 0
            current_losses = 0
        maximum_wins = max(maximum_wins, current_wins)
        maximum_losses = max(maximum_losses, current_losses)
    return maximum_wins, maximum_losses


def _trade_snapshot(trade: Mapping[str, Any]) -> dict[str, Any]:
    profit = _mapping(trade, "profit")
    return {
        "sequence": _integer(trade.get("sequence")),
        "pair": trade.get("pair"),
        "direction": trade.get("direction"),
        "opened_at": _iso_timestamp(_integer(trade.get("open_timestamp_ms"))),
        "closed_at": _iso_timestamp(_integer(trade.get("close_timestamp_ms"))),
        "profit_abs": _number(_decimal(profit.get("absolute"))),
        "profit_ratio": _number(_decimal(profit.get("ratio"))),
        "entry_tag": trade.get("entry_tag"),
        "exit_reason": trade.get("exit_reason"),
        "duration_minutes": _integer(trade.get("duration_minutes")),
    }


def _trade_profit(trade: Mapping[str, Any]) -> Decimal:
    return _decimal(_mapping(trade, "profit").get("absolute"))


def _cagr(
    starting_balance: Decimal,
    final_balance: Decimal,
    timerange: str,
) -> float | None:
    dates = _timerange_dates(timerange)
    if dates is None or starting_balance <= 0 or final_balance <= 0 or dates[1] <= dates[0]:
        return None
    years = (dates[1] - dates[0]).days / 365.2425
    if years <= 0:
        return None
    return _rounded(math.pow(float(final_balance / starting_balance), 1 / years) - 1)


def _timerange_dates(timerange: str) -> tuple[datetime, datetime] | None:
    parts = timerange.split("-", maxsplit=1)
    if len(parts) != 2:
        return None
    try:
        start = datetime.strptime(parts[0][:8], "%Y%m%d").replace(tzinfo=UTC)
        end = datetime.strptime(parts[1][:8], "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return start, end


def _timerange_start_ms(timerange: str) -> int | None:
    dates = _timerange_dates(timerange)
    return int(dates[0].timestamp() * 1_000) if dates is not None else None


def _iso_timestamp(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC)
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )


def _mean(values: Sequence[Decimal | int]) -> float:
    if not values:
        return 0.0
    total = sum((Decimal(value) for value in values), Decimal(0))
    return _number(total / len(values))


def _median(values: Sequence[Decimal | int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(Decimal(value) for value in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return _number(ordered[middle])
    return _number((ordered[middle - 1] + ordered[middle]) / 2)


def _ratio(numerator: int, denominator: int) -> float:
    return _rounded(numerator / denominator) if denominator else 0.0


def _number(value: Decimal) -> float:
    return _rounded(float(value))


def _rounded(value: float) -> float:
    return round(value, 12)


def _optional_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return _rounded(number) if math.isfinite(number) else None


def _optional_decimal_number(value: Decimal | None) -> float | None:
    return _number(value) if value is not None else None


def _decimal(value: Any) -> Decimal:
    number = _optional_decimal(value)
    if number is None:
        raise ValueError(f"expected a finite decimal, got {value!r}")
    return number


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _integer(value: Any) -> int:
    result = _optional_integer(value)
    if result is None:
        raise ValueError(f"expected an integer, got {value!r}")
    return result


def _optional_integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number


def _mapping(value: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    candidate = value.get(key) if value is not None else None
    return candidate if isinstance(candidate, Mapping) else {}


def _sequence(
    value: Mapping[str, Any] | None,
    key: str,
) -> list[Mapping[str, Any]]:
    candidate = value.get(key) if value is not None else None
    if not isinstance(candidate, list):
        return []
    return [item for item in candidate if isinstance(item, Mapping)]
