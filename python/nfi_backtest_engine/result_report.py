"""Human and machine-friendly presentation files for research-run results."""

# The self-contained HTML template intentionally keeps related markup and CSS rules
# on single lines so the generated file stays easy to inspect without a build step.
# ruff: noqa: E501

from __future__ import annotations

import csv
import html
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError
from .fixture import sha256_file
from .result_summary import build_result_summary
from .specs import validate_trade_surface

SUMMARY_FILENAME = "summary.json"
TRADES_FILENAME = "trades.csv"
HTML_FILENAME = "report.html"
_TERMINAL_BREAKDOWN_NAME_LIMIT = 48

_CSV_FIELDS = (
    "sequence",
    "pair",
    "direction",
    "open_time_utc",
    "close_time_utc",
    "duration_minutes",
    "open_rate",
    "close_rate",
    "amount",
    "stake_amount",
    "max_stake_amount",
    "leverage",
    "profit_abs",
    "profit_ratio",
    "profit_percent",
    "entry_tag",
    "exit_reason",
    "fee_open_rate",
    "fee_close_rate",
    "funding",
    "liquidation_price",
    "initial_stop_loss",
    "stop_loss",
    "minimum_rate",
    "maximum_rate",
    "order_count",
    "is_open",
)


def write_result_presentation(
    run_directory: str | Path,
    *,
    verification: Mapping[str, Any] | None = None,
    verification_path: str | Path | None = None,
) -> dict[str, Any]:
    """Generate all derived result files without modifying parity evidence.

    The function deliberately writes beside ``run.json`` and never rewrites it.
    This allows official confirmation to refresh the user-facing badge without
    invalidating a run report that may already be referenced by certification.
    """

    root = Path(run_directory).resolve()
    run_path = root / "run.json"
    if not run_path.is_file():
        raise BenchmarkError(f"research run.json does not exist: {run_path}")
    run_report = read_json(run_path)
    if not isinstance(run_report, dict):
        raise BenchmarkError(f"research run report must be an object: {run_path}")
    run_report = _with_adjacent_resource_measurement(root, run_report)

    surface = _load_bound_surface(root, run_report)
    verification_document = _resolve_verification(
        root,
        run_report,
        verification=verification,
        verification_path=verification_path,
    )
    summary = build_result_summary(
        run_report,
        surface,
        verification=verification_document,
    )
    write_json(root / SUMMARY_FILENAME, summary)
    _write_trades_csv(root / TRADES_FILENAME, surface)
    (root / HTML_FILENAME).write_text(
        _render_html(summary, surface),
        encoding="utf-8",
        newline="\n",
    )
    return summary


def _with_adjacent_resource_measurement(
    root: Path,
    run_report: dict[str, Any],
) -> dict[str, Any]:
    """Attach certification RSS data to the derived view when it is available.

    A normal research run does not claim peak RSS because its parent process may
    have unobserved children.  The certification runner writes a measured sibling
    file after the process exits; that value is safe to display without rewriting
    the original ``run.json``.
    """

    measurement_path = root / "certification-measurement.json"
    if not measurement_path.is_file():
        return run_report
    measurement = read_json(measurement_path)
    if not isinstance(measurement, Mapping) or measurement.get("exit_code") != 0:
        return run_report
    peak = measurement.get("peak_rss_bytes")
    if not isinstance(peak, int) or isinstance(peak, bool) or peak < 0:
        return run_report
    enriched = dict(run_report)
    enriched["resource_usage"] = {
        "peak_rss_bytes": peak,
        "source": str(measurement_path.resolve()),
    }
    return enriched


def load_result_summary(run_directory: str | Path) -> dict[str, Any]:
    path = Path(run_directory).resolve() / SUMMARY_FILENAME
    if not path.is_file():
        raise BenchmarkError(f"result summary does not exist: {path}")
    summary = read_json(path)
    if not isinstance(summary, dict):
        raise BenchmarkError(f"result summary must be an object: {path}")
    return summary


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
    if isinstance(blockers, list):
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
        ]
    )
    return "\n".join(lines)


def format_terminal_breakdowns(summary: Mapping[str, Any]) -> str:
    """Render complete pair, entry-tag, and exit-reason performance tables."""

    performance = _mapping(summary, "performance")
    activity = _mapping(summary, "activity")
    breakdowns = _mapping(summary, "breakdowns")
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
    return "\n\n".join(table for table in tables if table)


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
            str(record.get("strategy_class", "unknown")),
            _integer_text(record.get("pair_count")),
            _integer_text(record.get("trade_count")),
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


def _load_bound_surface(
    root: Path,
    run_report: Mapping[str, Any],
) -> dict[str, Any] | None:
    result = run_report.get("result")
    if not isinstance(result, Mapping):
        return None
    record = result.get("trade_surface")
    if not isinstance(record, Mapping):
        return None
    expected_hash = record.get("sha256")
    if not isinstance(expected_hash, str):
        raise BenchmarkError("research trade-surface record has no SHA-256")

    # Prefer the portable sibling path.  Older reports may contain an absolute
    # path from the machine that produced the run, while the sealed artifact has
    # since been copied to another supported host.
    candidates = [root / "trade-surface.json"]
    recorded_path = record.get("path")
    if isinstance(recorded_path, str):
        candidates.append(Path(recorded_path))
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_file():
            continue
        if sha256_file(resolved) != expected_hash:
            continue
        surface = read_json(resolved)
        validate_trade_surface(surface)
        if not isinstance(surface, dict):
            raise BenchmarkError(f"trade surface must be an object: {resolved}")
        if surface.get("schema_version") != "2.0.0":
            raise BenchmarkError(
                "result presentation requires trade-surface schema 2.0.0; "
                "normalize or rerun this legacy result first"
            )
        return surface
    raise BenchmarkError("research trade-surface artifact failed its hash binding")


