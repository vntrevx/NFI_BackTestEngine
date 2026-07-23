# Changelog

All notable changes are recorded here. This project follows Semantic Versioning.

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
