"""Terminal summary and run-list rendering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..canonical import read_json
from .contracts import (
    _TERMINAL_BREAKDOWN_NAME_LIMIT,
    EQUITY_FILENAME,
    EVIDENCE_INDEX_FILENAME,
    MARKDOWN_FILENAME,
    ORDERS_FILENAME,
    SUMMARY_FILENAME,
    TRADES_FILENAME,
    VERIFICATION_FILENAME,
)
from .values import (
    _compact_path,
    _decimal_text,
    _duration,
    _float,
    _format_timerange,
    _integer_text,
    _leverage_range,
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
    _truncate,
    _verification_label,
)


def format_terminal_summary(
    summary: Mapping[str, Any],
    run_directory: str | Path,
    *,
    include_breakdowns: bool = False,
) -> str:
    """Render either a compact card or the complete Freqtrade-style report."""
    output = Path(run_directory).resolve()
    if include_breakdowns:
        report = format_terminal_breakdowns(summary)
        footer = _terminal_footer(summary, output)
        return f"{report}\n\n{footer}" if report else footer

    run = _mapping(summary, "run")
    performance = _mapping(summary, "performance")
    risk = _mapping(summary, "risk")
    activity = _mapping(summary, "activity")
    execution = _mapping(summary, "execution")
    verification = _mapping(summary, "verification")
    futures = _mapping(summary, "futures")
    context = _mapping(run, "context")
    currency = _summary_currency(summary)
    rows = [
        ("Strategy", str(run.get("strategy") or "unknown")),
        ("Period", _format_timerange(context.get("timerange"))),
        ("Mode", _mode_label(context)),
        (
            "Pairs / trades",
            f"{_integer_text(run.get('pair_count'))} / {_integer_text(activity.get('trades'))}",
        ),
        (
            "Total profit",
            f"{_signed_percent(performance.get('return_ratio'))}  "
            f"({_signed_money(performance.get('profit_total_abs'), currency)})",
        ),
        ("Final balance", _money(performance.get("final_balance"), currency)),
        ("Max drawdown", _percent(risk.get("max_closed_trade_drawdown_ratio"))),
    ]
    if futures:
        rows.extend(
            [
                (
                    "Long / short",
                    f"{_integer_text(futures.get('long_trades'))} / "
                    f"{_integer_text(futures.get('short_trades'))}",
                ),
                ("Leverage", _leverage_range(futures)),
                (
                    "Liquidations / locks",
                    f"{_integer_text(futures.get('liquidation_exits'))} / "
                    f"{_integer_text(futures.get('protection_locks'))}",
                ),
            ]
        )
    rows.extend(
        [
            ("Runtime", _duration(execution.get("wall_time_seconds"))),
            ("Memory", _memory_label(execution)),
            ("Official parity", _verification_label(verification)),
        ]
    )
    card = _box_table(
        f"NFI BACKTEST — {_status_label(str(run.get('status', 'unknown')))}",
        ("METRIC", "VALUE"),
        rows,
    )
    return f"{card}\n\n{_terminal_footer(summary, output)}"


def format_terminal_breakdowns(summary: Mapping[str, Any]) -> str:
    """Render the full result in Freqtrade's terminal-report structure."""
    performance = _mapping(summary, "performance")
    activity = _mapping(summary, "activity")
    breakdowns = _mapping(summary, "breakdowns")
    tag_analysis = _mapping(summary, "tag_analysis")
    if not performance or not activity:
        return ""

    total = _total_row(performance, activity)
    sections = [
        _performance_table(
            "BACKTESTING REPORT",
            breakdowns.get("by_pair"),
            name_headers=("Pair",),
            name_keys=("pair",),
            total=total,
        ),
        _performance_table(
            "LEFT OPEN TRADES REPORT",
            breakdowns.get("by_open_pair"),
            name_headers=("Pair",),
            name_keys=("pair",),
            total=_rows_total(
                breakdowns.get("by_open_pair"),
                starting_balance=performance.get("starting_balance"),
            ),
        ),
        _performance_table(
            "ENTER TAG STATS",
            breakdowns.get("by_entry_tag"),
            name_headers=("Enter Tag",),
            name_keys=("entry_tag",),
            total=total,
            count_header="Entries",
        ),
        _performance_table(
            "EXIT REASON STATS",
            breakdowns.get("by_exit_reason"),
            name_headers=("Exit Reason",),
            name_keys=("exit_reason",),
            total=total,
            count_header="Exits",
        ),
        _performance_table(
            "MIXED TAG STATS",
            breakdowns.get("by_entry_exit"),
            name_headers=("Enter Tag", "Exit Reason"),
            name_keys=("entry_tag", "exit_reason"),
            total=total,
        ),
    ]
    signal_rows = _mapping(tag_analysis, "signal").get("rows")
    if isinstance(signal_rows, list) and signal_rows:
        sections.append(
            _performance_table(
                "SIGNAL TAG STATS · OVERLAPPING",
                signal_rows,
                name_headers=("Signal Tag",),
                name_keys=("signal_tag",),
                total=_rows_total(
                    signal_rows,
                    starting_balance=performance.get("starting_balance"),
                ),
            )
        )
    grind_table = _terminal_grind_table(_mapping(tag_analysis, "grind"))
    if grind_table:
        sections.append(grind_table)
    sections.extend(
        [
            _summary_metrics_table(summary),
            _strategy_summary_table(summary),
        ]
    )
    return "\n\n".join(section for section in sections if section)


