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

For the source-compiled X7 shape qualified through v17.4.580, these callback families
execute in Rust:

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
terminal values are source data. Rust returns the generic result directly. The legacy route
was retired from current payloads after independent decision and target-cache equality
proofs. An unknown companion tag still fails before simulation.

Research runs expose this as the `x7-generic-stateful` adapter lane. The immutable run
identity lists every stateful program root and requires its `primary` mode, so a source update
cannot silently fall back to a legacy-only Native path. The X7 adapter name now describes
the vector transport only; official Freqtrade fallback remains separate and visibly
announced when the generic contract cannot be proven.

Both system-v3.2 position-adjustment callbacks are independently source-compiled. Their
de-risk and Grind action order, level sets, exact tags, directional order scans, retry
windows, wallet guards, stake scaling, partial exits, and required dataframe columns are
serialized as `system-adjustment-program-v1`. Short behavior is compiled from the short
AST, never derived by sign-flipping the long program. The generic Rust evaluator is the sole
current lane. Its returned stake/tag and all custom-state writes were proven exact before
handwritten execution was retired.
Levels and tags are IR data; the runtime has no fixed five-level table or strategy-SHA
selector.

## Proof level

The source analyzer records the whole strategy SHA for cache/evidence identity only. Managed
routes, stop/target policy, `custom_exit`, and both system-v3 adjustment action sets are
structurally compiled, so runtime method-hash gates are gone. A
changed compiled callback therefore recompiles into the same generic opcode set or fails
structurally before inheriting unrelated Rust behavior.
It also inventories literal condition-index branches and the effective strategy
switches. Probe-only source changes are AST-bound to the expected class attribute and
old literal; routine upstream edits fail closed instead of silently changing the wrong
line.

Static exact lowering passes for X7 v17.4.585 at upstream commit
`47f3b66f4767fe228a74a98f0d4a7e51199e1488`. Its source SHA-256 is
`ff061a8c113b29a599306044cbcc2112ac2eb901f458a55de82bf15f93875e22`.
The system-v3.2 adjustment compiler extracts retry durations, profit thresholds,
de-risk state dependencies, late grind predicates, and bounded state-history reads as
typed operands and comparisons. Exact local aliases of enable flags, exit helpers,
system-v3 class constants, candle features, Futures mode, trade side, and liquidation
operands lower into the same generic IR. Mismatched alias shapes fail closed. Rust
therefore carries no release-specific alias, strategy-version, source-hash, pair, or
timerange branch.
The tag-121 regular-adjustment compiler likewise extracts its reverse order scan,
rebuy exclusions, dynamic Grind and de-risk tags, separate spot/futures stake ladders,
thresholds and stops, the leverage-scaled Futures drawdown fallback, and the
amount-based legacy-Grind continuation. Funding is included in the futures callback
profit snapshot before branch selection. Promotion required an independent reviewed
shadow to agree exactly; current payloads register no legacy shadow, and the preserved
comparison remains evidence rather than a runtime branch.

The v1.8.1 current-source qualification is deliberately separate from the immutable
distribution regression fixtures. Spot and Futures static reachability both report
zero reachable stateful gaps, complete closure, and Native compatibility for
v17.4.585. Two bounded fixtures compare independent Official Freqtrade and Native
trade surfaces and every-candle full state at zero tolerance:

- Spot `ADA/USDT` over `20251104-20251107`: 864 state events, stream SHA-256
  `c4295cbc14d4cfce8f0dd089277e503128c14aca967da73eedcd934fa91f7c2d`;
- isolated-Futures `THETA/USDT:USDT` over `20251113-20251117`: 1,152 state events,
  stream SHA-256
  `b0d3e80edb7af377a63dfc526e24a7ac766c0bd0d27a620247a7cb017e1a22c4`.

