"""Self-contained HTML result rendering."""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .values import (
    _compact_number,
    _compact_path,
    _date_label,
    _decimal_text,
    _difference_text,
    _duration,
    _escape,
    _float,
    _format_timerange,
    _integer_text,
    _mapping,
    _memory_label,
    _minutes,
    _mode_label,
    _money,
    _percent,
    _signed_money,
    _signed_percent,
    _status_label,
    _summary_currency,
    _value_class,
)


def _render_html(
    summary: Mapping[str, Any],
    surface: Mapping[str, Any] | None,
    evidence_index: Mapping[str, Any],
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
        + _metric_card(
            "Sharpe",
            _decimal_text(risk.get("closed_trade_sharpe")) if risk else "—",
            "Closed-trade event returns · not annualized",
        )
        + _metric_card(
            "Sortino",
            _decimal_text(risk.get("closed_trade_sortino")) if risk else "—",
            "Zero target · closed-trade downside deviation",
        )
    )
    verification_markup = _verification_panel(verification)
    verification_details = _verification_details(verification)
    blockers_markup = _blockers_panel(summary.get("blockers"))
    futures_markup = _futures_panel(futures, currency)
    orders_markup = _orders_panel(surface, currency)
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
    evidence = _evidence_panel(summary, evidence_index)
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
    .verification-details {{ margin-top: 14px; }}
    .verification-details code {{ display: inline-block; max-width: 360px; overflow: hidden; text-overflow: ellipsis; vertical-align: bottom; }}
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
      {verification_details}
    </div>
  </section>
  {blockers_markup}
  {futures_markup}
  {orders_markup}
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
  {_risk_definition_panel(risk)}
  <section class="panel" style="margin-top:10px">
    <div class="panel-head"><div><h2>Evidence and exports</h2><h3>Portable files beside this report</h3></div></div>
    {evidence}
  </section>
  <footer>Drawdown, Sharpe, and Sortino use closed-trade events only; no candle-level equity is invented. Use the exact trade surface and official confirmation for parity claims.</footer>
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


