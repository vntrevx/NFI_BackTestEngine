# Changelog

All notable changes are recorded here. This project follows Semantic Versioning.

## Unreleased

## 1.10.6 - 2026-09-02

- Extended the startup wordmark so `BACKTEST ENGINE` is rendered in the same
  five-row ASCII-art style as `NFI`.
- Preserved the selected strategy, timerange, pair count, and resume status beneath
  the expanded product banner.


## 1.10.5 - 2026-09-02

- Canonicalized observer-facing Futures free-wallet values to one nano-USDT while
  leaving simulation and trading arithmetic unchanged.
- Regenerated two affected derived projections from their immutable official traces,
  fixing the nightly fixtures and newer release-candidate evidence under one contract.


## 1.10.4 - 2026-09-01

- Replaced the guided restart's raw strategy-path prompt with a numbered strategy menu
  discovered directly from the NFI checkout.
- The current strategy is marked and defaults to selection 1; NFI generations are
  ordered newest first, and selecting another entry loads and analyzes that strategy.


## 1.10.3 - 2026-09-01

- Saved projects now offer a full guided restart before execution: choose the strategy
  again, then select trading mode, exchange, market count, data, and timerange.
- Guided restarts write a separate generated config and output while preserving prior
  run evidence. Enter continues the existing saved project.


## 1.10.2 - 2026-09-01

- Completed outputs now trigger an explicit choice to reuse the existing result or
  start a fresh backtest with the same saved settings.
- Fresh runs use a new sibling output and retarget the saved project only after final
  CPU consent, preserving all prior evidence. `--new-run --yes` provides the explicit
  unattended path.


## 1.10.1 - 2026-09-01

- Added a fail-closed confirmation before `nfi-bte run` starts input preparation or
  simulation. The prompt reports the planned CPU worker count; Enter defaults to No,
  while explicit `--yes` preserves unattended execution.
- Added physical, logical, affinity-visible, and safe worker counts to the persisted
  run preflight. The compact readiness line now distinguishes parallel workers from
  visible logical CPUs.


## 1.10.0 - 2026-09-01

- Rebuilt `nfi-bte run` as a product-style terminal flow: compact NFI ASCII branding,
  one rotating in-place progress line, one-line system readiness, and concise result
  artifact paths.
- Made the complete Freqtrade-style result view the default, including backtesting,
  open-trade, entry-tag, exit-reason, mixed-tag, summary-metric, and strategy-summary
  tables. Metrics unavailable from the sealed Native trade surface remain explicitly
  marked instead of being inferred.
- Added presentation metrics for daily results, stake and duration statistics,
  entry/exit timeouts, mixed tags, long/short profit, and zero-trade pair retention.
- Completed runs now regenerate derived presentation files on display while preserving
  immutable simulation evidence.


## 1.9.1 - 2026-09-01

- Fixed completed-run reuse after presentation-only package upgrades. The runner now
  accepts an older completed identity only when the package version is the sole
  difference, validates every preserved artifact, and still rejects incomplete or
  materially changed runs without modifying their evidence.

## 1.9.0 - 2026-08-31

- Replaced the browser-oriented `report.html` artifact with a compact Freqtrade-style
  `report.md`, portable ASCII tables, an explicit zero-trade explanation, and a simpler
  terminal result summary. Report generation removes stale HTML output and no longer
  prompts to open a browser.

## 1.8.4 - 2026-08-31

- Replaced the arbitrary one-pair example with BTC and added exact `1`, `10`, `20`,
  `40`, `80`, `100`, `all`, and custom first-run choices.
- Numeric portfolio sizes now execute NFI's current Binance volume/filter policy in
  pinned Freqtrade, preserve its ranked order in the saved project, and retry transient
  exchange failures. Large selections show the measured long-run memory warning before
  candle downloads begin.



## 1.8.3 - 2026-08-31

- Reworked the first run for non-technical users: safe quick-test defaults, an
  NFI-maintained large-pair preset, managed candle storage, a seven-day default
  period, live stage percentages with elapsed time, and explicit report locations.
- Added bounded retries for transient Binance candle failures. Exhausted attempts now
  preserve reusable partial downloads, write the technical trace to a diagnostic log,
  and return a short actionable error instead of an internal Python stack trace.


## 1.8.2 - 2026-08-31