The exact manifests are preserved as
[`future-nfi-spot-v17.4.585-aliases-ada`](../benchmarks/fixtures/captured/future-nfi-spot-v17.4.585-aliases-ada/manifest.json)
and
[`future-nfi-futures-v17.4.585-aliases-theta`](../benchmarks/fixtures/captured/future-nfi-futures-v17.4.585-aliases-theta/manifest.json),
with the shared identity in
[`future-nfi-v17.4.585-alias-compatibility.json`](../benchmarks/evidence/future-nfi-v17.4.585-alias-compatibility.json).
They are compact current-source evidence, not five-year certificates. The
release-candidate contract continues to replay sealed historical fixtures and does
not relabel those version-bound certificates.

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
run. Deeper `dl1`/`dl2`, `gd3`-`gd6`, stop, and Futures-fallback branches are executable
and have focused Rust tests, but do not yet have branch-reaching official fixtures.
The separate latest-source Spot Derisk/Buyback fixture reaches tag 121 and 29 filled
`d1` adjustments, including the first d1 exit on the entry-fill timestamp. Its one
trade, 31 orders, final balance, trade surface, and full state exactly match pinned
Freqtrade 2026.5.1. The fixture is an AST-bound branch probe, not a continuous release
certificate.

The whole Grind cluster set is source-compiled rather than selected by tag-120 runtime
code. `grind-transition-program-v3` carries first-entry profit and stop, source-ordered
post-de-risk and ordinary clusters, arbitrary source-defined level counts, each stop tag,
retry/age and stake policy, the Futures drawdown fallback, and the bounded Derisk/Buyback
restoration transition. Its tag, thresholds, dataframe guards, wallet policy, stake
formulas, and leverage behavior are extracted from the strategy AST. Every reached
compiled action is compared with the independent legacy callback implementation; a
mismatch invalidates the Native run. The official proof is preserved in
[`benchmarks/fixtures/captured/x7-derisk-buyback-spot-v17.4.488-2023-01-01_16`](../benchmarks/fixtures/captured/x7-derisk-buyback-spot-v17.4.488-2023-01-01_16/manifest.json).

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
candles. The scheduled latest-NFI workflow checks NFI, engine, pinned Freqtrade, and
semantic-profile identities every four hours and retains compact compatibility evidence.
Spot and Futures remain separate checks; an atomic hosted canary must validate both before
the latest ledger identity advances. The canary also seals a deterministic automation
decision per mode: independently exact changes may use Native, missing coverage enters
bounded discovery, and blocked generic lowering remains official-only while an
evidence-only Draft review is opened. A source change outside the supported state contracts
is therefore visible before a four-to-five-year run consumes resources. Missing Spot or
Futures branches use the separate nightly, resumable discovery lane. Only a minimized
independent official/Native exact fixture may open a Draft candidate PR; no review or
candidate PR is merged automatically.

For a source-reachability audit, run:

```bash
nfi-bte strategy stateful-coverage NostalgiaForInfinityX7.py \
  --class NostalgiaForInfinityX7 --trading-mode futures \
  --output .nfi/stateful-coverage-futures.json
```

Run it once per mode. The report derives enabled entry tags from the source, refreshes
the callback call graph, and verifies that every reachable tag has sealed Native exit
and adjustment programs. Disabled or non-emitted source routes remain visible as
dormant evidence and do not qualify as implemented behavior.

## Still blocked

The engine rejects rather than approximates:

- live-only partial-fill retries, which are outside Freqtrade backtest reachability;
- any currently dormant route that a future source revision makes reachable before a
  sealed Native program exists (the current upstream short-grind route is one example);
- dynamic or structurally new leverage callback programs;
- dynamic protection properties, unsupported protection methods, and direct live
  pair-lock mutation outside the compiled protection program;
- arbitrary portfolio-scale shared-wallet pressure or multi-pair tie-breaks beyond
  the captured two- and three-pair configured-order permutations.

## Required path to the full certificate

Every remaining branch must be lowered or certified incrementally:

1. capture an official Freqtrade fixture that reaches the branch;
2. freeze config, data, market metadata, Freqtrade version, and image digest;
3. compare the complete normalized trade surface with zero tolerance;
4. compare common wallet/trade/order state after every visible candle;
5. retain a smallest-prefix mismatch replay on failure.

No release may claim arbitrary or full X7 execution until the combined spot/futures
certificate and the 80-pair, five-year fresh performance gate both pass.
