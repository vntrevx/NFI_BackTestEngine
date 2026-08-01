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

For the reviewed X7 source shape through v17.4.435, these callback families execute in
Rust:

- backtest lifecycle no-op delegation and first-entry `order_filled` state writes;
- source-compiled custom stake and entry/exit confirmation programs;
- managed long exits for normal (1-13), pump (21-26), quick (41-53),
  rebuy (61-65), high-profit (81-82), rapid (101-110), top-coins (141-145),
  and scalp (161-163);
- managed short normal, pump, quick, rebuy, high-profit, rapid, scalp, and top-coins
  fallback exits, plus their short adjustment routes;
- the dedicated rebuy ladder and level-3 de-risk transition for tags 61-65;
- the shared system-v3.2 derisk/grind adjustment used by all 57 managed tags,
  including rebuy trades after their first level-3 de-risk fill;
- the tag-120 spot/futures backtest grind route: source-ordered `gm0`, `dl1`/`dl2`,
  `gd1` through `gd6`, their partial exits and stops, and the `d1` buyback cycle;
- the tag-121 regular-mode de-risk/grind prelude and its source-ordered transition into
  the legacy grind state machine;
- source-ordered tag-dependent futures leverage, capped by the frozen per-pair market
  limit;
- Binance isolated-futures transport with frozen leverage tiers, calculated and
  adjustment-updated liquidation prices, long/short signals, mark-price funding events,
  fees, historical precision, derisk/grind order replay, and final-surface normalization;
- static `CooldownPeriod`, `StoplossGuard`, `MaxDrawdown`, and `LowProfitPairs`
  definitions with side-aware local/global pair locks in the global event loop.

The route table preserves X7's callback order. All managed-long and managed-short
dispatch blocks, pure decision prefixes, and route-local state policy are compiled from
the supplied AST into separate generic primary programs. Recursive matcher IR handles
compound tags and the short fallback side predicate; both sides' quick/rapid conditions
are their own Scalar IR. Stop, target-cache, protected-signal, pure-scalp, and rebuy
terminal values are source data. Rust returns the generic result and independently executes
the legacy route as a shadow, comparing both the decision and complete target-cache state.
Any disagreement fails closed. An unknown companion tag still fails before simulation.

Both system-v3.2 position-adjustment callbacks are independently source-compiled. Their
de-risk and Grind action order, level sets, exact tags, directional order scans, retry
windows, wallet guards, stake scaling, partial exits, and required dataframe columns are
serialized as `system-adjustment-program-v1`. Short behavior is compiled from the short
AST, never derived by sign-flipping the long program. The generic Rust evaluator is the
primary lane while the handwritten implementation remains an independent shadow during
migration. Both the returned stake/tag and all custom-state writes must match exactly.
Levels and tags are IR data; the runtime has no fixed five-level table or strategy-SHA
selector.

## Proof level

The source analyzer pins the whole strategy SHA for cache/evidence identity. Managed route
wrappers, `custom_exit`, and both system-v3 adjustment action sets are structurally
compiled, so their runtime method-hash gates are gone. Residual stop/target helpers remain
identity-bound until their own staged promotion. A
changed compiled callback therefore recompiles into the same generic opcode set or fails
structurally before inheriting unrelated Rust behavior.
It also inventories literal condition-index branches and the effective strategy
switches. Probe-only source changes are AST-bound to the expected class attribute and
old literal; routine upstream edits fail closed instead of silently changing the wrong
line.

Static exact lowering also passes for X7 v17.4.435 at upstream commit
`2bc3058ed4f8480ed7498efca49b5195c7b47e9b`. Its source SHA-256 is
`6bb2aae39223e8e6d1980534f4159edc14b857d304c9410c92ed53320982d64a`.
The system-v3.2 adjustment compiler extracts retry durations, profit thresholds,
de-risk state dependencies, and late grind predicates as typed operands and
comparisons. Rust therefore does not carry release-specific grind 4/5 thresholds.
The tag-121 regular-adjustment compiler likewise extracts separate spot and futures
stake ladders, thresholds, stop levels, and de-risk levels. Funding is included in the
futures callback profit snapshot before branch selection, matching Freqtrade's
callback boundary.

