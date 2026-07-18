# NFI Backtest Engine

A lightweight Rust/Python research backtester with an official Freqtrade exact-parity
lane.

Phase 0 and Phase 1 are implemented. The repository can freeze official Freqtrade
fixtures, profile them, run the supported spot-long contract subset through one global
Rust event loop, and compare either the final trade surface or every candle state with
no numeric tolerance.

The full NFI X7 callback surface is not executable yet. X7 source preflight works, but
the engine deliberately fails before simulation when a strategy has not been compiled
into the supported IR. Official Freqtrade remains the final source of truth.

## Current status

| Area | Status | Exact boundary |
| --- | --- | --- |
| Hardware tuning | Implemented | Physical/logical CPU, available RAM, memory cap, safe job counts |
| Data preparation | Implemented | Missing ranges may be downloaded, then coverage and SHA-256 are sealed |
| Strategy preflight | Implemented | Iterative AST analysis, exact source diagnostics, no per-candle Python |
| Official reference | Implemented | Freqtrade 2026.5.1 Docker image pinned by platform digest, offline markets |
| Trade parity | Implemented | Versioned canonical surface, first semantic difference, zero tolerance |
| Full state parity | Implemented | Every visible candle, shared wallet/trades/orders/counters |
| Rust simulator | Contract subset | Spot-long stops, rebuy, timed/forced exits, fees, precision, shared slots |
| Full NFI X7 | Not implemented | Hot callbacks, shorts, futures, funding, liquidation, and protections remain gated |

## Install and inspect this computer

Windows uses Docker Desktop for the official reference and WSL for the Linux Rust core.

```powershell
uv sync --extra dev
uv run nfi-bte doctor
uv run nfi-bte system tune --output .nfi/execution-profile.json --memory-cap-gib 8
uv run nfi-bte system show .nfi/execution-profile.json
```

The generated profile reserves a physical core, caps working memory, and separates safe
parallelism for indicators, independent engine jobs, and memory-heavy official
Freqtrade jobs.

## Run the included exact-parity fixtures

`quick` compares the complete normalized trade result:

```powershell
uv run nfi-bte engine fixture `
  benchmarks/fixtures/captured/normal-routing-spot-2025-01-01_04/manifest.json `
  --output-dir benchmarks/work/normal-quick `
  --profile .nfi/execution-profile.json `
  --level quick
```

`full` additionally compares the shared portfolio state after every Freqtrade-visible
candle:

```powershell
uv run nfi-bte engine fixture `
  benchmarks/fixtures/captured/normal-routing-spot-2025-01-01_04/manifest.json `
  --output-dir benchmarks/work/normal-full `
  --profile .nfi/execution-profile.json `
  --level full
```

Each output contains the compiled simulation input, raw Rust result, Freqtrade-compatible
trade surface, hashes, resource measurements, and `run.json`. A failure also creates
`mismatch-replay/` with only the input prefix through the first mismatch and the exact
expected/actual fragments.

## Run a fresh official comparison

The performance command runs separate engine and official Freqtrade processes against
the same sealed manifest:

```powershell
uv run nfi-bte performance `
  benchmarks/fixtures/captured/normal-routing-spot-2025-01-01_04/manifest.json `
  --output-dir benchmarks/work/performance-normal `
  --profile .nfi/execution-profile.json `
  --level full
```

It records wall time, pipeline and core/container peak RSS, hardware identity, both
parity reports, and the observed speed ratio. A speed claim is automatically marked
`diagnostic-only` unless the fixture has at least 80 pairs and 365 days.

## Inspect an NFI strategy

```powershell
uv run nfi-bte strategy inspect path/to/NostalgiaForInfinityX7.py `
  --class NostalgiaForInfinityX7 `
  --output artifacts/x7-strategy-ir.json
```

The preflight finds classes, constants, required timeframes, vector methods, hot
callbacks, forbidden dynamic execution, unsafe shifts/rolling operations, and exact
source locations. `strategy prepare` creates a hash-bound bundle only after fatal
diagnostics are clear.

## Prepare immutable candle data

```powershell
uv run nfi-bte data prepare `
  --config user_data/config.json `
  --datadir user_data/data/binance `
  --timerange 20250101-20260101 `
  --timeframe 5m `
  --timeframe 1h `
  --output .nfi/data-seal.json

uv run nfi-bte data validate .nfi/data-seal.json
```

Existing data is never erased. Missing coverage is appended/prepended through the pinned
Freqtrade container, then every file and coverage boundary is sealed.

## Repository map

- `python/nfi_backtest_engine/` — orchestration, fixture/reference adapters, strategy IR,
  data seals, parity, diagnostics, and performance gates
- `rust/crates/nfi-sim-core/` — deterministic global chronological simulator
- `rust/crates/nfi-sim-cli/` — one-process JSON/JSONL execution boundary
- `rust/crates/nfi-py/` — reserved low-copy PyO3 package boundary
- `benchmarks/fixtures/captured/` — two official Freqtrade 2026.5.1 proof fixtures
- `benchmarks/reference/tracer/` — low-overhead official state/profiling instrumentation
- `tests/parity/` — exact surface and trace contracts
- `docs/architecture.md` — event order, ownership, and supported semantics

## Development verification

```powershell
uv run pytest -q
uv run ruff check .
wsl.exe -e bash -lc "cd /mnt/c/Users/0/project/NFI_BacktestEngine/rust && cargo fmt --all -- --check && cargo test --workspace --locked && cargo clippy --workspace --all-targets --locked -- -D warnings"
```

No upstream PR, live trading activation, or NFI strategy behavior change is part of this
repository.
