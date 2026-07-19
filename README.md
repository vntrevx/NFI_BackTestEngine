# NFI Backtest Engine

A lightweight Rust/Python research backtester for NFI strategies, with an exact
comparison lane against official Freqtrade results.

The engine inspects the current computer, chooses safe CPU process counts from physical
cores and available memory, calculates independent pair vectors in worker processes,
and keeps shared wallet, slot, trade, and order events deterministic in Rust.

> **Release status:** `v0.1.0` is an alpha release. It has exact certificates for a
> source-pinned NFI X7 v17.4.413 subset. It does not claim exact support for every NFI
> revision, pair, route, protection, pair lock, or liquidation event. Unsupported
> behavior stops explicitly instead of falling back to an approximate result.

## What you can do

- inspect an NFI/Freqtrade strategy before spending time on data preparation;
- automatically tune process and memory limits for the current computer;
- fill and seal required candle coverage;
- prepare pair indicators in isolated worker processes with immutable caching;
- run supported strategy behavior without calling Python once per candle;
- checkpoint and resume long research runs;
- compare an engine result with a plain or zipped Freqtrade export at zero tolerance;
- batch independent strategies or timeranges within the detected hardware limits.

Official Freqtrade remains the final source of truth for a candidate.

## Requirements

- Python 3.12, 3.13, or 3.14;
- Windows x64, Linux x86_64/aarch64, or macOS Apple Silicon;
- an NFI/Freqtrade strategy file, Freqtrade config, candle-data directory, and timerange;
- Docker Desktop or Docker Engine when candle data must be filled or the official
  Freqtrade reference lane is requested.

Public market metadata does not require exchange API credentials. Keep private keys out
of configs committed to this repository.

## Install

### Release wheel