- Replaced the first-run Freqtrade-config dead end with a mode-first wizard. Fresh
  checkouts now generate a credential-free, self-contained Spot or isolated-Futures
  config, preserve strict explicit-config loading, and need no manual JSON editing.

## 1.8.1 - 2026-08-31

- Generalized X7 system-adjustment lowering for exact local aliases of enable flags,
  exit helpers, system-v3 class constants, candle features, Futures mode, trade side, and
  liquidation operands. Mismatched alias shapes still fail closed; upstream v17.4.585
  now passes static Native checks and executes Spot and isolated-Futures workloads
  without strategy-version or source-hash branches.
  Bounded Spot and isolated-Futures fixtures match independent Official Freqtrade
  trade surfaces and every-candle state at zero tolerance; they are not five-year
  or release certificates.
- Reduced CI probe-matrix cost by reusing descriptor-validated v3 coverage instead of
  rereading fixture paths, releasing retained fixture payloads only in validation-only
  tests, and mutating then restoring the semantic registry for negative checks instead
  of making six full copies.
- Bounded compatibility automation to one reconciled blocker issue and one open
  exact-fixture Draft per trading mode. New immutable identities close only stale
  automation-owned Drafts; non-Draft review work is never mutated automatically and
  prevents another candidate from opening in the same mode.

## 1.8.0 - 2026-08-31

- Removed the fixed two-state-history assumption from source-compiled state operands.
  X7 programs can now declare their required maximum history, while omitted bounds
  preserve the prior two-state default and malformed or excessive requests fail closed.
- Qualified X7 v17.4.581 at upstream commit
  `01b1304afaa2a1385754908817ea91be5149ffc9`. Spot and isolated-Futures bounded
  transition fixtures both observe the changed `gm0` route and match independent
  Official Freqtrade trade surfaces and full state at zero tolerance.
- Repaired current-upstream discovery publication: changed-target identities are
  carried through candidate assessment, runtime-generated target fixtures validate
  against the sealed schema, and captures stop at the first pair-bound target event.
  One day of context keeps paired current/baseline evidence under the unchanged
  30 MiB candidate ceiling.
- Preserved the product certification boundary. Historical five-year Spot and Futures
  certificates remain version-bound to v1.0.0 and v1.1.0; v1.8.0 does not combine or
  relabel them.

## 1.7.0 - 2026-08-29

- Completed materialized semantic traces across the full Official Freqtrade and
  Native execution surfaces. Each source retains its own callback/event granularity;
  the common every-candle projection compares wallet, trade, order, protection, and
  counter state at zero tolerance.
- Added a versioned Official reference-state schema that retains both open and closed
  trades, plus a single fail-closed migration path for immutable legacy traces that
  omitted open trades.
- Qualified X7 v17.4.580 at upstream commit
  `b22cc60d1c018eeb984cb02a125bb790042bebd0` for source-compiled Spot and Futures
  stateful closure. Historical v17.4.473 release-candidate fixtures remain the
  distribution regression identity; this release does not claim a new continuous
  five-year or combined Spot/Futures certificate.
- Retained build-once release publication, SHA-256 manifests, required-CI
  commit matching, Linux/macOS exact-fixture evidence, and byte-identical RC-to-stable
  promotion. Native Windows is unsupported; WSL2 runs the Linux build and ABI.

## 1.6.1 - 2026-08-16

- Added `nfi-bte update` and a bounded latest-release notice for installed CLIs.
- Kept source checkouts developer-managed and preserved the v1.6.0 simulation and
  certification boundaries.

## 1.6.0 - 2026-08-14

- Added the source-compiled Full Native transport: Rust now executes the complete
  Indicator, Signal, Tag, callback, order, wallet, and state pipeline without
  importing or executing strategy Python at runtime.
- Added content-bound Full Native worker calibration, pair-local raw-frame loading,
  live-value release, and direct file-backed simulator handoff without persistent
  analyzed Feather outputs.
- Added fail-closed spool-capacity admission from the run's gap-fill bound, automatic
  delete-on-close cleanup, and profile evidence for admitted versus actual bytes.
- Split NFI manager validation failures from pair/timestamp/route/source-located
  runtime diagnostics, and validate the embedded simulator config before expensive
  pair preparation.
- Connected typed Rust vector output directly to the chronological simulator, with
  pair-parallel DAG preparation and no parallel wallet or order mutation.
- Preserved SHA-verified Feather as the bounded-memory evidence/replay path and added
  zero-tolerance trade-surface and every-candle full-state transport parity tests.