def _performance_table(
    title: str,
    value: Any,
    *,
    name_headers: tuple[str, ...],
    name_keys: tuple[str, ...],
    total: Mapping[str, Any],
    count_header: str = "Trades",
) -> str:
    rows = [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []
    starting_balance = _float(total.get("starting_balance"))

    def cells(row: Mapping[str, Any], *, total_row: bool = False) -> tuple[str, ...]:
        names = tuple(
            (
                "TOTAL"
                if total_row and index == 0
                else ""
                if total_row
                else str(row.get(key, "(none)"))
            )
            for index, key in enumerate(name_keys)
        )
        profit_abs = _float(row.get("profit_abs"))
        total_ratio = (
            profit_abs / starting_balance
            if (
                profit_abs is not None
                and starting_balance is not None
                and starting_balance != 0
            )
            else None
        )
        return (
            *names,
            _integer_text(row.get("trades")),
            _percent_number(row.get("average_profit_ratio")),
            _decimal_number(row.get("profit_abs")),
            _percent_number(total_ratio),
            _freqtrade_duration(row.get("average_duration_minutes")),
            _outcome_cell(row),
        )

    table_rows = [cells(row) for row in rows]
    table_rows.append(cells(total, total_row=True))
    headers = (
        *name_headers,
        count_header,
        "Avg Profit %",
        "Tot Profit",
        "Tot Profit %",
        "Avg Duration",
        "Win  Draw  Loss  Win%",
    )
    right = frozenset(range(len(name_headers), len(headers)))
    return _box_table(title, headers, table_rows, right_align=right)


def _summary_metrics_table(summary: Mapping[str, Any]) -> str:
    run = _mapping(summary, "run")
    context = _mapping(run, "context")
    performance = _mapping(summary, "performance")
    risk = _mapping(summary, "risk")
    futures = _mapping(summary, "futures")
    activity = _mapping(summary, "activity")
    execution = _mapping(summary, "execution")
    verification = _mapping(summary, "verification")
    breakdowns = _mapping(summary, "breakdowns")
    currency = _summary_currency(summary)
    timerange = str(context.get("timerange") or "")
    start, end, days = _timerange_parts(timerange)
    trades = _float(activity.get("trades")) or 0.0
    profit = _float(performance.get("profit_total_abs"))
    raw_day_rows = breakdowns.get("by_day")
    day_rows = (
        [row for row in raw_day_rows if isinstance(row, Mapping)]
        if isinstance(raw_day_rows, list)
        else []
    )
    best_pair = _extreme_row(breakdowns.get("by_pair"), maximum=True)
    worst_pair = _extreme_row(breakdowns.get("by_pair"), maximum=False)
    best_day = _extreme_row(day_rows, maximum=True)
    worst_day = _extreme_row(day_rows, maximum=False)
    best_trade = _mapping(performance, "best_trade")
    worst_trade = _mapping(performance, "worst_trade")
    winning_duration = _mapping(activity, "winning_duration_minutes")
    losing_duration = _mapping(activity, "losing_duration_minutes")
    points = _equity_points(summary)
    balances = [
        balance
        for point in points
        if (balance := _float(point.get("equity"))) is not None
    ]
    min_balance = min(balances, default=None)
    max_balance = max(balances, default=None)
    rows: list[tuple[str, str]] = [
        ("Backtesting from", start),
        ("Backtesting to", end),
        ("Trading Mode", _mode_label(context)),
        ("Max open trades", _integer_text(activity.get("max_open_trades"))),
        ("", ""),
        (
            "Total / Daily Avg Trades",
            f"{_integer_text(activity.get('trades'))} / {trades / days:.2f}",
        ),
        ("Starting balance", _money(performance.get("starting_balance"), currency)),
        ("Final balance", _money(performance.get("final_balance"), currency)),
        ("Absolute profit", _signed_money(profit, currency)),
        ("Total profit %", _signed_percent(performance.get("return_ratio"))),
        ("CAGR %", _signed_percent(performance.get("cagr_ratio"))),
        ("Sharpe (closed trades)", _decimal_text(risk.get("closed_trade_sharpe"))),
        ("Sortino (closed trades)", _decimal_text(risk.get("closed_trade_sortino"))),
        ("Calmar (closed trades)", "—"),
        ("SQN", "—"),
        ("Mean profit p-value", "—"),
        ("Profit factor", _decimal_text(performance.get("profit_factor"))),
        (
            "Expectancy (Ratio)",
            f"{_signed_money(performance.get('expectancy_abs'), currency)} "
            f"({_decimal_text(performance.get('expectancy_ratio'))})",
        ),
        (
            "Avg. daily profit",
            _signed_money(profit / days if profit is not None else None, currency),
        ),
        ("Avg. stake amount", _money(activity.get("average_stake_amount"), currency)),
        ("Market change", "— · not captured by Native trade surface"),
        ("Total trade volume", _money(activity.get("total_volume"), currency)),
    ]
    if futures:
        rows.extend(
            [
                ("", ""),
                (
                    "Long / Short trades",
                    f"{_integer_text(futures.get('long_trades'))} / "
                    f"{_integer_text(futures.get('short_trades'))}",
                ),
                (
                    "Long / Short profit",
                    f"{_signed_money(futures.get('long_profit_abs'), currency)} / "
                    f"{_signed_money(futures.get('short_profit_abs'), currency)}",
                ),
                ("Leverage", _leverage_range(futures)),
                ("Funding", _signed_money(futures.get("funding_total"), currency)),
                (
                    "Liquidations / locks",
                    f"{_integer_text(futures.get('liquidation_exits'))} / "
                    f"{_integer_text(futures.get('protection_locks'))}",
                ),
            ]
        )
    rows.extend(
        [
            ("", ""),
            ("Best Pair", _named_profit(best_pair, "pair", performance)),
            ("Worst Pair", _named_profit(worst_pair, "pair", performance)),
            ("Best trade", _trade_profit_label(best_trade)),
            ("Worst trade", _trade_profit_label(worst_trade)),
            ("Best day", _signed_money(best_day.get("profit_abs"), currency)),
            ("Worst day", _signed_money(worst_day.get("profit_abs"), currency)),
            ("Days win/draw/lose", _day_outcomes(day_rows)),
            ("Min/Max/Avg. Duration Winners", _duration_range(winning_duration)),
            ("Min/Max/Avg. Duration Losers", _duration_range(losing_duration)),
            (
                "Max Consecutive Wins / Loss",
                f"{_integer_text(risk.get('maximum_consecutive_wins'))} / "
                f"{_integer_text(risk.get('maximum_consecutive_losses'))}",
            ),
            ("Rejected Entry signals", _integer_text(activity.get("rejected_signals"))),
            (
                "Entry/Exit Timeouts",
                f"{_integer_text(activity.get('entry_timeouts'))} / "
                f"{_integer_text(activity.get('exit_timeouts'))}",
            ),
            ("", ""),
            (
                "Min/Max balance (closed trades)",
                f"{_money(min_balance, currency)} / {_money(max_balance, currency)}",
            ),
            (
                "Max % of account underwater",
                _percent(risk.get("max_closed_trade_drawdown_ratio")),
            ),
            (
                "Absolute drawdown",
                f"{_money(risk.get('max_closed_trade_drawdown_abs'), currency)} "
                f"({_percent(risk.get('max_closed_trade_drawdown_ratio'))})",
            ),
            ("Drawdown duration", _drawdown_duration(risk)),
            ("Drawdown start", _timestamp_label(risk.get("max_drawdown_peak_timestamp_ms"))),
            ("Drawdown end", _timestamp_label(risk.get("max_drawdown_trough_timestamp_ms"))),
            ("", ""),
            ("Runtime", _duration(execution.get("wall_time_seconds"))),
            ("Memory", _memory_label(execution)),
            ("Official parity", _verification_label(verification)),
        ]
    )
    return _box_table("SUMMARY METRICS", ("Metric", "Value"), rows)


def _strategy_summary_table(summary: Mapping[str, Any]) -> str:
    run = _mapping(summary, "run")
    performance = _mapping(summary, "performance")
    risk = _mapping(summary, "risk")
    activity = _mapping(summary, "activity")
    row = (
        str(run.get("strategy") or "unknown"),
        _integer_text(activity.get("trades")),
        _percent_number(performance.get("average_profit_ratio")),
        _decimal_number(performance.get("profit_total_abs")),
        _percent_number(performance.get("return_ratio")),
        _freqtrade_duration(activity.get("average_duration_minutes")),
        _outcome_cell(activity),
        (
            f"{_decimal_number(risk.get('max_closed_trade_drawdown_abs'))}  "
            f"{_percent_number(risk.get('max_closed_trade_drawdown_ratio'))}%"
        ),
    )
    context = _mapping(run, "context")
    timerange = _format_timerange(context.get("timerange"))
    prefix = (
        f"Backtested {timerange} | Max open trades: "
        f"{_integer_text(activity.get('max_open_trades'))}"
    )
    table = _box_table(
        "STRATEGY SUMMARY",
        (
            "Strategy",
            "Trades",
            "Avg Profit %",
            "Tot Profit",
            "Tot Profit %",
            "Avg Duration",
            "Win  Draw  Loss  Win%",
            "Drawdown",
        ),
        [row],
        right_align=frozenset(range(1, 8)),
    )
    return f"{prefix}\n{table}"


def _terminal_grind_table(grind: Mapping[str, Any]) -> str:
    raw_levels = grind.get("levels")
    levels = (
        [row for row in raw_levels if isinstance(row, Mapping)]
        if isinstance(raw_levels, list)
        else []
    )
    if not levels:
        return ""
    rows = [
        (
            _integer_text(row.get("level")),
            _integer_text(row.get("trades")),
            _integer_text(row.get("orders")),
            _integer_text(row.get("entries")),
            _integer_text(row.get("exits")),
            _integer_text(row.get("derisks")),
            ", ".join(str(form) for form in row.get("tag_forms", [])),
        )
        for row in levels
    ]
    rows.append(
        (
            "TOTAL",
            _integer_text(grind.get("trades")),
            _integer_text(grind.get("orders")),
            _integer_text(sum(int(row.get("entries", 0)) for row in levels)),
            _integer_text(sum(int(row.get("exits", 0)) for row in levels)),
            _integer_text(sum(int(row.get("derisks", 0)) for row in levels)),
            "",
        )
    )
    return _box_table(
        "GRIND LEVEL ACTIVITY",
        ("Level", "Trades", "Orders", "Entries", "Exits", "Derisks", "Tag Forms"),
        rows,
        right_align=frozenset(range(1, 6)),
    )


def _box_table(
    title: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    right_align: frozenset[int] = frozenset(),
) -> str:
    safe_rows = [tuple(str(value) for value in row) for row in rows]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in safe_rows))
        for index in range(len(headers))
    ]
    if widths:
        widths[0] = min(widths[0], _TERMINAL_BREAKDOWN_NAME_LIMIT)

    def render(row: Sequence[str]) -> str:
        cells = []
        for index, value in enumerate(row):
            shown = _truncate(value, widths[index])
            cells.append(
                shown.rjust(widths[index])
                if index in right_align
                else shown.ljust(widths[index])
            )
        return "│ " + " │ ".join(cells) + " │"

    top = "┏" + "┳".join("━" * (width + 2) for width in widths) + "┓"
    divider = "┡" + "╇".join("━" * (width + 2) for width in widths) + "┩"
    bottom = "└" + "┴".join("─" * (width + 2) for width in widths) + "┘"
    width = len(top)
    return "\n".join(
        [
            title.center(width),
            top,
            render(headers),
            divider,
            *(render(row) for row in safe_rows),
            bottom,
        ]
    )


