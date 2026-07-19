# NFI Backtest Engine

A lightweight Rust/Python research backtester with an official Freqtrade exact-parity
lane. Native wheels are built with PyO3/maturin for Linux x86_64/aarch64, Windows x64,
and macOS arm64.

Version 0.1.0 implements the benchmark, parity, vector, native simulator, strategy
adapter, and resumable research-runner foundations. It inspects the current computer,
chooses process counts from physical CPU and available memory, evaluates independent
pair vectors in isolated worker processes, and keeps the shared portfolio event loop
deterministic in Rust.

The source-pinned NFI X7 v17.4.413 adapter now executes both spot and a constrained
futures contract without per-candle Python. A sealed APE/USDT:USDT 2022 certificate
matches the official Freqtrade final trade surface exactly, including a short trade,
3x leverage, funding, and 142 position-adjustment orders. This is not a certificate
for arbitrary pairs, protections, pair locks, or liquidation events. Official
Freqtrade remains the final source of truth.

## Current status

| Area | Status | Exact boundary |
| --- | --- | --- |
| Hardware tuning | Implemented | Physical/logical CPU, current RAM, host reserve, process and nested-thread limits |
| Multiprocess execution | Implemented | Pair-vector workers and independent candidate jobs; shared portfolio loop stays ordered |
| Data preparation | Implemented | Timeframe startup requests, available coverage, shortfalls, files, and SHA-256 are sealed |
| Strategy preflight | Implemented | Iterative AST analysis, exact source diagnostics, no per-candle Python |
| X7 vector preparation | Implemented | Trusted isolated workers, informative alignment, immutable cache, direct sealed Feather input |
| Research runner | Implemented | Auto tuning/data coverage, frozen pairlist, checkpoints, resume |
| Official reference | Implemented | Freqtrade 2026.5.1 Docker image pinned by platform digest, offline markets |
| Trade parity | Implemented | Versioned canonical surface, first semantic difference, zero tolerance |
| Full state parity | Implemented | Every visible candle, shared wallet/trades/orders/counters |
| Rust simulator | Contract subset | Spot/futures, long/short, leverage, funding, precision, adjustments, partial exits, stops |
| X7 managed callbacks | Source-pinned | Long families plus short-rebuy tags 561-563 use compiled Rust policies |
| X7 legacy tag 120 | Partial exact | Full spot/backtest grind state machine is lowered; official ZEC proof reaches `gm0`, `gd1`, and `gd2` |
| X7 legacy tag 121 | Dormant / fail-closed | v17.4.413 has a route constant but no literal entry condition; any emitted 121 signal is rejected |
| Annual X7 futures proof | Final surface exact | APE/USDT:USDT, 2022-04-01 to 2023-01-01, 11 trades and 164 orders |
| Full arbitrary NFI X7 | Not certified | Other pairs/routes plus protections, locks, and liquidation events need differential proof |

The annual futures certificate reaches long and short trading, leverage, funding,
derisk levels 1-3, and grind levels 1-5. Its engine and official normalized surfaces
share SHA-256 `12386d5c...31bcd` with zero tolerance. Separate spot certificates prove
the APE top-coins path, a tag-62 rebuy exit, a ZEC tag-120 route through `gm0`, `gd1`,
and `gd2`, and one APE/AAVE equal-timestamp slot conflict. These are deliberately
narrow certificates; no result should be described as arbitrary or full X7 support.

The source-static entry inventory is sealed in
[`benchmarks/evidence/x7-v17.4.413-static-entry-inventory.json`](benchmarks/evidence/x7-v17.4.413-static-entry-inventory.json).
It records tag 120 as an active literal branch and tag 121 as a dormant route constant.
The narrow shared-slot certificate is recorded in
[`benchmarks/evidence/x7-ape-aave-shared-slot-v17.4.413.json`](benchmarks/evidence/x7-ape-aave-shared-slot-v17.4.413.json).
The annual futures certificate is recorded in
[`benchmarks/evidence/x7-ape-futures-2022-v17.4.413.json`](benchmarks/evidence/x7-ape-futures-2022-v17.4.413.json).

## Install

After PyPI trusted publishing has been enabled:

```text
pip install nfi-backtest-engine
nfi-bte --version
```

Every tagged version also publishes its wheel and source archive on GitHub Releases.

For a source checkout:

```text
uv sync --extra dev --frozen
uv run maturin develop --locked
```

## Install and inspect this computer

Windows uses Docker Desktop for the official reference and WSL for the Linux Rust core.