- Certified a five-year Full Native Spot workload with three byte-identical results,
  a pre-admitted 38.47 GB temporary spool bound, and no retained spool files. This is
  a single-host performance/storage claim, not a new official Freqtrade certificate.
- Generalized current X7 Grind-5 fallback lowering for `slice_profit_exit`, Boolean
  protection columns, bare Derisk state, Futures mode, and liquidation-distance
  expressions without strategy-version, pair, or expected-result branches.
- Pinned the Freqtrade same-candle contract: position adjustment and its filled order
  are applied before stop/exit evaluation. The external 2022 Futures report that
  motivated the hotfix remains unclaimed until its sealed input is supplied.
- Normalized the X7 NaN-tolerant Chaikin helper and its explicit NumPy `float64`
  zero buffer into generic Native kernels, keeping old release fixtures on the same
  Full Native path instead of falling back during installed-wheel verification.

## 1.5.0 - 2026-08-10

- Promoted source-compiled generic state-machine programs to the sole current
  Native lane for supported NFI stateful callbacks, including managed long/short
  exits, rebuy, regular adjustments, Grind, typed custom state, and finite
  source-ordered filled-order iteration.
- Removed current-runtime X7 shadow backends and method-hash behavior gates after
  independent exact proofs, while retaining backward readers for sealed evidence.
- Added structure-driven IR optimization, live-column projection, and incremental
  order aggregates without Signal, tag, strategy SHA, pair, timerange, or expected
  result specialization.
- Upgraded latest-NFI automation to bind NFI, engine, pinned Freqtrade, and semantic
  profile identities; Spot and Futures are classified independently and sealed by
  one atomic hosted canary.
- Added deterministic Native-exact, generic-review, bounded-discovery,
  exact-fixture Draft PR, external-data-deferred, and official-only routes. Unknown
  semantics use the announced official fallback when the pinned environment supports
  them, and are never promoted or merged automatically.
- Kept this as a product release with `combined_full_x7_certified=false`. The
  five-year Spot and Futures certificates remain bound to v1.0.0 and v1.1.0.

## 1.4.1 - 2026-07-30

- Fixed a false `long_exit_rebuy` compatibility blocker on Python 3.13 and
  Python 3.14. The callback had not changed; those Python versions changed the
  default empty-field rendering of `ast.dump()`.
- Made reviewed callback identities stable across every advertised Python runtime
  while preserving fail-closed rejection for real stateful route changes.
- Added lightweight Python 3.13 and 3.14 CI checks for the AST identity contract
  without duplicating the full three-operating-system test suite.
- Retained the v1.4.0 execution semantics and independent v1.0.0 Spot and v1.1.0
  Futures certificate boundaries. This hotfix does not claim a new combined Full X7
  certification.

## 1.4.0 - 2026-07-30

- Proved the latest NFI Signal 65 transition on both Spot and Futures with paired
  previous/latest official Freqtrade executions and latest Native execution.
  Changed-source coverage, trade surfaces, and full state are all exact.
- Generalized callback lowering and execution through source-derived
  `state-machine-program-v2`, including transitive helpers, tags, routes, thresholds,
  Grind state, and reviewed Freqtrade callback semantics without Signal-, strategy-,
  pair-, timerange-, SHA-, or expected-result runtime branches.
- Added digest-bound compact compatibility fixtures outside ordinary repository
  clones. Archive identity, paths, file types, and extracted size are verified before
  targeted qualification.
- Hardened official Freqtrade containers for portable bind ownership without fixed
  image usernames or UIDs, and retained only bounded failure diagnostics.
- Completed unattended four-hour Spot/Futures qualification on GitHub-hosted runners.
  Fully exact revisions skip redundant deep discovery; incomplete qualifications
  retain the bounded fail-closed discovery and visible official fallback paths.
- Retained the independent v1.0.0 Spot and v1.1.0 Futures five-year certificate
  boundaries. v1.4.0 is a product release and does not claim a new same-candidate
  combined Full X7 certification.

## 1.3.0 - 2026-07-30

- Added an explicit, user-visible transition from blocked Native execution to the
  pinned official Freqtrade lane, without mutating or overstating Native evidence.
- Lowered supported Signal, tag, Grind, protection, leverage, and trade-state
  behavior into source-derived generic IR instead of version- or Signal-specific
  runtime branches.
