"""Terminal summary and run-list rendering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..canonical import read_json
from .contracts import (
    _TERMINAL_BREAKDOWN_NAME_LIMIT,
    EQUITY_FILENAME,
    EVIDENCE_INDEX_FILENAME,
    HTML_FILENAME,
    ORDERS_FILENAME,
    SUMMARY_FILENAME,
    TRADES_FILENAME,
    VERIFICATION_FILENAME,
)
from .html_render import _leverage_range
from .values import (
    _compact_path,
    _decimal_text,
    _duration,
    _format_timerange,
    _integer_text,
    _mapping,
    _memory_label,
    _mode_label,
    _money,
    _percent,
    _short_timestamp,
    _signed_money,
    _signed_percent,
    _status_label,
    _summary_currency,
    _terminal_row,
    _truncate,
    _verification_label,
)


def format_terminal_summary(
    summary: Mapping[str, Any],
    run_directory: str | Path,
    *,
    include_breakdowns: bool = False,
) -> str:
    """Render the compact result and optional Freqtrade-style detail tables."""

    run = _mapping(summary, "run")
    performance = _mapping(summary, "performance")
    risk = _mapping(summary, "risk")
    futures = _mapping(summary, "futures")
    activity = _mapping(summary, "activity")
    execution = _mapping(summary, "execution")
    verification = _mapping(summary, "verification")
    context = _mapping(run, "context")
    status = str(run.get("status", "unknown"))
    output = Path(run_directory).resolve()
    lines = [
        "",
        f"NFI BACKTEST — {_status_label(status)}",
        "─" * 62,
        _terminal_row("Strategy", run.get("strategy") or "unknown"),
        _terminal_row("Period", _format_timerange(context.get("timerange"))),
        _terminal_row("Mode", _mode_label(context)),
        _terminal_row(
            "Execution lane",
            (
                "Official Freqtrade fallback (Native blocked)"
                if run.get("execution_lane") == "official"
                else "Native Rust"
            ),
        ),
        _terminal_row(
            "Pairs / trades",
            f"{_integer_text(run.get('pair_count'))} / {_integer_text(activity.get('trades'))}",
        ),
    ]
    if performance:
        currency = _summary_currency(summary)
        lines.extend(
            [
                _terminal_row(
                    "Total profit",
                    f"{_signed_percent(performance.get('return_ratio'))}  "
                    f"({_signed_money(performance.get('profit_total_abs'), currency)})",
                ),
                _terminal_row(
                    "Final balance",
                    _money(performance.get("final_balance"), currency),
                ),
                _terminal_row(
                    "Win rate",
                    f"{_percent(activity.get('win_rate'))}  "
                    f"({_integer_text(activity.get('wins'))}W / "
                    f"{_integer_text(activity.get('losses'))}L / "
                    f"{_integer_text(activity.get('draws'))}D)",
                ),
                _terminal_row(
                    "Max drawdown",
                    _percent(risk.get("max_closed_trade_drawdown_ratio")),
                ),
                _terminal_row(
                    "Profit factor",
                    _decimal_text(performance.get("profit_factor")),
                ),
            ]
        )
        if futures:
            lines.extend(
                [
                    _terminal_row(
                        "Long / short",
                        f"{_integer_text(futures.get('long_trades'))} / "
                        f"{_integer_text(futures.get('short_trades'))}",
                    ),
                    _terminal_row(
                        "Leverage",
                        _leverage_range(futures),
                    ),
                    _terminal_row(
                        "Funding",
                        f"{_signed_money(futures.get('funding_total'), currency)}  "
                        f"({_integer_text(futures.get('funded_trades'))} trades)",
                    ),
                    _terminal_row(
                        "Liquidations / locks",
                        f"{_integer_text(futures.get('liquidation_exits'))} / "
                        f"{_integer_text(futures.get('protection_locks'))}",
                    ),
                ]
            )
    lines.extend(
        [
            _terminal_row(
                "Runtime",
                _duration(execution.get("wall_time_seconds")),
            ),
            _terminal_row("Memory", _memory_label(execution)),
            _terminal_row(
                "Official parity",
                _verification_label(verification),
            ),
        ]
    )
    blockers = summary.get("blockers")
    if isinstance(blockers, list) and run.get("execution_lane") != "official":
        for blocker in blockers[:3]:
            if isinstance(blocker, Mapping):
                lines.append(
                    _terminal_row(
                        "Blocked",
                        f"{blocker.get('code', 'UNKNOWN')}: {blocker.get('message', '')}",
                    )
                )
    if include_breakdowns:
        detail = format_terminal_breakdowns(summary)
        if detail:
            lines.extend(["", detail])
    lines.extend(
        [
            "─" * 62,
            _terminal_row("HTML report", output / HTML_FILENAME),
            _terminal_row("Machine summary", output / SUMMARY_FILENAME),
            _terminal_row("Trades CSV", output / TRADES_FILENAME),
            _terminal_row("Orders CSV", output / ORDERS_FILENAME),
            _terminal_row("Equity CSV", output / EQUITY_FILENAME),
            _terminal_row("Verification JSON", output / VERIFICATION_FILENAME),
            _terminal_row("Evidence index", output / EVIDENCE_INDEX_FILENAME),
        ]
    )
    return "\n".join(lines)


def format_terminal_breakdowns(summary: Mapping[str, Any]) -> str:
    """Render complete performance and Signal/Grind tag tables."""

    performance = _mapping(summary, "performance")
    activity = _mapping(summary, "activity")
    breakdowns = _mapping(summary, "breakdowns")
    tag_analysis = _mapping(summary, "tag_analysis")
    if not performance or not activity:
        return ""
    currency = _summary_currency(summary)
    total = {
        "trades": activity.get("trades"),
        "wins": activity.get("wins"),
        "losses": activity.get("losses"),
        "draws": activity.get("draws"),
        "win_rate": activity.get("win_rate"),
        "profit_abs": performance.get("profit_total_abs"),
        "average_profit_ratio": performance.get("average_profit_ratio"),
    }
    definitions = (
        ("PAIR PERFORMANCE", "PAIR", "pair", breakdowns.get("by_pair")),
        (
            "ENTRY TAG PERFORMANCE",
            "ENTRY TAG",
            "entry_tag",
            breakdowns.get("by_entry_tag"),
        ),
        (
            "SIGNAL TAG PERFORMANCE (OVERLAPPING)",
            "SIGNAL TAG",
            "signal_tag",
            _mapping(tag_analysis, "signal").get("rows"),
        ),
        (
            "EXIT REASON PERFORMANCE",
            "EXIT REASON",
            "exit_reason",
            breakdowns.get("by_exit_reason"),
        ),
        (
            "DIRECTION PERFORMANCE",
            "DIRECTION",
            "direction",
            breakdowns.get("by_direction"),
        ),
    )
    tables = [
        _terminal_breakdown_table(
            value,
            title=title,
            name_header=name_header,
            key=key,
            currency=currency,
            total=total,
        )
        for title, name_header, key, value in definitions
    ]
    grind_table = _terminal_grind_table(_mapping(tag_analysis, "grind"))
    if grind_table:
        tables.append(grind_table)
    return "\n\n".join(table for table in tables if table)


def _terminal_grind_table(grind: Mapping[str, Any]) -> str:
    raw_levels = grind.get("levels")
    levels = (
        [row for row in raw_levels if isinstance(row, Mapping)]
        if isinstance(raw_levels, list)
        else []
    )
    if not levels:
        return ""
    headers = ("LEVEL", "TRADES", "ORDERS", "ENTRIES", "EXITS", "DERISKS", "TAG FORMS")

    def cells(row: Mapping[str, Any]) -> tuple[str, ...]:
        raw_forms = row.get("tag_forms")
        forms = raw_forms if isinstance(raw_forms, list) else []
        return (
            _integer_text(row.get("level")),
            _integer_text(row.get("trades")),
            _integer_text(row.get("orders")),
            _integer_text(row.get("entries")),
            _integer_text(row.get("exits")),
            _integer_text(row.get("derisks")),
            ", ".join(str(form) for form in forms),
        )

    rows = [cells(row) for row in levels]
    total = (
        "TOTAL",
        _integer_text(grind.get("trades")),
        _integer_text(grind.get("orders")),
        _integer_text(sum(int(row.get("entries", 0)) for row in levels)),
        _integer_text(sum(int(row.get("exits", 0)) for row in levels)),
        _integer_text(sum(int(row.get("derisks", 0)) for row in levels)),
        "",
    )
    all_rows = [*rows, total]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in all_rows))
        for index in range(len(headers))
    ]
    widths[-1] = min(widths[-1], _TERMINAL_BREAKDOWN_NAME_LIMIT)

    def render(row: tuple[str, ...]) -> str:
        values = list(row)
        values[-1] = _truncate(values[-1], widths[-1])
        return "  ".join(
            (
                value.ljust(widths[index])
                if index in {0, len(values) - 1}
                else value.rjust(widths[index])
            )
            for index, value in enumerate(values)
        )

    separator = "  ".join("─" * width for width in widths)
    return "\n".join(
        [
            f"GRIND LEVEL ACTIVITY · {len(rows)} levels",
            render(headers),
            separator,
            *(render(row) for row in rows),
            separator,
            render(total),
        ]
    )


def _terminal_breakdown_table(
    value: Any,
    *,
    title: str,
    name_header: str,
    key: str,
    currency: str | None,
    total: Mapping[str, Any],
) -> str:
    raw_rows = [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []
    if not raw_rows:
        return ""

    def cells(row: Mapping[str, Any], name: str) -> tuple[str, ...]:
        return (
            name,
            _integer_text(row.get("trades")),
            _signed_percent(row.get("average_profit_ratio")),
            _signed_money(row.get("profit_abs"), currency),
            _percent(row.get("win_rate")),
            (
                f"{_integer_text(row.get('wins'))} / "
                f"{_integer_text(row.get('draws'))} / "
                f"{_integer_text(row.get('losses'))}"
            ),
        )

    rows = [cells(row, str(row.get(key, "(none)"))) for row in raw_rows]
    total_cells = cells(total, "TOTAL")
    headers = (name_header, "TRADES", "AVG PROFIT", "TOTAL PROFIT", "WIN RATE", "W / D / L")
    all_rows = [*rows, total_cells]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in all_rows))
        for index in range(len(headers))
    ]
    widths[0] = min(widths[0], _TERMINAL_BREAKDOWN_NAME_LIMIT)

    def render(row: tuple[str, ...]) -> str:
        values = list(row)
        values[0] = _truncate(values[0], widths[0])
        return "  ".join(
            value.ljust(widths[index]) if index == 0 else value.rjust(widths[index])
            for index, value in enumerate(values)
        )

    header = render(headers)
    separator = "  ".join("─" * width for width in widths)
    return "\n".join(
        [
            f"{title} · {len(rows)} rows",
            header,
            separator,
            *(render(row) for row in rows),
            separator,
            render(total_cells),
        ]
    )


def format_run_list(records: Sequence[Mapping[str, Any]]) -> str:
    """Format the durable registry as a scan-friendly table."""

    if not records:
        return "No research runs are registered."
    columns = (
        ("UPDATED", 20),
        ("STATUS", 13),
        ("LANE", 9),
        ("STRATEGY", 24),
        ("PAIRS", 5),
        ("TRADES", 7),
        ("RUN ID", 12),
        ("OUTPUT", 36),
    )
    header = "  ".join(name.ljust(width) for name, width in columns)
    separator = "  ".join("─" * width for _, width in columns)
    rows = [header, separator]
    for record in records:
        values = (
            _short_timestamp(record.get("updated_at")),
            str(record.get("status", "unknown")),
            str(record.get("selected_lane") or "native"),
            str(record.get("strategy_class", "unknown")),
            _integer_text(record.get("pair_count")),
            _integer_text(
                record.get("official_trade_count")
                if record.get("selected_lane") == "official"
                else record.get("trade_count")
            ),
            str(record.get("run_id", ""))[:12],
            _compact_path(record.get("output_directory")),
        )
        rows.append(
            "  ".join(
                _truncate(value, width).ljust(width)
                for value, (_, width) in zip(values, columns, strict=True)
            )
        )
    return "\n".join(rows)


def format_run_record(
    record: Mapping[str, Any],
    *,
    include_breakdowns: bool = False,
) -> str:
    report = record.get("report")
    if not isinstance(report, Mapping):
        return (
            f"Run {record.get('run_id', 'unknown')}\n"
            f"Status: {record.get('status', 'unknown')}\n"
            f"Output: {record.get('output_directory', 'unknown')}\n"
            "The run report is no longer available at the registered path."
        )
    output = Path(str(record["output_directory"]))
    summary_path = output / SUMMARY_FILENAME
    if summary_path.is_file():
        summary = read_json(summary_path)
        if isinstance(summary, dict):
            return format_terminal_summary(
                summary,
                output,
                include_breakdowns=include_breakdowns,
            )
    return (
        f"Run {record.get('run_id', 'unknown')}\n"
        f"Status: {record.get('status', 'unknown')}\n"
        f"Strategy: {record.get('strategy_class', 'unknown')}\n"
        f"Pairs / trades: {record.get('pair_count', 0)} / "
        f"{record.get('trade_count', 0)}\n"
        f"Output: {output}\n"
        f"Generate the human report with: nfi-bte report {output}"
    )
