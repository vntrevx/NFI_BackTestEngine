# NFI Backtest Engine — Project Brief

## Goal

Build a high-speed research backtesting engine for NFI strategies.

The intended workflow is:

1. Plug in an NFI/Freqtrade strategy.
2. Screen and compare candidates much faster than Freqtrade.
3. Preserve Freqtrade trading semantics closely enough for exact parity checks.
4. Re-run only final candidates with official Freqtrade for maintainer-facing proof.

This is a research accelerator, not an immediate replacement for official Freqtrade validation.

## Why This Exists

Measured during Signal 65 work:

- Stops-only annual backtest: roughly 8–10 minutes.
- Normal routing 2022 backtest: roughly 1.5–2.5 hours.
- CPU behavior: effectively one saturated core; allowing more cores did not accelerate the hot loop.
- Memory: approximately 24–25 GiB for one normal-routing job.
- Two simultaneous jobs nearly exhausted 46 GiB RAM and started using swap.
- The bottleneck is the normal per-candle trade-management path:
  - position adjustment
  - rebuy
  - derisk
  - exit routing
  - repeated trade/order scans

Docker, data downloads, and orchestration are not the primary bottlenecks.

## Recommended Architecture

- Rust core:
  - chronological event loop
  - trade and order state
  - global slot allocation
  - stake and leverage
  - rebuy and position adjustment
  - fees, funding, liquidation, stops, and exits
- Python compatibility layer:
  - strategy loading
  - configuration
  - reports and experiment orchestration
  - fallback support for logic not yet compiled into the Rust core
- PyO3 + maturin:
  - Python/Rust integration
- Arrow/Polars:
  - columnar candle and indicator data
  - low-copy or zero-copy transfer where practical
- Optional Numba prototype:
  - validate hot-loop ideas before moving them into Rust

Avoid calling Python once per candle from Rust. That would preserve the current bottleneck. Entry, exit, and
protection expressions need a batched array interface or a small compilable IR/DSL.

## Safe Parallelism Boundary

Safe candidates for parallel execution:

- independent strategies
- independent years or timeranges
- indicator calculation by pair
- offline candidate scoring

Do not independently simulate each pair and merge the results. Global behavior depends on:

- `max_open_trades`
- slot competition
- chronological entry ordering
- shared wallet and stake
- position adjustment and rebuy timing

The portfolio event loop must preserve global chronological semantics.

## MVP Order

### Phase 0 — Measure

- Add a reproducible Freqtrade normal-routing benchmark fixture.
- Profile indicator calculation, callbacks, trade scans, and event simulation separately.
- Record time, peak RSS, trade count, and hardware information.

### Phase 1 — Parity Harness

- Normalize Freqtrade results into a deterministic trade surface.
- Compare:
  - pair and direction
  - open/close timestamps and rates
  - entry tags and exit reason
  - every entry/rebuy/exit order
  - stake, leverage, fees, and funding
  - profit and liquidation behavior
- Fail on the first semantic difference.

### Phase 2 — Fast Data/Indicator Layer

- Load candle data through Arrow/Polars.
- Calculate independent pair indicators in parallel.
- Cache immutable informative-timeframe alignment.
- Export contiguous arrays for the simulator.

### Phase 3 — Rust Event Simulator

- Implement the smallest chronological trade loop required by the benchmark fixture.
- Add routing features incrementally, gated by exact parity each time.
- Keep unsupported callbacks on an explicit slow fallback path.

### Phase 4 — Strategy Adapter

- Start with a constrained NFI strategy subset.
- Convert reusable boolean masks and thresholds into an IR.
- Make unsupported dynamic Python behavior visible instead of silently approximating it.

### Phase 5 — Research Runner

- Candidate registry and batch execution
- resumable jobs
- input/output SHA-256
- failure evidence
- deterministic reports
- Freqtrade confirmation command for finalists

## Correctness Rules

- Parity before speed claims.
- No approximating slot competition, rebuy, funding, fees, or liquidation.
- No lookahead or negative-shift leakage.
- Deterministic results across repeated runs.
- Never claim a speedup without fresh timing against the same fixture.
- Final maintainer evidence must still be reproduced by official Freqtrade.

## Initial Performance Target

Treat these as engineering targets, not promises:

- At least 10x faster candidate screening on the chosen normal-routing fixture.
- Peak memory materially below the current 24–25 GiB/job.
- Efficient use of multiple cores for indicators and independent jobs.
- Exact normalized trade-surface parity for the supported strategy subset.

## First Concrete Task

Create the repository skeleton and a benchmark/parity specification before implementing the simulator:

- `python/` — Python package and Freqtrade adapter
- `rust/` — Rust workspace and simulation core
- `benchmarks/` — frozen fixtures and timing scripts
- `tests/parity/` — exact trade-surface comparisons
- `docs/architecture.md` — event model and supported semantics

Then capture one small stops-only fixture and one normal-routing fixture from Freqtrade. Do not start with a full
NFI X7 rewrite.

## Current Signal 65 Work

The existing Signal 65 normal-routing baseline continues independently on the laptop. Do not stop, move, or reuse
its live evidence directory while creating this engine.

Relevant workspace:

`/home/turing/project/NFI_X7_Optimization_65_signal_work_07_16`

The new engine lives separately at:

`C:\Users\0\project\NFI_BacktestEngine`

No upstream PR, production activation, or strategy behavior change is authorized as part of this new project.