- Added a four-hour latest-NFI compatibility lane with independent Spot and Futures
  checks, AST/IR change classification, targeted official/Native full-state
  verification, append-only evidence, and deduplicated semantic and health issues.
- Added a separate bounded Futures branch-discovery lane. It searches listing-aware
  quarterly shards under a two-hour, single-worker budget, resumes only an exact
  identity cursor, and opens a size-limited Draft PR only after exact independent
  trade-surface and full-state qualification.
- Preserved the safe storage lifecycle: compact discovery records remain auditable,
  while raw candles, caches, and traces are excluded from permanent automation
  branches and completed discovery evidence remains protected by default.
- Retained the independent v1.0.0 Spot and v1.1.0 Futures five-year certificate
  boundaries. v1.3.0 is a product release and does not claim a new same-candidate
  combined Full X7 certification.

## 1.2.0 - 2026-07-29

- Froze the public v1.1 regression contract and added an append-only verification
  ledger that keeps quick compatibility checks, failed attempts, and release
  certification states distinct.
- Split CLI orchestration, strategy analysis and lowering, the X7 adapter and trade
  manager, reference/certification/reporting code, the Rust simulator, and the
  Feather boundary into responsibility-focused modules without changing exact
  result surfaces or event ordering.
- Applied measured, input-derived optimizations to vector preparation, the Rust event
  loop, scalar overlays, Feather decoding, and the Python/Rust boundary. No strategy,
  pair, timerange, SHA, or expected-result runtime branch was added.
- Added complete `summary.json`, `trades.csv`, and self-contained `report.html`
  artifacts, compact terminal output, and a one-command project setup/run/resume
  flow.
- Added hard-link-aware storage accounting and safe `clean --apply` receipts.
  Completed runs require explicit opt-in; preserved runs, release evidence,
  certificates, official Oracles, Freqtrade ZIPs, external paths, and active or
  ambiguous runtimes remain protected.
- Added managed default run directories and a configurable disk-aware cache budget,
  preventing ordinary use from retaining unbounded development-style vector caches.
- Split pull-request, nightly, protected long-certification, and release trust
  boundaries, with required CI on Linux, Windows, and macOS and exact final-wheel
  Spot/Futures fixtures on all three operating systems.
- Added build-once product-release publication for releases that do not claim a new
  same-candidate combined Full X7 certificate. The v1.0.0 Spot and v1.1.0 Futures
  certificates remain valid only for their own sealed candidates and are not
  relabeled as v1.2.0 certification.

## 1.1.0

- Certified the continuous X7 v17.4.435 Binance USDT-M isolated-Futures workload
  over 80 listing-aware pairs and `20210726-20260726`. The one official
  Freqtrade 2026.5.1 oracle and all native runs produce the same 174-trade,
  795-order surface at SHA-256
  `99fc0bd3f7622ba7feb0d16f3f76d5053b16c15db80568c257d29d9ee3af4ed5`.
- Added a content-addressed preserved-vector certification lane: one cold seed
  proves the complete strategy-to-vector pipeline, while three to five fresh
  simulation processes provide release timing. The final installed-wheel median is
  529.38 seconds versus 6,430.90 seconds official, an observed 12.148x speedup.
- Added final-wheel `exact-fixture` measurements for Windows x86_64, Linux x86_64,
  and macOS arm64. Each platform excludes one warmup, repeats three times and
  extends to five above 5% spread, records median wall time and peak RSS, and
  must produce one identical full-state Futures result before evidence is sealed.

- Added centralized release-mode contracts for Binance spot and Binance USDT-M
  isolated futures. Cross margin, other exchanges, malformed pairs, incomplete
  role sets, and fewer than 80 fully covered five-year pairs now fail closed.
- Added deterministic universe discovery from frozen market metadata, mode-aware
  input locks, exact futures candle/funding/mark role validation, and read-only
  compatibility for existing spot locks.
- Split Full X7 certification by mode and added a combined release gate. Spot and
  futures must bind the same strategy, package, native engine, reference identity,
  timerange, and five-timeframe scope. Missing per-mode three-OS evidence leaves
  the combined result in `preview`.
- Strengthened official probe contracts: futures release evidence must reach tag
  121, long and short lifecycle paths, non-zero funding, compound tags, variable
  leverage, a real liquidation exit, all four protections, pair locks, and a
  locked-entry rejection.