def _verification_details(verification: Mapping[str, Any]) -> str:
    stages = verification.get("stages")
    stage_rows = (
        [stage for stage in stages if isinstance(stage, Mapping)]
        if isinstance(stages, list)
        else []
    )
    identities = _mapping(verification, "identities")
    boundaries = _mapping(verification, "boundaries")
    rows = [
        (
            str(stage.get("id", "unknown")).replace("_", " ").title(),
            str(stage.get("status", "unknown")).upper(),
        )
        for stage in stage_rows
    ]
    for label, key in (
        ("Strategy SHA", "strategy_sha256"),
        ("Certified strategy SHA", "certified_strategy_sha256"),
        ("Package SHA", "package_sha256"),
        ("Certified package SHA", "certified_package_sha256"),
        ("Native binary SHA", "native_binary_sha256"),
    ):
        value = identities.get(key)
        rows.append(
            (
                label,
                f"<code>{_escape(value)}</code>"
                if isinstance(value, str)
                else "not captured",
            )
        )
    rows.extend(
        [
            (
                "Native timing",
                str(boundaries.get("native_performance_cache_state", "unknown")),
            ),
            ("Official timing", "not included in native performance"),
        ]
    )
    if not rows:
        return ""
    return (
        '<dl class="verification-details">'
        + "".join(
            f"<dt>{_escape(label)}</dt><dd>{value if value.startswith('<code>') else _escape(value)}</dd>"
            for label, value in rows
        )
        + "</dl>"
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
        + "".join(f"<dt>{_escape(label)}</dt><dd>{_escape(value)}</dd>" for label, value in values)
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


def _orders_panel(
    surface: Mapping[str, Any] | None,
    currency: str | None,
) -> str:
    raw_trades = surface.get("trades") if surface is not None else None
    trades = (
        [trade for trade in raw_trades if isinstance(trade, Mapping)]
        if isinstance(raw_trades, list)
        else []
    )
    rows: list[tuple[Mapping[str, Any], Mapping[str, Any], str]] = []
    entry_count = 0
    partial_exit_count = 0
    exit_count = 0
    for trade in trades:
        raw_orders = trade.get("orders")
        orders = (
            [order for order in raw_orders if isinstance(order, Mapping)]
            if isinstance(raw_orders, list)
            else []
        )
        exit_indexes = [
            index for index, order in enumerate(orders) if order.get("is_entry") is False
        ]
        final_exit_index = (
            exit_indexes[-1]
            if exit_indexes and not bool(trade.get("is_open"))
            else None
        )
        for index, order in enumerate(orders):
            action = (
                "entry"
                if order.get("is_entry") is True
                else "partial exit"
                if index != final_exit_index
                else "exit"
            )
            entry_count += action == "entry"
            partial_exit_count += action == "partial exit"
            exit_count += action == "exit"
            rows.append((trade, order, action))
    recent = sorted(
        rows,
        key=lambda item: (
            int(item[1].get("filled_timestamp_ms", 0)),
            int(item[0].get("sequence", 0)),
            int(item[1].get("sequence", 0)),
        ),
        reverse=True,
    )[:20]
    body = "".join(
        "<tr>"
        f"<td>{_escape(trade.get('pair'))}</td>"
        f"<td>{_escape(_date_label(int(order.get('filled_timestamp_ms', 0))))}</td>"
        f"<td>{_escape(action)}</td>"
        f"<td>{_escape(order.get('side'))}</td>"
        f"<td>{_escape(order.get('amount'))}</td>"
        f"<td>{_money(order.get('cost'), currency)}</td>"
        f"<td>{_escape(order.get('tag'))}</td>"
        "</tr>"
        for trade, order, action in recent
    )
    if not body:
        body = '<tr><td colspan="7">No filled orders</td></tr>'
    return f"""
<section class="panel" style="margin-top:10px">
  <div class="panel-head"><div><h2>Orders and position changes</h2><h3>{len(rows)} filled orders · {entry_count} entries · {partial_exit_count} partial exits · {exit_count} final exits · full export in orders.csv</h3></div></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Pair</th><th>Filled</th><th>Action</th><th>Side</th><th>Amount</th><th>Cost</th><th>Tag</th></tr></thead>
    <tbody>{body}</tbody>
  </table></div>
</section>
"""


def _risk_definition_panel(risk: Mapping[str, Any]) -> str:
    observations = _integer_text(risk.get("closed_trade_return_observations"))
    return f"""
<section class="panel" style="margin-top:10px">
  <div class="panel-head"><div><h2>Risk metric definitions</h2><h3>Presentation-only calculations; exact surface remains authoritative</h3></div></div>
  <dl>
    <dt>Equity source</dt><dd>Starting balance plus realized profit at each trade close</dd>
    <dt>Final reconciliation</dt><dd>equity.csv records any decimal delta to the sealed source final balance</dd>
    <dt>Candle-level equity</dt><dd>Not available and not interpolated</dd>
    <dt>Return observations</dt><dd>{observations} closed-trade events</dd>
    <dt>Sharpe</dt><dd>Mean event return / sample standard deviation; risk-free rate 0; not annualized</dd>
    <dt>Sortino</dt><dd>Mean event return / RMS downside deviation below target 0; not annualized</dd>
  </dl>
</section>
"""


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


def _evidence_panel(
    summary: Mapping[str, Any],
    evidence_index: Mapping[str, Any],
) -> str:
    artifacts = _mapping(summary, "artifacts")
    labels = {
        "run": "Run report",
        "trade_surface": "Exact trade surface",
        "summary": "Machine summary",
        "trades_csv": "Trades CSV",
        "orders_csv": "Orders CSV",
        "equity_csv": "Equity CSV",
        "verification": "Verification JSON",
    }
    raw_entries = evidence_index.get("entries")
    entries = (
        [entry for entry in raw_entries if isinstance(entry, Mapping)]
        if isinstance(raw_entries, list)
        else []
    )
    body = "".join(
        f'<a href="{_escape(entry.get("path"))}" '
        f'title="SHA-256 {_escape(entry.get("sha256"))}">'
        f'{_escape(labels.get(str(entry.get("role")), str(entry.get("role"))))} · '
        f'{_escape(str(entry.get("sha256") or "")[:12])} ↗</a>'
        for entry in entries
    )
    index_path = artifacts.get("evidence_index")
    if isinstance(index_path, str):
        body += f'<a href="{_escape(index_path)}">Evidence index ↗</a>'
    return f'<div class="evidence">{body}</div>'
