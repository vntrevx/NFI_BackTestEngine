# Result report contract

The result presentation layer makes a completed, prepared, or safely blocked
research run readable without weakening the exact-parity boundary.

## Files

Every research directory receives three derived files:

| File | Audience | Contract |
| --- | --- | --- |
| `summary.json` | automation and downstream tools | versioned derived metrics |
| `trades.csv` | spreadsheets and ad-hoc analysis | one row per normalized trade |
| `report.html` | people | responsive, self-contained one-page report |

These files do not replace `run.json` or `trade-surface.json`. The run report is
the evidence index, and the normalized trade surface is the exact comparison
authority. Presentation generation never rewrites either file.

`report.html` contains no remote fonts, images, scripts, analytics, or network
requests. It can be copied, archived, and opened directly on Windows, Linux, or
macOS.

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
direction, calendar month, and calendar year. The HTML shows compact top tables;
`summary.json` retains the complete breakdown, and `trades.csv` retains every
trade.

For a futures surface, `summary.json` 1.1 also contains a `futures` object. It
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

The default terminal card is intentionally compact. Print complete
Freqtrade-style pair, entry-tag, exit-reason, and direction tables when
investigating a run:

```bash
nfi-bte report RESULT_DIR --full-report
nfi-bte runs show RUN_ID --full-report
```

Each table includes trade count, average profit, absolute profit, win rate,
win/draw/loss counts, and a total row. Every group is printed. Terminal-only
labels longer than 48 characters are shortened with an ellipsis; exact labels
remain available in `summary.json` and `trades.csv`.

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