- Added an automatic result presentation layer for every research outcome:
  `summary.json`, spreadsheet-ready `trades.csv`, and a responsive self-contained
  `report.html` with equity/monthly charts, performance and risk metrics,
  pair/tag/exit/year breakdowns, recent trades, execution context, blockers, and
  an official exact-parity verdict. Futures views include direction, leverage,
  signed funding, liquidation-exit, and protection-lock summaries.
- Replaced raw-JSON-only terminal results and run listings with compact readable
  summaries while retaining explicit `--json` output for automation.
- Added opt-in `--full-report` terminal tables for every pair, entry-tag group,
  exit-reason group, and direction, including average profit, total profit, win rate,
  win/draw/loss counts, zero-trade configured pairs, and totals.
- Added `nfi-bte report` to regenerate derived presentation files without
  repeating a backtest. Official confirmation refreshes only the derived verdict;
  original run and trade-surface evidence bytes remain unchanged.
- Display peak RSS only from a measured process-tree checkpoint and otherwise
  label the enforced memory budget. Drawdown is explicitly defined as
  closed-trade equity drawdown to avoid overstating Freqtrade metric equivalence.
- Replaced loose `--resume` file reuse with an identity-bound stage machine.
  Completed runs now validate simulation-input, simulation-result, and trade-surface
  byte counts and SHA-256 values before returning without an engine call. Partial
  simulation stages resume only from ordered checkpoints, while tampered,
  contradictory, or uncheckpointed result artifacts fail closed without deletion.
  Completed v1.4 evidence remains readable through the same artifact validation,
  without in-place migration.
- Extended static exact lowering through upstream X7 v17.4.435. Method-local grind
  retry windows, late-entry profit gates, de-risk dependencies, and grind 4/5
  fallback predicates are now compiled into typed IR from the reviewed callback
  source and evaluated generically in Rust. A changed literal changes the IR; an
  unsupported expression still fails closed. The X7 adapter now also consumes the
  exact immediate-fill proof for unreachable open-order timeout callbacks and
  revalidates its opcode and execution scope before omitting executable state.
  This is a compatibility result, not a replacement for a continuous official spot
  or futures certificate.
- Sealed a path-independent v17.4.435 compatibility record for one spot and one
  isolated-futures interval. Both native trade surfaces are byte-identical to
  pinned Freqtrade 2026.5.1; the record explicitly excludes long-horizon,
  branch-completeness, and performance claims.
- Compiled tag 121's complete spot and futures regular-adjustment branches from
  upstream source into typed IR. Futures stake sizing, de-risk profitability, and
  funding-before-adjustment ordering now follow the selected market mode without
  fallback constants or tag-specific runtime patches.
- Replaced the older mixed-source probe set with thirteen v17.4.435 fixtures bound
  to one upstream commit and Freqtrade 2026.5.1. The independent futures matrix now
  proves tag 121, long and short lifecycle paths, funding, compound tags, variable
  leverage, liquidation, all four protections, locks, and locked-entry rejection
  with zero-tolerance final-surface and full-state parity.
- Extended tag 120's source-bound grind state machine to both spot and Futures
  backtests without widening older spot-only evidence. Added exact Freqtrade funding
  refresh at same-timestamp position increases, Python ties-to-even profit formatting,
  configured slot reporting, and NumPy's eight-lane summary reduction.
- Sealed a v17.4.435 ten-pair, six-month Binance isolated-futures differential:
  all 63 trades and 296 orders, including long, short, funded, compound-tag, and
  tag-120 routes, are byte-identical to the immutable Freqtrade 2026.5.1 surface.
  The one-run cache-warm timings remain diagnostic and do not replace the full release
  gates.

## 1.0.0

- Completed the continuous X7 v17.4.421 spot oracle over 80 pairs and
  `20210101-20260101` with pinned Freqtrade 2026.5.1. The final 927-trade,
  11,783-order native and official surfaces are byte-identical at SHA-256
  `8ae4fe84eaf869904cc8a26056f08218548546b316f620441e57417c24cac38c`.
- Added identity-bound reconciliation for a completed official export after a native
  parity correction. It reuses the immutable Freqtrade ZIP only when the run, strategy,
  image, platform, market snapshot, and official surface all match the new cold native
  baseline; official backtest bytes are never rewritten.
- Compiled X7's source-ordered signal-65 early-recovery exit and structurally proved its
  orderbook timeout callbacks unreachable only under the native immediate-fill backtest
  contract. Threshold, side, orderbook, or price-callback changes still fail closed.
