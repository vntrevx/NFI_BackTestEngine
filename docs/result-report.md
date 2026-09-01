# Result report contract

The result presentation layer makes a completed, prepared, or safely blocked
research run readable without weakening the exact-parity boundary.

## Files

Every research directory receives these derived presentation files:

| File | Audience | Contract |
| --- | --- | --- |
| `summary.json` | automation and downstream tools | versioned derived metrics |
| `trades.csv` | spreadsheets and ad-hoc analysis | one row per normalized trade |
| `orders.csv` | order and position-change analysis | one row per normalized order |
| `equity.csv` | closed-trade equity analysis | start row plus one row per trade close |
| `report.md` | people | compact Freqtrade-style ASCII tables in Markdown |

These files do not replace `run.json` or `trade-surface.json`. The run report is
the evidence index, and the normalized trade surface is the exact comparison
authority. Presentation generation never rewrites either file.

`report.md` is plain UTF-8 Markdown with no scripts, remote assets, analytics, or
network requests. It can be read directly in a terminal or editor, rendered by
GitHub-compatible Markdown viewers, copied, and archived on Windows, Linux, or
macOS.

Starting with v1.9.0, report generation writes no HTML and never asks to open a
browser. Regenerating an older run deletes a stale sibling `report.html`. The
`summary.json` schema `2.1.0` adds daily, duration, stake, timeout,
mixed-tag, open-trade, and long/short presentation metrics. Its artifact map uses
`"markdown_report": "report.md"` and does not expose `html_report`.

`nfi-bte run` prints the complete Freqtrade-style terminal structure by default:
backtesting, left-open-trade, entry-tag, exit-reason, mixed-tag, summary-metric,
and strategy-summary tables. Interactive work uses one rotating in-place status line.
A completed run with no trades still names the configured pairs and prints zero totals.

## Summary metrics

All monetary and trade values come from the hash-bound normalized trade surface.
Presentation calculations use decimal input tokens before producing ordinary JSON
numbers.

- **Total return** is `profit_total_abs / starting_balance`.
- **CAGR** uses the requested timerange and the start/final balances. It is omitted
  when the range or balances cannot support the calculation.
- **Win rate** counts trades with positive absolute profit. Negative profit is a
  loss and zero profit is a draw.
- **Profit factor** is gross positive absolute profit divided by the absolute
  value of gross negative profit. It is `null` when there are no losing trades.
- **Expectancy** is mean absolute profit per trade.
- **Maximum drawdown** is reconstructed from starting balance plus
  close-time-ordered trade profit. It is always labeled
  `closed-trade equity drawdown`; it is not claimed to be Freqtrade's candle-level
  intratrade drawdown.
- **Monthly and yearly return** divides the period's absolute profit by equity at
  the start of that period.
- **Peak RSS** appears only when a process-tree certification measurement is
  present. Otherwise the report shows the enforced working-memory budget.

Breakdowns group the same trades by pair, exact entry-tag string, exit reason,
direction, calendar month, and calendar year. The Markdown report uses compact,
portable ASCII tables for the complete breakdown, while `summary.json` retains
the same structured data and `trades.csv` retains every trade.

For a futures surface, `summary.json` 2.0.0 also contains a `futures` object. It
counts long and short trades, trades with a non-zero official funding value,
the signed funding total, exact `liquidation` exit reasons, protection locks,
and the observed leverage distribution. Spot and blocked runs use `null` rather
than synthetic zero-valued futures metrics. Liquidation is counted only from the
sealed exit reason; the presentation layer does not reconstruct a liquidation
price or promote internal simulator state into evidence.

## Verification lifecycle

A native run starts with:

```text
Official parity       NOT RUN — confirmation required
```

Either official path refreshes the derived files:

```bash
nfi-bte reference research RESULT_DIR --output-dir OFFICIAL_DIR
nfi-bte confirm RESULT_DIR BACKTEST_EXPORT.zip --output-dir CONFIRM_DIR
```

An exact match produces a prominent `EXACT MATCH` verdict. A mismatch records and
shows the first exact semantic difference. A direct `confirm` report must carry the
same research-run identity and trade-surface hash. The independently identified
official-reference run is bound through its recorded engine trade-surface hash.
Proof that does not match the current surface is rejected.

The proof path and SHA-256 are retained in `summary.json`. Later report
regeneration preserves the verdict only while that proof file remains present and
hash-valid.

## Regeneration and automation

Regenerate an older or moved run without repeating simulation:

```bash
nfi-bte report RESULT_DIR
```

Bind an existing proof explicitly:

```bash
nfi-bte report RESULT_DIR --confirmation CONFIRM_DIR/confirmation.json
```

`nfi-bte report` and `nfi-bte runs show` remain compact by default. Request the same
complete view used by `nfi-bte run` with:

```bash
nfi-bte report RESULT_DIR --full-report
nfi-bte runs show RUN_ID --full-report
```

Use `nfi-bte run --no-full-report` when a saved-project run only needs the compact card.
Performance tables include trade count, average and total profit, total-profit
percentage, average duration, win/draw/loss counts, and totals. Terminal-only labels
longer than 48 characters are shortened with an ellipsis; exact labels remain
available in `summary.json` and `trades.csv`.

The durable run index is human-readable by default:

```bash
nfi-bte runs list
nfi-bte runs show RUN_ID
```

Automation retains a stable machine path:

```bash
nfi-bte runs list --json
nfi-bte runs show RUN_ID --json
```

## CSV columns

`trades.csv` includes sequence, pair, direction, UTC open/close times, duration,
rates, amounts, stake values, leverage, absolute and ratio profit, entry tag, exit
reason, fees, funding, liquidation/stop prices, observed min/max rates, order
count, and open status. Decimal strings from the exact surface are written without
rounding.
