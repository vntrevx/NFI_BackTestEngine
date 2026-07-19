# Changelog

All notable changes are recorded here. This project follows Semantic Versioning.

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