```powershell
uv sync --extra dev
uv run nfi-bte doctor
uv run nfi-bte system tune --output .nfi/execution-profile.json
uv run nfi-bte system show .nfi/execution-profile.json
```

The generated profile reserves host memory and normally one physical core, then
separates safe process counts for indicators, independent engine jobs, and memory-heavy
official Freqtrade jobs. Each spawned worker limits NumPy/BLAS/Polars/Rayon nesting to
one thread, avoiding `jobs * library threads` oversubscription. Indicator workers use a
conservative 3 GiB peak-memory assumption until the target strategy is measured.

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

## Prepare a checkpointed X7 research run

This reads the current computer, reuses or recalibrates the hardware profile, resolves
the effective Freqtrade config, freezes pair order, fills missing public candle coverage,
and calculates pair vectors in isolated worker processes:

```powershell
uv run nfi-bte backtest path/to/NostalgiaForInfinityX7.py `
  --class NostalgiaForInfinityX7 `
  --config user_data/config.json `
  --datadir user_data/data/binance `
  --timerange 20250101-20260101 `
  --output-dir artifacts/x7-2025 `
  --prepare-only
```

Resume only hash-valid completed stages:

```powershell
uv run nfi-bte backtest path/to/NostalgiaForInfinityX7.py `
  --class NostalgiaForInfinityX7 `
  --config user_data/config.json `
  --datadir user_data/data/binance `
  --timerange 20250101-20260101 `
  --output-dir artifacts/x7-2025 `
  --prepare-only `
  --resume
```

Omit `--prepare-only` and pass a frozen market snapshot with `--markets` to request
simulation. A run proceeds only when every active callback has an exact lowering and
every emitted signal tag belongs to a compiled route. The X7 adapter accepts its
source-pinned managed long routes and short-rebuy tags 561-563; unknown tags, unequal
per-entry futures leverage, or unsupported callbacks return
`blocked_unsupported_semantics`. The runner never falls back to per-candle Python.

Supported runs write a compact `simulation-input.manifest.json` that references the
sealed Feather vectors directly, plus `simulation-result.json` and `trade-surface.json`.
Confirm that result against a plain or zipped official Freqtrade export:

```powershell
uv run nfi-bte confirm artifacts/simple-run backtest-result.zip `
  --strategy MyStrategy `
  --output-dir artifacts/simple-confirmation
```

The command normalizes the official export and fails at the first exact semantic
difference.

Public market metadata can be captured without exchange credentials:

```powershell
uv run nfi-bte markets capture `
  --config user_data/config.json `
  --pair BTC/USDT `
  --output artifacts/binance-markets.json
```

If `--markets` is omitted for a supported callback-free run, the runner captures the
selected public markets automatically. Use `--no-market-download` for a fully offline,
fail-closed run.

## Batch candidates and inspect runs

Batch jobs are independent strategies or timeranges. The runner limits concurrent jobs
to the hardware profile and divides pair-indicator workers between them:

```powershell
uv run nfi-bte batch examples/batch-v1.example.json `
  --output-dir artifacts/batch-01
```

Every checkpointed run is indexed in a SQLite WAL registry:

```powershell
uv run nfi-bte runs list --limit 20
uv run nfi-bte runs show RUN_ID
```

The batch manifest schema and all paths are deterministic; relative paths resolve from
the manifest directory. Use `--resume` to reuse only hash-valid data and vector stages.

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
- `rust/crates/nfi-py/` — low-copy PyO3 package boundary
- `rust/crates/nfi-vector-io/` — SHA-verified projected Feather reader
- `benchmarks/fixtures/captured/` — two official Freqtrade 2026.5.1 proof fixtures
- `benchmarks/evidence/` — narrow, hash-sealed NFI X7 differential certificates
- `benchmarks/reference/tracer/` — low-overhead official state/profiling instrumentation
- `tests/parity/` — exact surface and trace contracts
- `docs/architecture.md` — event order, ownership, and supported semantics

## Development verification

```powershell
uv run pytest -q
uv run ruff check .
uv run basedpyright --level error python/nfi_backtest_engine
wsl.exe -e bash -lc "cd /mnt/c/Users/0/project/NFI_BacktestEngine/rust && cargo fmt --all -- --check && cargo test --workspace --locked && cargo clippy --workspace --all-targets --locked -- -D warnings"
```

Release instructions and the exact readiness boundary are in
[`docs/release.md`](docs/release.md).
The current X7 callback boundary and the path to a full certificate are in
[`docs/x7-support.md`](docs/x7-support.md).

No upstream PR, live trading activation, or NFI strategy behavior change is part of this
repository.
