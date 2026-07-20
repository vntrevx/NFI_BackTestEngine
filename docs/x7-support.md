# NFI X7 support boundary

## Executable now

The runner loads a trusted X7 strategy, resolves and redacts Freqtrade config
includes, freezes pair order, derives safe CPU/RAM settings, fills and seals candle
coverage, and evaluates the dataframe methods in isolated pair workers:

- `populate_indicators`
- `populate_entry_trend`
- `populate_exit_trend`

Informative frames are aligned without lookahead. Signals are shifted to the next
executable candle open using Freqtrade's startup/timerange boundary. Pre-start rows
remain available to callbacks, while a serialized `execution_start_index` prevents
context rows and the shifted head row from entering orders, wallet state, or time
ordering. The resulting Feather files are SHA-256 sealed; Rust projects only the
callback columns declared by the source-compiled IR and runs one global chronological
portfolio loop.

For the reviewed X7 source shape through v17.4.421, these callback families execute in
Rust:

- backtest lifecycle no-op delegation and first-entry `order_filled` state writes;
- source-compiled custom stake and entry/exit confirmation programs;
- managed long exits for normal (1-13), pump (21-26), quick (41-53),
  rebuy (61-65), high-profit (81-82), rapid (101-110), top-coins (141-145),
  and scalp (161-163);
- managed short-rebuy exits and adjustment for tags 561-563;
- the dedicated rebuy ladder and level-3 de-risk transition for tags 61-65;
- the shared system-v3.2 derisk/grind adjustment used by all 57 managed tags,
  including rebuy trades after their first level-3 de-risk fill;
- the tag-120 spot/backtest legacy grind route: source-ordered `gm0`, `dl1`/`dl2`,
  `gd1` through `gd6`, their partial exits and stops, and the `d1` buyback cycle.
- the tag-121 regular-mode de-risk/grind prelude and its source-ordered transition into
  the legacy grind state machine;
- source-ordered tag-dependent futures leverage, capped by the frozen per-pair market
  limit;
- Binance isolated-futures transport with frozen leverage tiers, calculated and
  adjustment-updated liquidation prices, long/short signals, mark-price funding events,
  fees, historical precision, derisk/grind order replay, and final-surface normalization;
- static `CooldownPeriod`, `StoplossGuard`, `MaxDrawdown`, and `LowProfitPairs`
  definitions with side-aware local/global pair locks in the global event loop.

The route table preserves X7's callback order. A mixed tag is accepted only when every
word belongs to the compiled scope. Rebuy, rapid, and scalp combinations retain their
source-specific dispatch order; an unknown companion word fails before simulation.

## Proof level

The source analyzer pins the whole strategy SHA plus each handwritten stateful callback
method SHA. A changed callback cannot silently inherit the prior Rust policy.
It also inventories literal condition-index branches and the effective strategy
switches. Probe-only source changes are AST-bound to the expected class attribute and
old literal; routine upstream edits fail closed instead of silently changing the wrong
line.

The latest branch matrix pins X7 v17.4.421 at upstream commit
`5e168431991e05a889514eb1e16fdbebc6a09811` and Freqtrade 2026.5.1. Its seven
official full-state fixtures reach:

- tag 121 in spot mode;
- `CooldownPeriod`, `StoplossGuard`, `MaxDrawdown`, and `LowProfitPairs`, including
  generated locks and locked-entry rejection where applicable;
- the compound top-coins tag `141 142`;
- tag-dependent futures leverage values 2 and 3;
- an actual isolated-futures liquidation exit after partial position reductions.

The fixtures live under `benchmarks/fixtures/captured/x7-*`. Every manifest seals the
effective strategy, compact candle inputs, native and raw reference market metadata,
official export, normalized surface, observer trace, and coverage report.

The latest certificate is X7 v17.4.418 on APE/USDT:USDT isolated futures from
2022-04-01 through 2023-01-01. The engine and offline Freqtrade 2026.5.1 produce
byte-identical normalized surfaces with zero numeric tolerance: 11 trades, 164 orders,
142 adjustment orders, one short trade, and eight funded trades. The run reaches
derisk levels 1-3 and grind levels 1-5. Its sealed official callback audit exposed and
then protected Freqtrade's stop-loss-before-liquidation collision order. The run has no
liquidation exit and does not certify other pairs, pair locks, or protections.