def _resolve_verification(
    root: Path,
    run_report: Mapping[str, Any],
    *,
    verification: Mapping[str, Any] | None,
    verification_path: str | Path | None,
) -> dict[str, Any] | None:
    """Load explicit proof or retain a still hash-valid previous proof link."""

    if verification is not None:
        document = dict(verification)
        if verification_path is not None:
            source = Path(verification_path).resolve()
            document["report_path"] = str(source)
            if source.is_file():
                document["report_sha256"] = sha256_file(source)
        _validate_verification_binding(run_report, document)
        return document

    previous_path = root / SUMMARY_FILENAME
    if not previous_path.is_file():
        return None
    previous = read_json(previous_path)
    if not isinstance(previous, Mapping):
        return None
    previous_run = previous.get("run")
    previous_verification = previous.get("verification")
    if (
        not isinstance(previous_run, Mapping)
        or previous_run.get("id") != run_report.get("run_id")
        or not isinstance(previous_verification, Mapping)
        or previous_verification.get("status") not in {"exact_match", "mismatch"}
    ):
        return None
    source_value = previous_verification.get("source")
    source_sha256 = previous_verification.get("source_sha256")
    if not isinstance(source_value, str) or not isinstance(source_sha256, str):
        return None
    source = Path(source_value)
    if not source.is_file() or sha256_file(source) != source_sha256:
        return None
    candidate = read_json(source)
    if not isinstance(candidate, dict):
        return None
    candidate["report_path"] = str(source.resolve())
    candidate["report_sha256"] = source_sha256
    _validate_verification_binding(run_report, candidate)
    return candidate


def _validate_verification_binding(
    run_report: Mapping[str, Any],
    verification: Mapping[str, Any],
) -> None:
    verification_run_id = verification.get("run_id")
    if (
        "equal" in verification
        and isinstance(verification_run_id, str)
        and verification_run_id != run_report.get("run_id")
    ):
        raise BenchmarkError("confirmation report belongs to a different research run")
    result = run_report.get("result")
    if not isinstance(result, Mapping):
        return
    surface = result.get("trade_surface")
    if not isinstance(surface, Mapping):
        return
    current_hash = surface.get("sha256")
    if not isinstance(current_hash, str):
        return
    engine = verification.get("engine")
    inputs = verification.get("inputs")
    reference_candidate = (
        inputs.get("engine_trade_surface") if isinstance(inputs, Mapping) else None
    )
    confirmed_records: tuple[Mapping[str, Any], Mapping[str, Any]] = (
        engine if isinstance(engine, Mapping) else {},
        reference_candidate if isinstance(reference_candidate, Mapping) else {},
    )
    for record in confirmed_records:
        confirmed_hash = record.get("sha256")
        if isinstance(confirmed_hash, str) and confirmed_hash != current_hash:
            raise BenchmarkError("confirmation report belongs to a different trade surface")