def _total_row(
    performance: Mapping[str, Any],
    activity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "trades": activity.get("trades"),
        "wins": activity.get("wins"),
        "losses": activity.get("losses"),
        "draws": activity.get("draws"),
        "win_rate": activity.get("win_rate"),
        "profit_abs": performance.get("profit_total_abs"),
        "average_profit_ratio": performance.get("average_profit_ratio"),
        "average_duration_minutes": activity.get("average_duration_minutes"),
        "starting_balance": performance.get("starting_balance"),
    }


def _rows_total(value: Any, *, starting_balance: Any = None) -> dict[str, Any]:
    rows = [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []
    trades = sum(int(row.get("trades", 0)) for row in rows)
    profit = sum((_float(row.get("profit_abs")) or 0.0 for row in rows), 0.0)
    ratio_sum = sum(
        (
            (_float(row.get("average_profit_ratio")) or 0.0)
            * int(row.get("trades", 0))
            for row in rows
        ),
        0.0,
    )
    duration_sum = sum(
        (
            (_float(row.get("average_duration_minutes")) or 0.0)
            * int(row.get("trades", 0))
            for row in rows
        ),
        0.0,
    )
    wins = sum(int(row.get("wins", 0)) for row in rows)
    draws = sum(int(row.get("draws", 0)) for row in rows)
    losses = sum(int(row.get("losses", 0)) for row in rows)
    return {
        "trades": trades,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "win_rate": wins / trades if trades else 0.0,
        "profit_abs": profit,
        "average_profit_ratio": ratio_sum / trades if trades else 0.0,
        "average_duration_minutes": duration_sum / trades if trades else 0.0,
        "starting_balance": starting_balance,
    }


def _outcome_cell(row: Mapping[str, Any]) -> str:
    win_rate = _float(row.get("win_rate"))
    if win_rate is None:
        return "  —     —     —     —"
    return (
        f"{_integer_text(row.get('wins')):>3}  "
        f"{_integer_text(row.get('draws')):>4}  "
        f"{_integer_text(row.get('losses')):>4}  "
        f"{win_rate * 100:>4.1f}"
    )


def _percent_number(value: Any) -> str:
    number = _float(value)
    return f"{number * 100:.2f}" if number is not None else "—"


def _decimal_number(value: Any) -> str:
    number = _float(value)
    return f"{number:,.3f}" if number is not None else "—"


def _freqtrade_duration(value: Any) -> str:
    minutes = _float(value)
    if minutes is None:
        return "—"
    total = max(0, round(minutes))
    days, remainder = divmod(total, 1_440)
    hours, mins = divmod(remainder, 60)
    return f"{days}d {hours:02d}:{mins:02d}" if days else f"{hours:02d}:{mins:02d}"


def _duration_range(value: Mapping[str, Any]) -> str:
    return " / ".join(
        _freqtrade_duration(value.get(key))
        for key in ("minimum", "maximum", "average")
    )


def _timerange_parts(value: str) -> tuple[str, str, int]:
    parts = value.split("-", maxsplit=1)
    if len(parts) != 2:
        return value or "unknown", value or "unknown", 1
    labels = []
    dates = []
    for token in parts:
        try:
            parsed = datetime.strptime(token[:8], "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            labels.append(token or "open")
        else:
            dates.append(parsed)
            labels.append(parsed.strftime("%Y-%m-%d 00:00:00"))
    days = max(1, (dates[1] - dates[0]).days) if len(dates) == 2 else 1
    return labels[0], labels[1], days


def _extreme_row(value: Any, *, maximum: bool) -> Mapping[str, Any]:
    rows = [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []
    if not rows:
        return {}
    return (max if maximum else min)(rows, key=lambda row: _float(row.get("profit_abs")) or 0.0)


def _named_profit(
    row: Mapping[str, Any],
    key: str,
    performance: Mapping[str, Any],
) -> str:
    name = row.get(key)
    profit = _float(row.get("profit_abs"))
    starting = _float(performance.get("starting_balance"))
    if name is None or profit is None or starting is None or starting == 0:
        return "—"
    return f"{name}  {profit / starting * 100:+.2f}%"


def _trade_profit_label(trade: Mapping[str, Any]) -> str:
    pair = trade.get("pair")
    ratio = _float(trade.get("profit_ratio"))
    return f"{pair}  {ratio * 100:+.2f}%" if pair is not None and ratio is not None else "—"


def _day_outcomes(rows: Sequence[Mapping[str, Any]]) -> str:
    profits = [_float(row.get("profit_abs")) or 0.0 for row in rows]
    wins = sum(value > 0 for value in profits)
    draws = sum(value == 0 for value in profits)
    losses = sum(value < 0 for value in profits)
    return f"{wins} / {draws} / {losses}"


def _equity_points(summary: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = _mapping(summary, "equity_curve").get("points")
    return [point for point in raw if isinstance(point, Mapping)] if isinstance(raw, list) else []


def _timestamp_label(value: Any) -> str:
    timestamp = _float(value)
    if timestamp is None:
        return "—"
    return datetime.fromtimestamp(timestamp / 1_000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _drawdown_duration(risk: Mapping[str, Any]) -> str:
    start = _float(risk.get("max_drawdown_peak_timestamp_ms"))
    end = _float(risk.get("max_drawdown_trough_timestamp_ms"))
    if start is None or end is None:
        return "—"
    return _freqtrade_duration((end - start) / 60_000)


def _terminal_footer(summary: Mapping[str, Any], output: Path) -> str:
    run = _mapping(summary, "run")
    verification = _mapping(summary, "verification")
    lane = "Official Freqtrade" if run.get("execution_lane") == "official" else "Native Rust"
    status = _status_label(str(run.get("status", "unknown"))).replace(" ✓", "").title()
    root = _display_path(output)
    report = _display_path(output / MARKDOWN_FILENAME)
    return "\n".join(
        [
            f"  ✓  {status}  ·  {lane}  ·  {_verification_label(verification)}",
            "",
            f"  Results  {root}",
            f"  Report   {report}",
            f"  Exports  {TRADES_FILENAME} · {ORDERS_FILENAME} · {EQUITY_FILENAME}",
            f"  Evidence {VERIFICATION_FILENAME} · {EVIDENCE_INDEX_FILENAME}",
        ]
    )


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd().resolve()))
    except ValueError:
        return _compact_path(path)


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