The full-year APE/USDT spot fixture separately proves exact final trade-surface parity
for the top-coins path: 12 trades, 232 orders, and a byte-identical normalized surface.
A ZEC/USDT fixture proves the tag-120 legacy route through `gm0`, repeated `gd1`, and
`gd2`: one trade and 13 orders are byte-identical to an offline Freqtrade 2026.5.1
run. Deeper `dl1`/`dl2`, `gd3`-`gd6`, stop, and `d1` branches are executable and have
focused Rust tests, but do not yet have branch-reaching official fixtures.

A separate mid-day Unix-timerange fixture proves the tag-62 rebuy entry, generic
confirmation path, and rebuy custom exit with one exact trade. That trade did not
reach a rebuy adjustment, so the entry/de-risk ladder still has source-identity and
focused Rust proof only.
An APE/AAVE fixture starts at a deliberately chosen five-minute boundary where both
pairs request entry on the first executable timestamp. With frozen pair order and
`max_open_trades=1`, offline Freqtrade and Rust both admit APE, reject AAVE once, and
produce the same normalized trade surface with zero tolerance. This certifies that one
shared-slot conflict, not arbitrary multi-pair pressure.
The narrow public claims and artifact hashes are recorded in
[`benchmarks/evidence/x7-ape-top-coins-v17.4.413.json`](../benchmarks/evidence/x7-ape-top-coins-v17.4.413.json)
and
[`benchmarks/evidence/x7-ape-rebuy-exit-v17.4.413.json`](../benchmarks/evidence/x7-ape-rebuy-exit-v17.4.413.json).
The reached tag-120 order sequence and its independent sealed hashes are in
[`benchmarks/evidence/x7-zec-legacy-grind-v17.4.413.json`](../benchmarks/evidence/x7-zec-legacy-grind-v17.4.413.json).
The equal-timestamp shared-slot hashes are in
[`benchmarks/evidence/x7-ape-aave-shared-slot-v17.4.413.json`](../benchmarks/evidence/x7-ape-aave-shared-slot-v17.4.413.json).
The annual futures inputs, dependency versions, result hashes, and exact counts are in
[`benchmarks/evidence/x7-ape-futures-2022-v17.4.413.json`](../benchmarks/evidence/x7-ape-futures-2022-v17.4.413.json).
The corresponding current-source proof, official callback-audit hash, container-memory
measurement, and zero-tolerance surface hashes are in
[`benchmarks/evidence/x7-ape-futures-2022-v17.4.418.json`](../benchmarks/evidence/x7-ape-futures-2022-v17.4.418.json).

The broad bounded spot proof uses the same X7 v17.4.418 source over 80 configured
pairs and `20250701-20260101`. Its 167 trades, 402 orders, rejected-signal count,
balances, tags, and numeric tokens are byte-identical to pinned Freqtrade 2026.5.1.
The exact hashes and resource observations are in
[`benchmarks/evidence/x7-80pair-spot-2025h2-parity-2026-07-20.json`](../benchmarks/evidence/x7-80pair-spot-2025h2-parity-2026-07-20.json).
This is the broadest current portfolio differential, but it does not turn an
unreached branch into a certified one or replace the continuous multi-year gate.

Generated `hot-callback-ir.json` remains the source of truth for the exact strategy
file used by a run. Context-only callbacks may be inactive for a mode; for example,
Freqtrade does not call `leverage()` in spot mode.

`nfi-bte strategy check` performs this source and callback compilation without preparing
candles. The scheduled latest-NFI workflow downloads upstream X7 every day and retains
the compatibility report. A source change outside the handwritten state contracts can
continue immediately; a changed state contract is visible before a four-to-five-year
run consumes resources.

## Still blocked

The engine rejects rather than approximates:

- the live-only partial-fill retry in the tag-120 route;
- short routes outside the compiled 561-563 family;
- dynamic or structurally new leverage callback programs;
- dynamic protection properties, unsupported protection methods, and direct live
  pair-lock mutation outside the compiled protection program;
- broader shared-wallet pressure and multi-pair tie-breaks beyond the captured
  APE/AAVE equal-timestamp fixture.

## Required path to the full certificate

Every remaining branch must be lowered or certified incrementally:

1. capture an official Freqtrade fixture that reaches the branch;
2. freeze config, data, market metadata, Freqtrade version, and image digest;
3. compare the complete normalized trade surface with zero tolerance;
4. compare common wallet/trade/order state after every visible candle;
5. retain a smallest-prefix mismatch replay on failure.

No release may claim arbitrary or full X7 execution until the combined spot/futures
certificate and the 80-pair, five-year fresh performance gate both pass.