def _write_trades_csv(
    destination: Path,
    surface: Mapping[str, Any] | None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    trades = surface.get("trades", []) if surface is not None else []
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        if not isinstance(trades, list):
            return
        for trade in trades:
            if not isinstance(trade, Mapping):
                continue
            profit = _mapping(trade, "profit")
            fees = _mapping(trade, "fees")
            ratio = _float(profit.get("ratio"))
            writer.writerow(
                {
                    "sequence": trade.get("sequence"),
                    "pair": trade.get("pair"),
                    "direction": trade.get("direction"),
                    "open_time_utc": _iso_timestamp(trade.get("open_timestamp_ms")),
                    "close_time_utc": _iso_timestamp(trade.get("close_timestamp_ms")),
                    "duration_minutes": trade.get("duration_minutes"),
                    "open_rate": trade.get("open_rate"),
                    "close_rate": trade.get("close_rate"),
                    "amount": trade.get("amount"),
                    "stake_amount": trade.get("stake_amount"),
                    "max_stake_amount": trade.get("max_stake_amount"),
                    "leverage": trade.get("leverage"),
                    "profit_abs": profit.get("absolute"),
                    "profit_ratio": profit.get("ratio"),
                    "profit_percent": ratio * 100 if ratio is not None else None,
                    "entry_tag": trade.get("entry_tag"),
                    "exit_reason": trade.get("exit_reason"),
                    "fee_open_rate": fees.get("open_rate"),
                    "fee_close_rate": fees.get("close_rate"),
                    "funding": fees.get("funding"),
                    "liquidation_price": trade.get("liquidation_price"),
                    "initial_stop_loss": trade.get("initial_stop_loss"),
                    "stop_loss": trade.get("stop_loss"),
                    "minimum_rate": trade.get("minimum_rate"),
                    "maximum_rate": trade.get("maximum_rate"),
                    "order_count": len(trade.get("orders", []))
                    if isinstance(trade.get("orders"), list)
                    else 0,
                    "is_open": trade.get("is_open"),
                }
            )


def _render_html(
    summary: Mapping[str, Any],
    surface: Mapping[str, Any] | None,
) -> str:
    run = _mapping(summary, "run")
    performance = _mapping(summary, "performance")
    risk = _mapping(summary, "risk")
    activity = _mapping(summary, "activity")
    futures = _mapping(summary, "futures")
    verification = _mapping(summary, "verification")
    context = _mapping(run, "context")
    breakdowns = _mapping(summary, "breakdowns")
    currency = _summary_currency(summary)
    status = str(run.get("status", "unknown"))
    status_class = (
        "good"
        if status == "complete"
        else "info"
        if status == "prepared"
        else "warn"
        if status == "blocked_unsupported_semantics"
        else "bad"
    )
    title = f"{run.get('strategy') or 'NFI'} backtest report"
    cards = (
        _metric_card(
            "Total return",
            _signed_percent(performance.get("return_ratio")) if performance else "—",
            _signed_money(performance.get("profit_total_abs"), currency)
            if performance
            else "No simulation result",
            _value_class(performance.get("return_ratio")) if performance else "",
        )
        + _metric_card(
            "Final balance",
            _money(performance.get("final_balance"), currency) if performance else "—",
            f"Started at {_money(performance.get('starting_balance'), currency)}"
            if performance
            else "Prepared inputs only",
        )
        + _metric_card(
            "Trades",
            _integer_text(activity.get("trades")),
            f"{_integer_text(activity.get('wins'))} wins · "
            f"{_integer_text(activity.get('losses'))} losses",
        )
        + _metric_card(
            "Win rate",
            _percent(activity.get("win_rate")) if performance else "—",
            f"{_integer_text(activity.get('draws'))} breakeven",
        )
        + _metric_card(
            "Max drawdown",
            _percent(risk.get("max_closed_trade_drawdown_ratio")) if performance else "—",
            "Closed-trade equity",
            "negative" if performance else "",
        )
        + _metric_card(
            "Profit factor",
            _decimal_text(performance.get("profit_factor")) if performance else "—",
            f"Expectancy {_signed_money(performance.get('expectancy_abs'), currency)}"
            if performance
            else "Not available",
        )
    )
    verification_markup = _verification_panel(verification)
    blockers_markup = _blockers_panel(summary.get("blockers"))
    futures_markup = _futures_panel(futures, currency)
    equity_markup = _equity_chart(_mapping(summary, "equity_curve"))
    monthly_markup = _monthly_chart(breakdowns.get("by_month"))
    details = _detail_grid(summary, currency)
    pair_table = _breakdown_table(
        breakdowns.get("by_pair"),
        key="pair",
        heading="Pairs",
        currency=currency,
    )
    tag_table = _breakdown_table(
        breakdowns.get("by_entry_tag"),
        key="entry_tag",
        heading="Entry tags",
        currency=currency,
    )
    exit_table = _breakdown_table(
        breakdowns.get("by_exit_reason"),
        key="exit_reason",
        heading="Exit reasons",
        currency=currency,
    )
    yearly_table = _period_table(
        breakdowns.get("by_year"),
        key="year",
        heading="Yearly performance",
        currency=currency,
    )
    recent_trades = _recent_trades_table(surface, currency)
    evidence = _evidence_panel(summary)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
  <title>{_escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #ffffff;
      --panel: #ffffff;
      --panel-2: #f5f6f3;
      --line: #d9ddd9;
      --line-strong: #838b85;
      --text: #151815;
      --muted: #69716c;
      --accent: #087a4b;
      --negative: #b4232f;
      --warning: #946200;
      --info: #245f96;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
      font-variant-numeric: tabular-nums;
    }}
    main {{ width: min(1320px, calc(100% - 48px)); margin: 0 auto; padding: 28px 0 64px; }}
    header {{
      display: flex; justify-content: space-between; gap: 28px; align-items: flex-start;
      padding: 20px 0 24px; border-top: 3px solid var(--text);
      border-bottom: 1px solid var(--line-strong);
    }}
    .eyebrow {{
      color: var(--muted); font: 600 11px/1.3 ui-monospace, "SFMono-Regular", Consolas, monospace;
      letter-spacing: .12em; text-transform: uppercase; margin-bottom: 10px;
    }}
    h1 {{ margin: 0; font-size: clamp(28px, 3.4vw, 42px); line-height: 1.08; letter-spacing: -.035em; font-weight: 680; }}
    h2 {{ margin: 0 0 3px; font-size: 17px; letter-spacing: -.01em; font-weight: 680; }}
    h3 {{
      margin: 0; color: var(--muted);
      font: 500 11px/1.4 ui-monospace, "SFMono-Regular", Consolas, monospace;
    }}
    .subhead {{ margin: 10px 0 0; color: var(--muted); max-width: 760px; }}
    .badge {{
      flex: none; display: inline-flex; align-items: center; gap: 8px;
      padding: 7px 10px; border: 1px solid var(--line-strong); background: var(--panel);
      font: 700 11px/1.2 ui-monospace, "SFMono-Regular", Consolas, monospace;
      letter-spacing: .08em; text-transform: uppercase;
    }}
    .badge::before {{ content: ""; width: 6px; height: 6px; background: currentColor; }}
    .badge.good {{ color: var(--accent); }}
    .badge.warn {{ color: var(--warning); }}
    .badge.info {{ color: var(--info); }}
    .badge.bad {{ color: var(--negative); }}
    .meta {{
      display: grid; grid-template-columns: 1.35fr .8fr .65fr .65fr 1fr;
      margin: 0 0 22px; border: 1px solid var(--line); border-top: 0;
      background: var(--line); gap: 1px;
    }}
    .meta-item {{ min-width: 0; padding: 10px 12px; background: var(--panel); }}
    .meta-item span {{
      display: block; margin-bottom: 2px; color: var(--muted);
      font: 500 10px/1.3 ui-monospace, "SFMono-Regular", Consolas, monospace;
      letter-spacing: .08em; text-transform: uppercase;
    }}
    .meta-item strong {{ display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 600; }}
    .metrics {{
      display: grid; grid-template-columns: repeat(6, minmax(0,1fr));
      gap: 1px; padding: 1px; background: var(--line);
    }}
    .metric {{ padding: 17px 16px; min-height: 112px; background: var(--panel); }}
    .metric .label {{
      color: var(--muted);
      font: 600 10px/1.3 ui-monospace, "SFMono-Regular", Consolas, monospace;
      text-transform: uppercase; letter-spacing: .08em;
    }}
    .metric .value {{ margin-top: 11px; font-size: clamp(19px, 1.8vw, 27px); font-weight: 690; letter-spacing: -.03em; white-space: nowrap; }}
    .metric .note {{ margin-top: 5px; color: var(--muted); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .positive {{ color: var(--accent); }} .negative {{ color: var(--negative); }}
    .grid-2 {{ display: grid; grid-template-columns: 1.45fr 1fr; gap: 10px; margin-top: 10px; }}
    .grid-3 {{ display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 10px; margin-top: 10px; }}
    .panel {{ min-width: 0; padding: 18px; border: 1px solid var(--line); background: var(--panel); }}
    .panel-head {{
      display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
      margin-bottom: 16px; padding-bottom: 11px; border-bottom: 1px solid var(--line);
    }}
    .panel-head p {{ margin: 0; color: var(--muted); font-size: 12px; }}
    svg {{ display: block; width: 100%; height: auto; overflow: visible; }}
    .verification {{ display: grid; grid-template-columns: auto 1fr; gap: 13px; align-items: center; }}
    .seal {{
      width: 42px; height: 42px; display: grid; place-items: center;
      font: 800 20px/1 ui-monospace, "SFMono-Regular", Consolas, monospace;
      border: 1px solid currentColor;
    }}
    .seal.good {{ color: var(--accent); }} .seal.warn {{ color: var(--warning); }} .seal.bad {{ color: var(--negative); }}
    .verification strong {{ display: block; margin-bottom: 2px; font-size: 16px; letter-spacing: .01em; }}
    .verification span {{ color: var(--muted); font-size: 12px; }}
    dl {{ display: grid; grid-template-columns: 1fr auto; gap: 0 16px; margin: 0; }}
    dt, dd {{ padding: 7px 0; border-bottom: 1px solid var(--line); }}
    dt {{ color: var(--muted); }} dd {{ margin: 0; font-weight: 620; text-align: right; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{
      color: var(--muted);
      font: 600 10px/1.3 ui-monospace, "SFMono-Regular", Consolas, monospace;
      letter-spacing: .06em; text-transform: uppercase; text-align: right; padding: 0 9px 9px;
    }}
    th:first-child, td:first-child {{ text-align: left; padding-left: 0; }}
    td {{ padding: 8px 9px; border-top: 1px solid var(--line); text-align: right; white-space: nowrap; font-size: 12px; }}
    tbody tr:hover {{ background: var(--panel-2); }}
    td.name {{ max-width: 290px; overflow: hidden; text-overflow: ellipsis; }}
    .blocked {{ border-left: 3px solid var(--warning); }}
    .blocked code {{ color: var(--warning); }}
    code {{ font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; }}
    a {{ color: var(--accent); text-decoration: none; }} a:hover {{ text-decoration: underline; }}
    .evidence {{ display: flex; flex-wrap: wrap; gap: 1px; background: var(--line); border: 1px solid var(--line); }}
    .evidence a {{
      padding: 9px 11px; background: var(--panel-2);
      font: 600 10px/1.3 ui-monospace, "SFMono-Regular", Consolas, monospace;
      letter-spacing: .04em; text-transform: uppercase;
    }}
    footer {{ margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--line); color: var(--muted); font-size: 11px; }}
    @media (max-width: 1180px) {{ .metrics {{ grid-template-columns: repeat(3,1fr); }} }}
    @media (max-width: 820px) {{
      main {{ width: min(100% - 24px, 1320px); padding-top: 14px; }}
      header {{ display: block; }} header .badge {{ margin-top: 16px; }}
      .meta {{ grid-template-columns: repeat(2, 1fr); }}
      .meta-item:last-child:nth-child(odd) {{ grid-column: 1 / -1; }}
      .metrics {{ grid-template-columns: repeat(2,1fr); }}
      .grid-2, .grid-3 {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 500px) {{
      .metric {{ min-height: 100px; padding: 14px 12px; }}
      .metric .value {{ font-size: 19px; white-space: normal; }}
      .panel {{ padding: 14px; }}
    }}
    @media (max-width: 350px) {{ .metrics, .meta {{ grid-template-columns: 1fr; }} }}
    @media print {{
      body {{ background: white; }} main {{ width: 100%; padding: 0; }}
      .metric, .panel {{ break-inside: avoid; }} a {{ color: #075; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <div class="eyebrow">NFI Backtest Engine · Result report</div>
      <h1>{_escape(run.get("strategy") or "Unknown strategy")}</h1>
      <p class="subhead">Native simulation summary. Source artifacts remain hash-bound and unchanged.</p>
    </div>
    <div class="badge {status_class}">{_escape(_status_label(status))}</div>
  </header>
  <div class="meta">
    <div class="meta-item"><span>Period</span><strong>{_escape(_format_timerange(context.get("timerange")))}</strong></div>
    <div class="meta-item"><span>Mode</span><strong>{_escape(_mode_label(context))}</strong></div>
    <div class="meta-item"><span>Pairs</span><strong>{_integer_text(run.get("pair_count"))}</strong></div>
    <div class="meta-item"><span>Trades</span><strong>{_integer_text(activity.get("trades"))}</strong></div>
    <div class="meta-item"><span>Run ID</span><strong>{_escape(str(run.get("id") or "unknown")[:12])}</strong></div>
  </div>
  <section class="metrics">{cards}</section>
  <section class="grid-2">
    <div class="panel">
      <div class="panel-head"><div><h2>Equity</h2><h3>Closed-trade cumulative balance</h3></div></div>
      {equity_markup}
    </div>
    <div class="panel">
      <div class="panel-head"><div><h2>Official verification</h2><h3>Zero-tolerance parity status</h3></div></div>
      {verification_markup}
    </div>
  </section>
  {blockers_markup}
  {futures_markup}
  <section class="grid-2">
    <div class="panel">
      <div class="panel-head"><div><h2>Monthly returns</h2><h3>Profit divided by opening monthly equity</h3></div></div>
      {monthly_markup}
    </div>
    <div class="panel">
      <div class="panel-head"><div><h2>Run details</h2><h3>Workload and risk context</h3></div></div>
      {details}
    </div>
  </section>
  <section class="grid-3">{pair_table}{tag_table}{exit_table}</section>
  <section class="grid-2">{yearly_table}{recent_trades}</section>
  <section class="panel" style="margin-top:10px">
    <div class="panel-head"><div><h2>Evidence and exports</h2><h3>Portable files beside this report</h3></div></div>
    {evidence}
  </section>
  <footer>Drawdown is reconstructed from closed-trade equity and is labeled accordingly. Use the exact trade surface and official confirmation for parity claims.</footer>
</main>
</body>
</html>
"""


def _metric_card(
    label: str,
    value: str,
    note: str,
    value_class: str = "",
) -> str:
    return (
        '<article class="metric">'
        f'<div class="label">{_escape(label)}</div>'
        f'<div class="value {_escape(value_class)}">{_escape(value)}</div>'
        f'<div class="note" title="{_escape(note)}">{_escape(note)}</div>'
        "</article>"
    )


def _verification_panel(verification: Mapping[str, Any]) -> str:
    status = str(verification.get("status", "not_run"))
    difference = verification.get("difference")
    if status == "exact_match":
        seal, css, title, detail = (
            "✓",
            "good",
            "EXACT MATCH",
            ("Native and official Freqtrade surfaces are semantically identical."),
        )
    elif status == "mismatch":
        seal, css, title, detail = "×", "bad", "MISMATCH", (_difference_text(difference))
    else:
        seal, css, title, detail = (
            "!",
            "warn",
            "NOT RUN",
            ("Run the official confirmation lane before using this result as final proof."),
        )
    source = verification.get("source")
    source_markup = (
        f'<div style="margin-top:10px"><code>proof · {_escape(_compact_path(source))}</code></div>'
        if source
        else ""
    )
    return (
        '<div class="verification">'
        f'<div class="seal {css}">{seal}</div>'
        f"<div><strong>{title}</strong><span>{_escape(detail)}</span>{source_markup}</div>"
        "</div>"
    )


def _blockers_panel(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return ""
    rows = []
    for blocker in value:
        if not isinstance(blocker, Mapping):
            continue
        rows.append(
            "<p>"
            f"<code>{_escape(blocker.get('code') or 'UNKNOWN')}</code> "
            f"{_escape(blocker.get('message') or '')}"
            "</p>"
        )
    if not rows:
        return ""
    return (
        '<section class="panel blocked" style="margin-top:14px">'
        '<div class="panel-head"><div><h2>Exact lowering blocked</h2>'
        "<h3>This is a safety verdict, not a crash</h3></div></div>" + "".join(rows) + "</section>"
    )


def _futures_panel(futures: Mapping[str, Any], currency: str | None) -> str:
    """Render futures lifecycle evidence without inventing spot placeholders."""

    if not futures:
        return ""
    values = (
        (
            "Long / short trades",
            f"{_integer_text(futures.get('long_trades'))} / "
            f"{_integer_text(futures.get('short_trades'))}",
        ),
        ("Leverage range", _leverage_range(futures)),
        (
            "Funding total",
            f"{_signed_money(futures.get('funding_total'), currency)} across "
            f"{_integer_text(futures.get('funded_trades'))} trades",
        ),
        ("Liquidation exits", _integer_text(futures.get("liquidation_exits"))),
        ("Protection locks", _integer_text(futures.get("protection_locks"))),
        ("Margin mode", str(futures.get("margin_mode") or "unknown")),
    )
    details = (
        "<dl>"
        + "".join(
            f"<dt>{_escape(label)}</dt><dd>{_escape(value)}</dd>"
            for label, value in values
        )
        + "</dl>"
    )
    leverage_table = _breakdown_table(
        futures.get("by_leverage"),
        key="leverage",
        heading="Leverage",
        currency=currency,
    )
    return f"""
<section class="grid-2">
  <div class="panel">
    <div class="panel-head"><div><h2>Futures lifecycle</h2><h3>Exact fields from the sealed trade surface</h3></div></div>
    {details}
  </div>
  {leverage_table}
</section>
"""


def _leverage_range(futures: Mapping[str, Any]) -> str:
    count = _integer_text(futures.get("distinct_leverages"))
    minimum = _decimal_text(futures.get("minimum_leverage"))
    maximum = _decimal_text(futures.get("maximum_leverage"))
    if minimum == "—" or maximum == "—":
        return "—"
    return f"{minimum}x–{maximum}x ({count} distinct)"


def _detail_grid(summary: Mapping[str, Any], currency: str | None) -> str:
    performance = _mapping(summary, "performance")
    risk = _mapping(summary, "risk")
    activity = _mapping(summary, "activity")
    execution = _mapping(summary, "execution")
    values = (
        ("CAGR", _signed_percent(performance.get("cagr_ratio"))),
        (
            "Gross profit / loss",
            f"{_money(performance.get('gross_profit_abs'), currency)} / "
            f"{_money(performance.get('gross_loss_abs'), currency)}",
        ),
        (
            "Average / median trade",
            f"{_signed_percent(performance.get('average_profit_ratio'))} / "
            f"{_signed_percent(performance.get('median_profit_ratio'))}",
        ),
        (
            "Average / median duration",
            f"{_minutes(activity.get('average_duration_minutes'))} / "
            f"{_minutes(activity.get('median_duration_minutes'))}",
        ),
        (
            "Consecutive wins / losses",
            f"{_integer_text(risk.get('maximum_consecutive_wins'))} / "
            f"{_integer_text(risk.get('maximum_consecutive_losses'))}",
        ),
        ("Rejected signals", _integer_text(activity.get("rejected_signals"))),
        ("Wall time", _duration(execution.get("wall_time_seconds"))),
        (
            "Indicator workers / CPU limit",
            f"{_integer_text(execution.get('indicator_workers'))} / "
            f"{_integer_text(execution.get('cpu_process_limit'))}",
        ),
        ("Memory", _memory_label(execution)),
    )
    return (
        "<dl>"
        + "".join(f"<dt>{_escape(label)}</dt><dd>{_escape(value)}</dd>" for label, value in values)
        + "</dl>"
    )


def _equity_chart(equity: Mapping[str, Any]) -> str:
    raw = equity.get("points")
    points = [point for point in raw if isinstance(point, Mapping)] if isinstance(raw, list) else []
    if len(points) < 2:
        return '<p style="color:var(--muted)">No completed trades to chart.</p>'
    values = [_float(point.get("equity")) for point in points]
    timestamps = [_float(point.get("timestamp_ms")) for point in points]
    if any(value is None for value in values) or any(value is None for value in timestamps):
        return '<p style="color:var(--muted)">Equity points are incomplete.</p>'
    numeric_values = [value for value in values if value is not None]
    numeric_times = [value for value in timestamps if value is not None]
    low, high = min(numeric_values), max(numeric_values)
    span = high - low or max(abs(high), 1.0)
    width, height, pad_x, pad_y = 1000.0, 300.0, 22.0, 26.0
    time_span = numeric_times[-1] - numeric_times[0] or 1.0
    coordinates = [
        (
            pad_x + (timestamp - numeric_times[0]) / time_span * (width - pad_x * 2),
            pad_y + (high - value) / span * (height - pad_y * 2),
        )
        for timestamp, value in zip(numeric_times, numeric_values, strict=True)
    ]
    line = " ".join(f"{x:.2f},{y:.2f}" for x, y in coordinates)
    area = (
        f"M {coordinates[0][0]:.2f} {height - pad_y:.2f} "
        + " ".join(f"L {x:.2f} {y:.2f}" for x, y in coordinates)
        + f" L {coordinates[-1][0]:.2f} {height - pad_y:.2f} Z"
    )
    first_date = _date_label(int(numeric_times[0]))
    last_date = _date_label(int(numeric_times[-1]))
    return f"""
<svg viewBox="0 0 1000 330" role="img" aria-label="Closed-trade equity curve">
  <line x1="22" y1="26" x2="978" y2="26" stroke="#d9ddd9"/>
  <line x1="22" y1="274" x2="978" y2="274" stroke="#d9ddd9"/>
  <path d="{area}" fill="#087a4b" fill-opacity=".06"/>
  <polyline points="{line}" fill="none" stroke="#087a4b" stroke-width="2"/>
  <text x="22" y="18" fill="#69716c" font-size="12">{_escape(_compact_number(high))}</text>
  <text x="22" y="294" fill="#69716c" font-size="12">{_escape(first_date)}</text>
  <text x="978" y="294" fill="#69716c" font-size="12" text-anchor="end">{_escape(last_date)}</text>
</svg>
"""


def _monthly_chart(value: Any) -> str:
    rows = [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []
    if not rows:
        return '<p style="color:var(--muted)">No monthly return data.</p>'
    displayed = rows[-60:]
    returns = [_float(row.get("return_ratio")) or 0.0 for row in displayed]
    maximum = max((abs(value) for value in returns), default=0.0) or 1.0
    width, pad_x, axis_y = 1000.0, 22.0, 140.0
    gap = 3.0
    slot = (width - pad_x * 2) / len(displayed)
    bar_width = max(1.0, slot - gap)
    bars = []
    for index, value in enumerate(returns):
        bar_height = abs(value) / maximum * 105
        x = pad_x + index * slot + gap / 2
        y = axis_y - bar_height if value >= 0 else axis_y
        color = "#087a4b" if value >= 0 else "#b4232f"
        month = str(displayed[index].get("month", ""))
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" '
            f'height="{bar_height:.2f}" fill="{color}">'
            f"<title>{_escape(month)}: {_signed_percent(value)}</title></rect>"
        )
    return f"""
<svg viewBox="0 0 1000 310" role="img" aria-label="Monthly return chart">
  <line x1="22" y1="{axis_y}" x2="978" y2="{axis_y}" stroke="#838b85"/>
  {"".join(bars)}
  <text x="22" y="18" fill="#69716c" font-size="12">{_escape(_signed_percent(maximum))}</text>
  <text x="22" y="292" fill="#69716c" font-size="12">{_escape(str(displayed[0].get("month", "")))}</text>
  <text x="978" y="292" fill="#69716c" font-size="12" text-anchor="end">{_escape(str(displayed[-1].get("month", "")))}</text>
</svg>
"""


def _breakdown_table(
    value: Any,
    *,
    key: str,
    heading: str,
    currency: str | None,
) -> str:
    rows = [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []
    body = "".join(
        "<tr>"
        f'<td class="name" title="{_escape(row.get(key))}">{_escape(row.get(key))}</td>'
        f"<td>{_integer_text(row.get('trades'))}</td>"
        f"<td>{_percent(row.get('win_rate'))}</td>"
        f'<td class="{_value_class(row.get("profit_abs"))}">'
        f"{_signed_money(row.get('profit_abs'), currency)}</td>"
        "</tr>"
        for row in rows[:10]
    )
    if not body:
        body = '<tr><td colspan="4">No data</td></tr>'
    return f"""
<div class="panel">
  <div class="panel-head"><div><h2>{_escape(heading)}</h2><h3>Top 10 by absolute profit</h3></div></div>
  <div class="table-wrap"><table>
    <thead><tr><th>{_escape(heading[:-1] if heading.endswith("s") else heading)}</th><th>Trades</th><th>Win</th><th>Profit</th></tr></thead>
    <tbody>{body}</tbody>
  </table></div>
</div>
"""


def _period_table(
    value: Any,
    *,
    key: str,
    heading: str,
    currency: str | None,
) -> str:
    rows = [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []
    body = "".join(
        "<tr>"
        f"<td>{_escape(row.get(key))}</td>"
        f"<td>{_integer_text(row.get('trades'))}</td>"
        f"<td>{_percent(row.get('win_rate'))}</td>"
        f'<td class="{_value_class(row.get("profit_abs"))}">'
        f"{_signed_money(row.get('profit_abs'), currency)}</td>"
        f'<td class="{_value_class(row.get("return_ratio"))}">'
        f"{_signed_percent(row.get('return_ratio'))}</td>"
        "</tr>"
        for row in rows
    )
    if not body:
        body = '<tr><td colspan="5">No data</td></tr>'
    return f"""
<div class="panel">
  <div class="panel-head"><div><h2>{_escape(heading)}</h2><h3>Closed-trade calendar periods</h3></div></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Period</th><th>Trades</th><th>Win</th><th>Profit</th><th>Return</th></tr></thead>
    <tbody>{body}</tbody>
  </table></div>
</div>
"""


def _recent_trades_table(
    surface: Mapping[str, Any] | None,
    currency: str | None,
) -> str:
    raw = surface.get("trades") if surface is not None else None
    trades = [trade for trade in raw if isinstance(trade, Mapping)] if isinstance(raw, list) else []
    recent = sorted(
        trades,
        key=lambda trade: (
            int(trade.get("close_timestamp_ms", 0)),
            int(trade.get("sequence", 0)),
        ),
        reverse=True,
    )[:20]
    body = "".join(
        "<tr>"
        f"<td>{_escape(trade.get('pair'))}</td>"
        f"<td>{_escape(_date_label(int(trade.get('close_timestamp_ms', 0))))}</td>"
        f'<td class="{_value_class(_mapping(trade, "profit").get("absolute"))}">'
        f"{_signed_money(_mapping(trade, 'profit').get('absolute'), currency)}</td>"
        f"<td>{_escape(trade.get('exit_reason'))}</td>"
        "</tr>"
        for trade in recent
    )
    if not body:
        body = '<tr><td colspan="4">No trades</td></tr>'
    return f"""
<div class="panel">
  <div class="panel-head"><div><h2>Recent trades</h2><h3>Latest 20 · full export in trades.csv</h3></div></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Pair</th><th>Closed</th><th>Profit</th><th>Exit</th></tr></thead>
    <tbody>{body}</tbody>
  </table></div>
</div>
"""


def _evidence_panel(summary: Mapping[str, Any]) -> str:
    artifacts = _mapping(summary, "artifacts")
    links = (
        ("Run report", artifacts.get("run")),
        ("Exact trade surface", artifacts.get("source_surface")),
        ("Machine summary", artifacts.get("summary")),
        ("Trades CSV", artifacts.get("trades_csv")),
    )
    body = "".join(
        f'<a href="{_escape(path)}">{_escape(label)} ↗</a>'
        for label, path in links
        if isinstance(path, str)
    )
    return f'<div class="evidence">{body}</div>'


def _summary_currency(summary: Mapping[str, Any]) -> str | None:
    breakdowns = _mapping(summary, "breakdowns")
    rows = breakdowns.get("by_pair")
    if not isinstance(rows, list):
        return None
    currencies = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        pair = str(row.get("pair", ""))
        if "/" not in pair:
            continue
        quote = pair.split("/", maxsplit=1)[1].split(":", maxsplit=1)[0]
        if quote:
            currencies.add(quote)
    return next(iter(currencies)) if len(currencies) == 1 else None


def _terminal_row(label: str, value: Any) -> str:
    return f"{label:<21} {value}"


def _status_label(status: str) -> str:
    return {
        "complete": "COMPLETE ✓",
        "prepared": "PREPARED",
        "blocked_unsupported_semantics": "BLOCKED — SAFE STOP",
    }.get(status, status.upper())


def _verification_label(verification: Mapping[str, Any]) -> str:
    status = verification.get("status")
    if status == "exact_match":
        return "EXACT MATCH ✓"
    if status == "mismatch":
        return f"MISMATCH — {_difference_text(verification.get('difference'))}"
    return "NOT RUN — confirmation required"


def _difference_text(value: Any) -> str:
    if isinstance(value, Mapping):
        path = value.get("path")
        reason = value.get("reason")
        if path or reason:
            return f"{path or 'unknown path'}: {reason or 'values differ'}"
    return "official and native surfaces differ"


def _memory_label(execution: Mapping[str, Any]) -> str:
    peak = _float(execution.get("peak_rss_bytes"))
    if peak is not None:
        return f"{_bytes(peak)} peak RSS"
    budget = _float(execution.get("memory_budget_bytes"))
    return f"{_bytes(budget)} budget" if budget is not None else "not measured"


def _mode_label(context: Mapping[str, Any]) -> str:
    mode = str(context.get("trading_mode") or "unknown")
    margin = context.get("margin_mode")
    timeframe = context.get("timeframe")
    details = [mode]
    if margin:
        details.append(str(margin))
    if timeframe:
        details.append(str(timeframe))
    return " · ".join(details)


def _format_timerange(value: Any) -> str:
    text = str(value or "unknown")
    parts = text.split("-", maxsplit=1)
    if len(parts) != 2:
        return text
    return f"{_date_token(parts[0])} → {_date_token(parts[1])}"


def _date_token(value: str) -> str:
    if len(value) >= 8 and value[:8].isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value or "open"


def _duration(value: Any) -> str:
    seconds = _float(value)
    if seconds is None:
        return "not measured"
    rounded = max(0, round(seconds))
    hours, remainder = divmod(rounded, 3_600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _bytes(value: float) -> str:
    size = max(0.0, value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024 or candidate == units[-1]:
            break
        size /= 1024
    return f"{size:.1f} {unit}"


def _money(value: Any, currency: str | None) -> str:
    number = _float(value)
    if number is None:
        return "—"
    suffix = f" {currency}" if currency else ""
    return f"{number:,.2f}{suffix}"


def _signed_money(value: Any, currency: str | None) -> str:
    number = _float(value)
    if number is None:
        return "—"
    suffix = f" {currency}" if currency else ""
    return f"{number:+,.2f}{suffix}"


def _percent(value: Any) -> str:
    number = _float(value)
    return f"{number * 100:.2f}%" if number is not None else "—"


def _signed_percent(value: Any) -> str:
    number = _float(value)
    return f"{number * 100:+.2f}%" if number is not None else "—"


def _decimal_text(value: Any) -> str:
    number = _float(value)
    return f"{number:,.2f}" if number is not None else "—"


def _minutes(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "—"
    if number >= 1_440:
        return f"{number / 1_440:.1f}d"
    if number >= 60:
        return f"{number / 60:.1f}h"
    return f"{number:.0f}m"


def _value_class(value: Any) -> str:
    number = _float(value)
    if number is None or number == 0:
        return ""
    return "positive" if number > 0 else "negative"


def _integer_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError, OverflowError):
        return "—"


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _iso_timestamp(value: Any) -> str | None:
    timestamp = _float(value)
    if timestamp is None:
        return None
    return (
        datetime.fromtimestamp(timestamp / 1_000, tz=UTC)
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )


def _date_label(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC).strftime("%Y-%m-%d")


def _compact_number(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:.2f}"


def _short_timestamp(value: Any) -> str:
    text = str(value or "")
    return text.replace("T", " ")[:19] if text else "unknown"


def _compact_path(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "unknown"
    # Proofs can move between Windows and POSIX hosts.  Normalize only for display;
    # the complete source path remains untouched in summary.json.
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    if len(parts) <= 2:
        return text
    return f"…/{'/'.join(parts[-2:])}"


def _truncate(value: str, width: int) -> str:
    return value if len(value) <= width else f"{value[: max(0, width - 1)]}…"


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    candidate = value.get(key)
    return candidate if isinstance(candidate, Mapping) else {}