A narrow v17.4.435 runtime check additionally records exact final-surface parity for
one spot interval and one isolated-futures interval in
[`benchmarks/evidence/x7-v17.4.435-small-parity-2026-07-25.json`](../benchmarks/evidence/x7-v17.4.435-small-parity-2026-07-25.json).
Its claim boundary is deliberately limited to those eight trades and does not replace
the branch matrices or either continuous release certificate.

The latest branch matrices pin X7 v17.4.435 at upstream commit
`2bc3058ed4f8480ed7498efca49b5195c7b47e9b` and Freqtrade 2026.5.1.
Thirteen official full-state fixtures reach:

- tag 121 in both spot and isolated-futures modes;
- `CooldownPeriod`, `StoplossGuard`, `MaxDrawdown`, and `LowProfitPairs`, including
  generated locks and locked-entry rejection in futures, plus the spot protection
  regression matrix;
- both long and short isolated-futures lifecycle paths with non-zero funding;
- the compound top-coins tag `141 142`;
- tag-dependent futures leverage values 2, 3, and an exchange-tier-capped 5;
- an actual isolated-futures liquidation exit after partial position reductions.

The fixtures live under `benchmarks/fixtures/captured/x7-*`. Every manifest seals the
effective strategy, compact candle inputs, native and raw reference market metadata,
official export, normalized surface, observer trace, and coverage report.

The spot and futures branch contracts now pass independently. These compact fixtures
are not continuous release certificates: the mode-aware release gate separately
requires an 80-pair, five-year oracle and three-OS wheel evidence for each claimed
mode. The v1.1.0 Futures certificate closes this gate for the listing-aware
`20210726-20260726` universe. Pre-listing history is never synthesized: every pair's
sealed market onboarding and first available candle determine activation, while the
remaining continuous portfolio state is processed without timerange chunking.
The combined latest-revision Spot/Futures status remains `preview` because the
v1.0.0 Spot certificate uses an earlier X7 source and candidate wheel.

The broadest current isolated-futures portfolio differential uses the same v17.4.435
source over ten pairs and `20220101-20220701`. Native and pinned Freqtrade 2026.5.1
surfaces are byte-identical: 63 trades, 296 orders, 53 long trades, 10 short trades,
24 funded trades, two tag-120 trades, and every summary token match at zero tolerance.
The engine core completed in 10.89 seconds and the full cache-warm native pipeline in
38.37 seconds; the offline official run took 339.61 seconds. These one-run timings are
diagnostic, not a repeated or release-grade speed claim. The sealed hashes and explicit
limitations are in
[`benchmarks/evidence/x7-10pair-futures-2022h1-parity-2026-07-27.json`](../benchmarks/evidence/x7-10pair-futures-2022h1-parity-2026-07-27.json).

The representative v17.4.435 Futures certificate covers 80 pairs and five years.
Pinned Freqtrade completed once in 6,430.90 seconds. Three deterministic final-wheel
preserved-vector runs completed in a 529.38-second median, producing a 12.148x
observed speedup and the same normalized surface SHA-256
`99fc0bd3f7622ba7feb0d16f3f76d5053b16c15db80568c257d29d9ee3af4ed5`.
One cold seed proves the complete strategy-to-vector pipeline, while nine official
full-state probes reach tag 121, all four protections, locks, compound tags, variable
leverage, liquidation, and both long and short funded lifecycles.

The latest annual single-pair certificate is X7 v17.4.418 on APE/USDT:USDT isolated futures from
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
candles. The scheduled latest-NFI workflow checks upstream and engine identities every
four hours and retains compact compatibility evidence. A source change outside the
reviewed state contracts can continue immediately; a changed state contract is visible
before a four-to-five-year run consumes resources. Missing Futures branches are handed
to a separate nightly, two-hour, resumable discovery lane. Only a minimized independent
official/Native exact fixture may open a Draft candidate PR.

## Still blocked

The engine rejects rather than approximates:

- the live-only partial-fill retry in the tag-120 route;
- the separate legacy short-grind tag 620 route;
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