- Added seven X7 v17.4.421 branch-reaching official fixtures for tag 121, all four
  supported protections and pair locks, compound tags, variable leverage, and a real
  isolated-futures liquidation exit. Each fixture passes zero-tolerance surface parity
  and complete-state parity against pinned Freqtrade 2026.5.1.
- Derived fixture data roots from sealed candle roles so both spot layouts and the
  standard `data/futures` layout run identically on Windows, Linux, and macOS. Lock
  surfaces now retain the official canonical field order, while full-state futures
  projections exclude synthetic base positions, normalize sub-nano wallet float noise,
  and sort lock snapshots independently of insertion order.
- Added AST-bound numeric probe toggles, informative-only pair staging, and pinned
  on-demand reference-market capture without line-number or date-specific source edits.
- Matched Freqtrade's config-over-strategy stoploss precedence and its observable
  futures float/order-replay boundaries for partial exits, liquidation refresh, and
  eight-decimal profit normalization.
- Corrected Full X7 release input selection to require strict five-year interval edges
  while sealing Freqtrade-compatible pre-listing startup shortfalls. Data downloads now
  flatten config includes, omit unrelated API service settings, and reject silent
  zero-output Freqtrade failures even when the container returns success.
- Bound Full X7 data directories and seal request fields to the portable release lock,
  required branch probes to use that same upstream commit, and made the pinned warmup
  capture a missing raw reference-market snapshot before all measured runs go offline.
- Split candidate building, prerelease publishing, and stable promotion so a certified
  candidate is built once and the RC and stable GitHub releases reuse byte-identical
  SHA-256-verified distribution assets at the same source commit.
- Made source-tree version identity come from `pyproject.toml`, preventing ignored
  stale editable-install metadata from contaminating certification reports.
- Packaged the pinned Freqtrade tracer with the wheel and mount only the engine
  package plus tracer roots into official containers, so installed release tools
  neither depend on a source checkout nor shadow container binary dependencies.
- Separated Full X7 proof roles: the continuous official Freqtrade oracle now runs
  once for exactness, while only the installed native candidate repeats three to five
  times for timing and peak-RSS statistics. Added identity-bound oracle import and
  resumable native/probe checkpoints so an interrupted certificate does not discard a
  completed multi-year reference run.
- Replaced the official research lane's duplicate all-RAM analyzed frames and
  per-candle Python row lists with a source-hash-guarded Arrow datastore. Indicator
  calls retain official pair order, callback reads retain the exact 1,000-candle
  DataProvider window, and storage metrics plus ephemeral cleanup are sealed in every
  reference report. Flushed/read Arrow files are advised out of Linux page cache so
  disk-backed data does not exhaust the cgroup allowance. The final 80-pair,
  six-month proof remained byte-identical while using 3,637,440,512 peak bytes and
  zero swap.
- Matched the pinned Freqtrade 2026.5.1 final surface exactly for the latest X7
  v17.4.418 over 80 configured spot pairs and the bounded
  `20250701-20260101` interval: 167 trades, 402 orders, 23 rejected signals,
  and every normalized numeric token are byte-identical.
- Preserved Freqtrade's open-trade-first pair scheduling, confirmation-rejected
  order-ID consumption, closure-order export, trade-open price precision, and
  rebuy-to-shared-grind stake transition. Focused Rust regressions protect each
  lifecycle rule without pair- or date-specific exceptions.
- Compiled X7's source-ordered tag-dependent leverage callback, per-pair exchange caps,
  tag-121 regular adjustment, and transition into the legacy grind state machine.
- Added Binance isolated-liquidation tiers and recalculation after position adjustment;
  matched Freqtrade's stop-loss-before-liquidation collision order and retained a
  focused no-fallthrough regression.
- Added static `CooldownPeriod`, `StoplossGuard`, `MaxDrawdown`, and `LowProfitPairs`
  programs with deterministic local/global pair-lock state in the Rust event loop.
- Added a one-command official reference for completed research runs. Strategy and
  sanitized effective-config copies are now sealed inside the run so daily NFI updates
  cannot change an older verification input.
- Added an X7 v17.4.418 annual APE futures certificate: the engine and pinned Freqtrade
  2026.5.1 produce the same 11-trade, 164-order surface with zero tolerance. The narrow
  evidence does not claim an actual liquidation exit, enabled protection, pair lock, or
  tag-121 entry.