Download the wheel for your platform from
[the latest GitHub release](https://github.com/vntrevx/NFI_BackTestEngine/releases/latest):

| Platform | Wheel suffix |
| --- | --- |
| Windows x64 | `win_amd64.whl` |
| Linux x86_64 | `manylinux2014_x86_64.whl` |
| Linux aarch64 | `manylinux2014_aarch64.whl` |
| macOS Apple Silicon | `macosx_11_0_arm64.whl` |

Create a virtual environment and install the downloaded wheel.

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install `
  "$HOME\Downloads\nfi_backtest_engine-0.1.0-cp312-abi3-win_amd64.whl"
.\.venv\Scripts\nfi-bte.exe --version
```

Linux or macOS:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install ~/Downloads/nfi_backtest_engine-0.1.0-*.whl
.venv/bin/nfi-bte --version
```

The examples below use `nfi-bte`. Use `.\.venv\Scripts\nfi-bte.exe` on Windows or
`.venv/bin/nfi-bte` on Linux/macOS when the virtual environment is not activated.

PyPI publishing is not enabled yet, so `pip install nfi-backtest-engine` is not the
current installation path.

### Source checkout

A source checkout is useful for the included exact-parity fixtures and development:

```powershell
git clone https://github.com/vntrevx/NFI_BackTestEngine.git
cd NFI_BackTestEngine
uv sync --extra dev --frozen
uv run maturin develop --release --locked
uv run nfi-bte --version
```

When using a source checkout, prefix the remaining examples with `uv run`.

## 1. Check this computer

`doctor` is read-only. It checks Python, available memory, Docker, and the pinned
Freqtrade image:

```powershell
nfi-bte doctor --output .nfi/doctor.json
```

The backtest command creates a hardware profile automatically when one does not exist.
Run this explicitly only when you want to inspect the selected limits:

```powershell
nfi-bte system tune --output .nfi/execution-profile.json
nfi-bte system show .nfi/execution-profile.json
```

The profile reserves memory and normally one physical core for the host. Each child
process limits NumPy, Polars, Rayon, OpenMP, OpenBLAS, and MKL nesting to one thread so
process-level parallelism does not oversubscribe the CPU.

## 2. Inspect the strategy

Run static preflight before downloading or calculating data:

```powershell
nfi-bte strategy inspect `
  path\to\NostalgiaForInfinityX7.py `
  --class NostalgiaForInfinityX7 `
  --output artifacts\x7-strategy-analysis.json
```

This reports the selected class, required timeframes, vector methods, hot callbacks,
unsafe dynamic behavior, and exact source locations. A fatal diagnostic stops the run.

## 3. Prepare an NFI run

The minimum inputs are:

| Input | Meaning |
| --- | --- |
| strategy path | NFI/Freqtrade Python strategy file |
| `--class` | strategy class inside that file |
| `--config` | effective Freqtrade JSON config |
| `--datadir` | existing Freqtrade candle-data root |
| `--timerange` | `YYYYMMDD-YYYYMMDD` research interval |
| `--output-dir` | a new directory owned by this run |

Start with `--prepare-only`. It validates the strategy, detects hardware, freezes the
pairlist, seals data identity, and calculates pair vectors without claiming a simulated
trade result:

```powershell
nfi-bte backtest `
  path\to\NostalgiaForInfinityX7.py `
  --class NostalgiaForInfinityX7 `
  --config user_data\config.json `
  --datadir user_data\data\binance `
  --timerange 20250101-20260101 `
  --output-dir artifacts\x7-2025 `
  --prepare-only
```

Pairs come from the effective config whitelist. Repeat `--pair`, for example
`--pair BTC/USDT --pair ETH/USDT`, to freeze an explicit subset.

By default, missing candle coverage is filled through the pinned Freqtrade container.
Use `--no-download` when an offline run must fail instead of downloading data.

## 4. Run or resume the simulation

Reuse the prepared directory with `--resume` and omit `--prepare-only`:

```powershell
nfi-bte backtest `
  path\to\NostalgiaForInfinityX7.py `
  --class NostalgiaForInfinityX7 `
  --config user_data\config.json `
  --datadir user_data\data\binance `
  --timerange 20250101-20260101 `
  --output-dir artifacts\x7-2025 `
  --resume
```

The command has three meaningful outcomes:

| Status | Meaning |
| --- | --- |
| `prepared` | immutable data and vectors are ready; simulation was not requested |
| `complete` | the declared supported contract produced a deterministic result |
| `blocked_unsupported_semantics` | at least one active behavior has no exact lowering |

`blocked_unsupported_semantics` is a safety result, not a crash. The engine never
silently substitutes simplified trading behavior.

Useful output files include:

- `run.json` — final status and run identity;
- `execution-profile.json` — CPU and memory limits used;
- `strategy-analysis.json` and `hot-callback-ir.json` — compiled capability boundary;
- `data-seal.json` — candle files, coverage, sizes, and hashes;
- `simulation-result.json` and `trade-surface.json` — supported-run results;
- `checkpoints/` — hash-validated stages used by `--resume`.

## 5. Confirm against Freqtrade

Export the same candidate with official Freqtrade, then compare the plain JSON or ZIP
result:

```powershell
nfi-bte confirm `
  artifacts\x7-2025 `
  path\to\backtest-result.zip `
  --strategy NostalgiaForInfinityX7 `
  --output-dir artifacts\x7-2025-confirmation
```

The comparator normalizes both results and fails at the first exact semantic
difference. It does not use a floating-point tolerance.

## Verify the included exact fixture

This smoke test requires a source checkout because the frozen fixture is stored in the
repository:

```powershell
nfi-bte engine fixture `
  benchmarks\fixtures\captured\normal-routing-spot-2025-01-01_04\manifest.json `
  --output-dir artifacts\fixture-smoke `
  --level full
```

Expected result:

```text
engine fixture parity (full): trades=True, state=True
```

`quick` compares the final normalized trade surface. `full` additionally compares the
shared portfolio state after every Freqtrade-visible candle.

## Batch independent candidates

Batch jobs are separate strategies or timeranges. Concurrency is capped by the hardware
profile:

```powershell
nfi-bte batch examples\batch-v1.example.json `
  --output-dir artifacts\batch-01

nfi-bte runs list --limit 20
nfi-bte runs show RUN_ID
```

Use `--resume` to reuse only stages whose complete input identity still matches.

## Current exact-support boundary

Version 0.1.0 executes the source-pinned X7 v17.4.413 managed long routes,
short-rebuy tags 561–563, constrained isolated-futures accounting with uniform 3x
leverage, and the tag-120 spot/backtest grind state machine.

The sealed APE/USDT:USDT annual futures certificate covers 2022-04-01 through
2023-01-01 and matches the official final surface exactly: 11 trades, 164 orders,
142 adjustment orders, one short trade, and eight funded trades. Separate narrow spot
certificates cover APE top-coins, an APE rebuy exit, ZEC tag 120, and an APE/AAVE
equal-timestamp slot conflict.

These certificates do not prove arbitrary NFI X7 behavior. Unknown tags, tag 121,
unsupported mixed tags, non-uniform per-entry futures leverage, unsupported callbacks,
protections, pair locks, and liquidation paths remain fail-closed.

See:

- [X7 support and exact certificates](docs/x7-support.md)
- [Architecture and semantic ownership](docs/architecture.md)
- [Benchmark fixture specification](benchmarks/README.md)
- [Release boundary and publishing](docs/release.md)

## Repository map

```text
python/nfi_backtest_engine/   Python CLI, orchestration, strategy IR, parity, reports
rust/crates/nfi-sim-core/     deterministic chronological portfolio simulator
rust/crates/nfi-vector-io/    SHA-verified projected Feather reader
benchmarks/fixtures/          synthetic contracts and captured Freqtrade fixtures
benchmarks/evidence/          narrow, hash-sealed X7 certificates
tests/                        unit, integration, exact surface, and state-parity tests
docs/                         architecture, support boundary, and release procedure
```

The large captured state traces are intentional test fixtures. Generated profiles,
caches, run registries, build outputs, and user runs belong in `.nfi/`, `dist/`, or
`artifacts/`; those paths are ignored by Git.

## Development checks

```powershell
uv lock --check
uv run ruff check .
uv run basedpyright --level error python/nfi_backtest_engine
uv run pytest -q
```

Rust checks:

```bash
cd rust
cargo fmt --all -- --check
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before publishing
changes or reporting a vulnerability.
