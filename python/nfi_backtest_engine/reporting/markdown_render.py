"""Freqtrade-style Markdown result rendering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .values import (
    _compact_path,
    _decimal_text,
    _duration,
    _format_timerange,
    _integer_text,
    _iso_timestamp,
    _leverage_range,
    _mapping,
    _memory_label,
    _minutes,
    _mode_label,
    _money,
    _percent,
    _short_timestamp,
    _signed_money,
    _signed_percent,
    _status_label,
    _summary_currency,
    _verification_label,
)

_RECENT_TRADE_LIMIT = 50


def _render_markdown(
    summary: Mapping[str, Any],
    surface: Mapping[str, Any] | None,
    evidence_index: Mapping[str, Any],
) -> str:
    """Render one portable Markdown report without scripts or external assets."""

    run = _mapping(summary, "run")
    activity = _mapping(summary, "activity")
    performance = _mapping(summary, "performance")
    risk = _mapping(summary, "risk")
    execution = _mapping(summary, "execution")
    verification = _mapping(summary, "verification")
    context = _mapping(run, "context")
    currency = _summary_currency(summary)
    status = str(run.get("status", "unknown"))
    strategy = str(run.get("strategy") or "NFI")
    trade_count = _integer(activity.get("trades"))

    lines = [
        "# Backtest Result",
        "",
        _banner(strategy, _status_label(status)),
        "",
        "## Backtest Overview",
        "",
        _table(
            ("Field", "Value"),
            (
                ("Strategy", strategy),
                ("Period", _format_timerange(context.get("timerange"))),
                ("Mode", _mode_label(context)),
                ("Execution lane", _execution_lane(run)),
                ("Pairs", _integer_text(run.get("pair_count"))),
                ("Trades", _integer_text(activity.get("trades"))),
                ("Run ID", _short_hash(run.get("id"))),
                ("Created", _short_timestamp(run.get("created_at"))),
            ),
        ),
        "",
        "## Summary Metrics",
        "",
    ]
    if performance:
        lines.extend(
            [
                _summary_metrics(
                    performance,
                    risk,
                    activity,
                    execution,
                    verification,
                    currency=currency,
                ),
                "",
            ]
        )
    else:
        lines.extend(
            [
                "No simulation result is available. Inputs were prepared, "
                "but no backtest result was produced.",
                "",
                _table(
                    ("Metric", "Value"),
                    (
                        ("Runtime", _duration(execution.get("wall_time_seconds"))),
                        ("Memory", _memory_label(execution)),
                        ("Official parity", _verification_label(verification)),
                    ),
                ),
                "",
            ]
        )

    if trade_count == 0 and performance:
        lines.extend(
            [
                "> **No trades were opened.** The selected pairs and timerange "
                "produced no completed entries. This is a valid zero-trade result, "
                "not an execution error.",
                "",
            ]
        )

    lines.extend(_breakdown_sections(summary, currency=currency))
    lines.extend(_futures_section(summary, currency=currency))
    lines.extend(_trade_section(surface, currency=currency))
    lines.extend(_order_section(surface))
    lines.extend(_verification_section(verification))
    lines.extend(_artifact_section(summary, evidence_index))
    lines.extend(_notice_section(summary))
    return "\n".join(lines).rstrip() + "\n"


def _summary_metrics(
    performance: Mapping[str, Any],
    risk: Mapping[str, Any],
    activity: Mapping[str, Any],
    execution: Mapping[str, Any],
    verification: Mapping[str, Any],
    *,
    currency: str | None,
) -> str:
    rows: list[tuple[Any, Any]] = [
        ("Starting balance", _money(performance.get("starting_balance"), currency)),
        ("Final balance", _money(performance.get("final_balance"), currency)),
        ("Absolute profit", _signed_money(performance.get("profit_total_abs"), currency)),
        ("Total profit", _signed_percent(performance.get("return_ratio"))),
        ("Total trades", _integer_text(activity.get("trades"))),
        (
            "Wins / Draws / Losses",
            f"{_integer_text(activity.get('wins'))} / "
            f"{_integer_text(activity.get('draws'))} / "
            f"{_integer_text(activity.get('losses'))}",
        ),
        ("Win rate", _percent(activity.get("win_rate"))),
    ]
    if activity.get("trades"):
        best = _mapping(performance, "best_trade")
        worst = _mapping(performance, "worst_trade")
        rows.extend(
            [
                ("Average profit", _signed_percent(performance.get("average_profit_ratio"))),
                ("Average duration", _minutes(activity.get("average_duration_minutes"))),
                ("Best trade", _trade_metric(best, currency=currency)),
                ("Worst trade", _trade_metric(worst, currency=currency)),
            ]
        )
    rows.extend(
        [
            ("Profit factor", _decimal_text(performance.get("profit_factor"))),
            (
                "Max closed-trade drawdown",
                f"{_percent(risk.get('max_closed_trade_drawdown_ratio'))} "
                f"({_money(risk.get('max_closed_trade_drawdown_abs'), currency)})",
            ),
            ("Runtime", _duration(execution.get("wall_time_seconds"))),
            ("Memory", _memory_label(execution)),
            ("Official parity", _verification_label(verification)),
        ]
    )
    return _table(("Metric", "Value"), rows)


def _breakdown_sections(
    summary: Mapping[str, Any],
    *,
    currency: str | None,
) -> list[str]:
    breakdowns = _mapping(summary, "breakdowns")
    tag_analysis = _mapping(summary, "tag_analysis")
    activity = _mapping(summary, "activity")
    performance = _mapping(summary, "performance")
    total = {
        "trades": activity.get("trades"),
        "wins": activity.get("wins"),
        "draws": activity.get("draws"),
        "losses": activity.get("losses"),
        "win_rate": activity.get("win_rate"),
        "average_profit_ratio": performance.get("average_profit_ratio"),
        "profit_abs": performance.get("profit_total_abs"),
    }
    definitions = (
        ("Backtesting Report", "Pair", "pair", breakdowns.get("by_pair")),
        ("Enter Tag Stats", "Enter Tag", "entry_tag", breakdowns.get("by_entry_tag")),
        (
            "Signal Tag Stats (overlapping)",
            "Signal Tag",
            "signal_tag",
            _mapping(tag_analysis, "signal").get("rows"),
        ),
        ("Exit Reason Stats", "Exit Reason", "exit_reason", breakdowns.get("by_exit_reason")),
        ("Direction Stats", "Direction", "direction", breakdowns.get("by_direction")),
        ("Yearly Breakdown", "Year", "year", breakdowns.get("by_year")),
        ("Monthly Breakdown", "Month", "month", breakdowns.get("by_month")),
    )
    sections: list[str] = []
    for title, label, key, value in definitions:
        rows = _mapping_rows(value)
        if not rows:
            continue
        sections.extend(
            [
                f"## {title}",
                "",
                _performance_table(
                    rows,
                    label=label,
                    key=key,
                    currency=currency,
                    total=total,
                ),
                "",
            ]
        )
    grind = _mapping(tag_analysis, "grind")
    levels = _mapping_rows(grind.get("levels"))
    if levels:
        sections.extend(
            [
                "## Grind Level Activity",
                "",
                _table(
                    ("Level", "Trades", "Orders", "Entries", "Exits", "Derisks", "Tag forms"),
                    tuple(
                        (
                            row.get("level"),
                            row.get("trades"),
                            row.get("orders"),
                            row.get("entries"),
                            row.get("exits"),
                            row.get("derisks"),
                            ", ".join(str(value) for value in row.get("tag_forms", [])),
                        )
                        for row in levels
                    ),
                ),
                "",
            ]
        )
    return sections


def _performance_table(
    rows: Sequence[Mapping[str, Any]],
    *,
    label: str,
    key: str,
    currency: str | None,
    total: Mapping[str, Any],
) -> str:
    rendered = [
        (
            row.get(key, "(none)"),
            _integer_text(row.get("trades")),
            _signed_percent(row.get("average_profit_ratio")),
            _signed_money(row.get("profit_abs"), currency),
            _percent(row.get("win_rate")),
            f"{_integer_text(row.get('wins'))} / {_integer_text(row.get('draws'))} / "
            f"{_integer_text(row.get('losses'))}",
        )
        for row in rows
    ]
    rendered.append(
        (
            "TOTAL",
            _integer_text(total.get("trades")),
            _signed_percent(total.get("average_profit_ratio")),
            _signed_money(total.get("profit_abs"), currency),
            _percent(total.get("win_rate")),
            f"{_integer_text(total.get('wins'))} / {_integer_text(total.get('draws'))} / "
            f"{_integer_text(total.get('losses'))}",
        )
    )
    return _table(
        (label, "Trades", "Avg Profit", "Total Profit", "Win Rate", "W / D / L"),
        rendered,
    )


def _futures_section(summary: Mapping[str, Any], *, currency: str | None) -> list[str]:
    futures = _mapping(summary, "futures")
    if not futures:
        return []
    return [
        "## Futures Lifecycle",
        "",
        _table(
            ("Metric", "Value"),
            (
                ("Margin mode", futures.get("margin_mode") or "unknown"),
                (
                    "Long / short trades",
                    f"{_integer_text(futures.get('long_trades'))} / "
                    f"{_integer_text(futures.get('short_trades'))}",
                ),
                ("Leverage", _leverage_range(futures)),
                ("Funding total", _signed_money(futures.get("funding_total"), currency)),
                ("Funded trades", _integer_text(futures.get("funded_trades"))),
                ("Liquidation exits", _integer_text(futures.get("liquidation_exits"))),
                ("Protection locks", _integer_text(futures.get("protection_locks"))),
            ),
        ),
        "",
    ]


def _trade_section(surface: Mapping[str, Any] | None, *, currency: str | None) -> list[str]:
    trades = _surface_trades(surface)
    if not trades:
        return ["## Trades", "", "No completed trades.", ""]
    recent = sorted(
        trades,
        key=lambda trade: (
            _integer(trade.get("close_timestamp_ms")),
            _integer(trade.get("sequence")),
        ),
        reverse=True,
    )[:_RECENT_TRADE_LIMIT]
    note = (
        f"Showing the most recent {_RECENT_TRADE_LIMIT} of {len(trades)} trades. "
        "See `trades.csv` for the complete export."
        if len(trades) > _RECENT_TRADE_LIMIT
        else f"Showing all {len(trades)} trades."
    )
    rows = []
    for trade in recent:
        profit = _mapping(trade, "profit")
        rows.append(
            (
                trade.get("sequence"),
                trade.get("pair"),
                trade.get("direction"),
                _iso_timestamp(trade.get("open_timestamp_ms")),
                _iso_timestamp(trade.get("close_timestamp_ms")),
                _signed_percent(profit.get("ratio")),
                _signed_money(profit.get("absolute"), currency),
                trade.get("entry_tag") or "(none)",
                trade.get("exit_reason") or "(none)",
            )
        )
    return [
        "## Trades",
        "",
        note,
        "",
        _table(
            (
                "#",
                "Pair",
                "Side",
                "Opened (UTC)",
                "Closed (UTC)",
                "Profit %",
                "Profit",
                "Enter Tag",
                "Exit Reason",
            ),
            rows,
        ),
        "",
    ]


def _order_section(surface: Mapping[str, Any] | None) -> list[str]:
    trades = _surface_trades(surface)
    orders = [
        order for trade in trades for order in trade.get("orders", []) if isinstance(order, Mapping)
    ]
    partial_exits = sum(
        order.get("is_entry") is False and index < len(trade.get("orders", [])) - 1
        for trade in trades
        for index, order in enumerate(trade.get("orders", []))
        if isinstance(order, Mapping)
    )
    if not orders:
        return []
    return [
        "## Orders and Position Changes",
        "",
        _table(
            ("Metric", "Value"),
            (
                ("Orders", _integer_text(len(orders))),
                (
                    "Entry orders",
                    _integer_text(sum(order.get("is_entry") is True for order in orders)),
                ),
                (
                    "Exit orders",
                    _integer_text(sum(order.get("is_entry") is False for order in orders)),
                ),
                ("Partial exits", _integer_text(partial_exits)),
                ("Complete order export", "orders.csv"),
                ("Closed-trade equity", "equity.csv"),
            ),
        ),
        "",
        "> Candle-level equity is not invented. Drawdown, Sharpe, and Sortino use "
        "closed-trade events and are not annualized.",
        "",
    ]


def _verification_section(verification: Mapping[str, Any]) -> list[str]:
    stages = _mapping_rows(verification.get("stages"))
    identities = _mapping(verification, "identities")
    lines = [
        "## Official Verification",
        "",
        _table(
            ("Field", "Value"),
            (
                ("Status", _verification_label(verification)),
                ("Source", _compact_path(verification.get("source"))),
                ("Source SHA-256", _short_hash(verification.get("source_sha256"))),
                ("Difference", _difference_summary(verification.get("difference"))),
            ),
        ),
        "",
    ]
    if str(verification.get("status") or "not_run").lower() == "not_run":
        return lines
    if stages:
        lines.extend(
            [
                "### Verification Stages",
                "",
                _table(
                    ("Stage", "Status", "Detail"),
                    tuple(
                        (stage.get("id"), stage.get("status"), stage.get("detail"))
                        for stage in stages
                    ),
                ),
                "",
            ]
        )
    identity_rows = tuple(
        (label, _short_hash(identities.get(key)))
        for key, label in (
            ("strategy_sha256", "Strategy SHA-256"),
            ("certified_strategy_sha256", "Certified strategy SHA-256"),
            ("package_version", "Package version"),
            ("package_sha256", "Package SHA-256"),
            ("certified_package_sha256", "Certified package SHA-256"),
            ("native_binary_sha256", "Native binary SHA-256"),
            ("trade_surface_sha256", "Trade surface SHA-256"),
        )
    )
    if identities:
        lines.extend(["### Bound Identities", "", _table(("Identity", "Value"), identity_rows), ""])
    return lines


def _artifact_section(
    summary: Mapping[str, Any],
    evidence_index: Mapping[str, Any],
) -> list[str]:
    entries = _mapping_rows(evidence_index.get("entries"))
    artifacts = _mapping(summary, "artifacts")
    rows = []
    seen = set()
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        seen.add(path)
        rows.append((entry.get("role"), path))
    for role, path in artifacts.items():
        if not isinstance(path, str) or not path or path in seen or path == "report.md":
            continue
        rows.append((role, path))
    return [
        "## Result Files",
        "",
        _table(("Role", "File"), rows),
        "",
    ]


def _notice_section(summary: Mapping[str, Any]) -> list[str]:
    notices = summary.get("notices")
    values = [str(value) for value in notices] if isinstance(notices, list) else []
    blockers = _mapping_rows(summary.get("blockers"))
    if not values and not blockers:
        return []
    lines = ["## Notes", ""]
    lines.extend(f"- {_cell(value)}" for value in values)
    lines.extend(
        f"- **{_cell(blocker.get('code') or 'BLOCKED')}**: {_cell(blocker.get('message') or '')}"
        for blocker in blockers
    )
    lines.append("")
    return lines


def _table(
    headers: Sequence[Any],
    rows: Sequence[Sequence[Any]],
) -> str:
    normalized = [
        tuple(_ascii_cell(value) for value in row) for row in (tuple(headers), *tuple(rows))
    ]
    column_count = len(headers)
    widths = [
        min(
            48,
            max(len(row[index]) if index < len(row) else 0 for row in normalized),
        )
        for index in range(column_count)
    ]
    border = f"+{'+'.join('-' * (width + 2) for width in widths)}+"

    def render(row: Sequence[str]) -> str:
        values = [
            _truncate_cell(row[index] if index < len(row) else "", widths[index])
            for index in range(column_count)
        ]
        return f"| {' | '.join(value.ljust(widths[index]) for index, value in enumerate(values))} |"

    return "\n".join(
        ["```text", border, render(normalized[0]), border]
        + [render(row) for row in normalized[1:]]
        + [border, "```"]
    )


def _banner(strategy: str, status: str) -> str:
    width = 62
    title = "NFI BACKTEST RESULT"
    return "\n".join(
        (
            "```text",
            f"+{'-' * width}+",
            f"| {title.ljust(width - 2)} |",
            f"| {_truncate_cell(strategy, width - 2).ljust(width - 2)} |",
            f"| {_truncate_cell(status, width - 2).ljust(width - 2)} |",
            f"+{'-' * width}+",
            "```",
        )
    )


def _ascii_cell(value: Any) -> str:
    if value is None:
        return "-"
    text = (
        str(value)
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\t", " ")
        .replace("|", "/")
        .replace("`", "'")
    )
    return text.strip() or "-"


def _truncate_cell(value: str, width: int) -> str:
    return value if len(value) <= width else f"{value[: max(0, width - 3)]}..."


def _short_hash(value: Any) -> str:
    text = str(value or "")
    return f"{text[:12]}..." if len(text) > 16 else text or "-"


def _difference_summary(value: Any) -> str:
    if isinstance(value, Mapping):
        path = value.get("path") or "unknown path"
        reason = value.get("reason") or "values differ"
        return f"{path}: {reason}"
    return str(value) if value else "-"


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    text = (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", "\\\\")
        .replace("|", "\\|")
    )
    return text.replace("\r\n", "<br>").replace("\n", "<br>") or "—"


def _mapping_rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _surface_trades(surface: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if surface is None:
        return []
    value = surface.get("trades")
    return (
        [trade for trade in value if isinstance(trade, Mapping)] if isinstance(value, list) else []
    )


def _trade_metric(trade: Mapping[str, Any], *, currency: str | None) -> str:
    if not trade:
        return "—"
    return (
        f"{trade.get('pair') or 'unknown'} "
        f"{_signed_percent(trade.get('profit_ratio'))} "
        f"({_signed_money(trade.get('profit_abs'), currency)})"
    )


def _execution_lane(run: Mapping[str, Any]) -> str:
    return (
        "Official Freqtrade fallback (Native blocked)"
        if run.get("execution_lane") == "official"
        else "Native Rust"
    )


def _integer(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