- Added long-horizon history-availability seals and avoided a redundant prepend
  download when a requested candle file did not exist yet.
- Reduced long-history precision extraction from one Python formatting call per
  OHLC value to one call per distinct monthly price while preserving Freqtrade's
  exact NumPy formatting rule.
- Stabilized nullable tag columns in every compressed Arrow record batch and
  decode the transport marker before simulation, avoiding Arrow2's zero-byte
  UTF-8 buffer panic without changing strategy tags.
- Removed redundant vector-cache copies and repeated hashes on same-volume runs
  with immutable hard links, verified copy fallback, fail-closed cache-hit hash
  binding, and one prune pass per vector batch.
- Buffered each pair's sequential file-backed event stream across chronological
  round-robin switches, removing one seek/read system-call pair per candle while
  keeping long-history vectors outside the Rust heap. Read windows overlap the
  compiled five-candle callback history so block boundaries do not cause alternating
  current/previous-row reads.
- Buffered Arrow-to-spool writes as well, removing one kernel write per normalized
  candle while preserving the same fixed-width disk-backed transport.
- Folded NFI short-tag validation into the existing candle-validation pass while
  retaining general-validation error precedence, removing a redundant full-history
  spool scan.
- Cached one next timestamp per pair in the chronological event loop, removing
  repeated file-backed timestamp reads while retaining original pair order for
  equal-time wallet and slot decisions.
- Added a no-position/no-entry fast path that reads only the two entry-flag bits
  instead of materializing a complete candle; observer ordering and event counters
  remain unchanged.
- Added a result-only sparse scheduler: idle pairs advance directly to their next
  sealed entry signal, while pairs with an open trade retain every-candle execution.
  Full-state observer runs remain dense, and lightweight timestamp counting preserves
  comparable profile totals.
- Added end-to-end research stage timings so vector preparation, manifest
  construction, native simulation, and surface generation can be optimized from
  measured evidence rather than engine-core timing alone.
- Added reproducible certification bundles with at least three representative
  repetitions, maximum memory, median timing, and separate branch-reaching full-state
  probes. S3-compatible evidence transfer verifies both object metadata and content
  hashes.
- Preserved the exact 750-trade result on a sealed X7 v17.4.418, 80-pair,
  `20210101-20260101` native diagnostic while reducing process time from 2,022.07
  seconds to 763.70 seconds and event-loop time from 1,638.36 seconds to 474.86
  seconds. The evidence explicitly remains single-host, warm-vector, and non-official.
- Hardened completed-run official verification against common service-only API settings
  and the pinned Freqtrade `list-pairs` CLI contract while retaining a read-only,
  hash-checked source configuration.

## 0.5.0 - 2026-07-20

- Replaced fixed per-worker memory assumptions and reserve percentages with a
  content-bound full-timerange probe, OS-native peak RSS, and live admission against
  current free memory, CPU affinity, and explicit user caps.
- Added aggregate Rust phase profiles without changing result bytes.
- Added a SHA-verified row spool for Feather vectors so multi-pair, multi-year engine
  memory no longer duplicates every candle and callback feature in heap memory.
- Added `--recalibrate` and an optional disk-backed `--spool-directory`, while keeping
  the useful calibration pair output and invalidating measurements when workload or
  hardware identity changes.

## 0.4.0 - 2026-07-19

- Established the moving-target product contract: current NFI source in, five complete
  years by default, fast native screening, and exact official Freqtrade confirmation.
- Added `nfi-bte strategy check` and daily upstream X7 compatibility reporting so a new
  callback contract is detected before long data preparation begins.
- Raised release-grade performance eligibility from one year to at least four years
  while retaining short fixtures as diagnostic evidence.
- Added digest-verified one-command installers for Windows, Linux, and Apple Silicon
  macOS using isolated uv tool environments.
- Grouped Dependabot maintenance by ecosystem on a monthly schedule and protected local
  agent/tool directories from accidental commits.

## 0.3.0 - 2026-07-19

- Added Docker daemon CPU and memory inspection so Docker Desktop VM resources are
  budgeted separately from native host resources.
- Added one-at-a-time managed Freqtrade containers with portable process locking,
  cgroup memory limits, live usage accounting for unrelated containers, ownership
  labels, exact CID cleanup, and stopped-container housekeeping that never prunes
  unrelated workloads.
- Added cgroup peak and OOM-event reporting while retaining hardware-aware native
  pair-process parallelism and exact, unsplit timerange semantics.
- Added `nfi-bte system docker` for readable daemon policy and managed-container
  diagnostics.

## 0.2.0 - 2026-07-19

- Added `nfi-bte init`, a small setup wizard that detects standard Freqtrade strategy,
  config, exchange-data, pairlist, timerange, and output settings without storing
  credentials.
- Added `nfi-bte run`, which creates the saved project on first use and subsequently
  runs or resumes it with one command.
- Added automatic selection of hash-valid resume mode for an existing project output,
  while keeping inline reconfiguration and destructive replacement fail-closed.

## 0.1.0 - 2026-07-19

- Added reproducible official Freqtrade benchmark fixtures and exact trade/state parity.
- Added a native PyO3 Rust simulator package with Linux, Windows, and macOS wheel builds.
- Added hardware-bound execution tuning, immutable data seals, and content-addressed caches.
- Added trusted NFI/Freqtrade vector workers with process isolation and no per-candle Python.
- Added checkpointed research preparation with frozen pairlists and fail-closed callback IR.
- Added automatic public market snapshots, exact official-export confirmation, a SQLite
  run registry, and memory-aware independent candidate batches.
- Added cache-stable vector evidence so warm and cold runs retain the same signal and
  column metadata.
- Added a strategy-oriented indicator memory budget and separate safe research-job count
  so batch preparation cannot reuse the much smaller Rust-engine memory assumption.
- Added opt-in short, leverage, funding, liquidation, per-pair precision, and partial-exit
  simulator contracts while preserving the captured spot-long fixture behavior.
- Added source-pinned X7 short-rebuy routes 561-563 and constrained isolated-futures
  execution with uniform callback leverage, mark-price funding, and exact order replay.
- Added a sealed APE/USDT:USDT annual futures certificate for 2022-04-01 through
  2023-01-01: 11 exact trades, 164 exact orders, 142 adjustments, one short trade,
  and eight funded trades with zero numeric tolerance.
- Added a SHA-verified projected Feather transport that avoids duplicating full X7 vectors
  into multi-hundred-megabyte simulation JSON.
- Added source-pinned Rust routing for 57 X7 managed long tags plus narrow legacy
  grind/BTC routes, with ordered target-cache mutation and fail-closed mixed-tag checks.
- Added the separate X7 rebuy ladder and level-3 de-risk transition for tags 61-65,
  without widening the captured APE certificate beyond its top-coins route.
- Added the source-ordered tag-120 spot/backtest state machine for first recovery,
  de-risk, six grind levels, partial exits, stops, and the `d1` buyback cycle.
- Added an offline Freqtrade 2026.5.1 ZEC differential fixture proving one tag-120
  trade and 13 orders through `gm0`, `gd1`, and `gd2` with zero tolerance.
- Added a source-static literal condition-index inventory which proves tag 121 is
  dormant in X7 v17.4.413 and keeps any future emitted signal fail-closed.
- Added bounded extraction of annotated class constants, including the source-defined
  `startup_candle_count`, without importing the strategy during preflight.
- Matched CPython 3.14 compensated float summation for Freqtrade `total_volume`
  instead of hiding one-ulp aggregation differences with rounding.
- Added Freqtrade-compatible Unix-second/millisecond timerange parsing and an exact
  offline tag-62 rebuy-exit differential fixture.
- Matched Freqtrade's timerange callback boundary by retaining pre-start callback
  context while excluding startup rows and the shifted head row from Rust execution.
- Added an offline APE/AAVE equal-timestamp differential fixture proving deterministic
  pair-order admission and one shared-slot rejection at `max_open_trades=1`.
- Fixed computed negative dataframe indices in the confirmation VM, which the first
  generic managed-long entry exposed after top-coins-only certification.
- Preserved the captured APE top-coins exact surface after the transport and route
  expansion.
- Added hardware-bound process pools, one-thread numeric-library limits, atomic
  cross-process cache publication, and measured four-process X7 preparation scaling.
- Embedded the Rust source fingerprint in the native extension so a stale development
  module falls back safely instead of running mismatched source.
- Pinned the vector runtime dependency set to the Freqtrade oracle versions and included
  those versions in immutable vector-cache identity.

The complete NFI X7 strategy-callback lowering is not included in 0.1.0.
