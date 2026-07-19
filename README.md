# NFI Backtest Engine

A lightweight Rust/Python research backtester for NFI strategies, with an exact
comparison lane against official Freqtrade results.

The target is always the NFI file supplied today, not one permanently embedded
revision. A normal research run covers the previous five complete calendar years,
screens with the native engine, and confirms a finalist with official Freqtrade against
the same sealed inputs. See the [product contract](PROJECT_BRIEF.md).

The engine inspects the current computer, chooses safe CPU process counts from physical
cores and available memory, calculates independent pair vectors in worker processes,
and keeps shared wallet, slot, trade, and order events deterministic in Rust.

> **Release status:** `v0.4.0` is an alpha release. It has exact certificates for a
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
- batch independent strategies or timeranges within the detected hardware limits;
- keep native host resources separate from Docker daemon CPU and memory limits.

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

### One command

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/vntrevx/NFI_BackTestEngine/main/install.ps1 | iex
```

Linux x86_64/aarch64 or macOS Apple Silicon:

```bash
curl -LsSf https://raw.githubusercontent.com/vntrevx/NFI_BackTestEngine/main/install.sh | sh
```

The installer detects the platform, selects the latest release wheel, verifies its
GitHub-published SHA-256 digest, and installs `nfi-bte` into an isolated
[`uv tool`](https://docs.astral.sh/uv/guides/tools/) environment. It installs `uv`
through Astral's official installer only when it is missing.

To inspect an installer before running it:

```powershell
irm https://raw.githubusercontent.com/vntrevx/NFI_BackTestEngine/main/install.ps1 `
  -OutFile install.ps1
Get-Content .\install.ps1
.\install.ps1
```

### Manual release wheel

Download the matching wheel from
[the latest GitHub release](https://github.com/vntrevx/NFI_BackTestEngine/releases/latest)
and install it with `uv`:

```powershell
uv tool install --python 3.12 path\to\nfi_backtest_engine-*.whl
```

PyPI publishing is not enabled yet. Once trusted publishing is configured, the standard
short form will be `uv tool install nfi-backtest-engine`, with
`pipx install nfi-backtest-engine` retained as a familiar alternative. npm and bun are
not used because this is a Python/Rust native application; a Node wrapper would add a
second runtime without simplifying the platform package.

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

## Fastest path: one command

On the first run, pass only the strategy file:

```powershell
nfi-bte run path\to\NostalgiaForInfinityX7.py
```

The setup wizard automatically:

1. finds the single strategy class, or asks you to choose when the file has several;
2. detects `user_data/config.json`;
3. detects `user_data/data/<exchange>`;
4. reads pairs from the effective Freqtrade whitelist;
5. suggests the previous five complete calendar years;
6. creates a readable output name;
7. saves the choices in `.nfi/project.json`;
8. tunes CPU processes and memory for the current computer;
9. starts the checkpointed research backtest.

Only values that cannot be determined safely are requested. To accept every
unambiguous discovery and the five-year default without prompts:

```powershell
nfi-bte run path\to\NostalgiaForInfinityX7.py --yes
```

After the first setup, the complete command is:

```powershell
nfi-bte run
```

If the output directory already contains the same run identity, `run` automatically
resumes only hash-valid data and vector stages. It never deletes an existing directory
or silently reuses inputs whose identity changed.

### Prepare without simulation

Use this when you want the wizard, hardware tuning, data seal, and pair vectors but do
not yet want a simulated result:

```powershell
nfi-bte run path\to\NostalgiaForInfinityX7.py --prepare-only
```

Continue later with:

```powershell
nfi-bte run
```

### Configure without running

`init` saves the project but does not start a backtest:

```powershell
nfi-bte init path\to\NostalgiaForInfinityX7.py
```

Review `.nfi/project.json`, then run `nfi-bte run`. Reconfigure without deleting any
run data:

```powershell
nfi-bte init path\to\NostalgiaForInfinityX7.py --force
```

The saved project contains paths and execution choices only. It never copies API keys,
secrets, or the contents of the Freqtrade config.

### Explicit first-run options

Non-standard folders can still be provided once:

```powershell
nfi-bte run path\to\NostalgiaForInfinityX7.py `
  --class NostalgiaForInfinityX7 `
  --config user_data\config.json `
  --datadir user_data\data\binance `
  --timerange 20210101-20260101 `
  --output-dir artifacts\x7-2021-2025 `
  --pair BTC/USDT `
  --yes
```

These options become the saved project. Once it exists, use `init --force` to change
them instead of passing temporary overrides to `run`.

By default, missing candle coverage is filled through the pinned Freqtrade container.
Use `--no-download` when an offline run must fail instead. Use `--no-market-download`
with `--markets path\to\markets.json` for frozen offline market metadata.

## Check or inspect manually

`doctor` is read-only:

```powershell
nfi-bte doctor --output .nfi/doctor.json
```

The `run` command creates a hardware profile automatically. To inspect it explicitly:

```powershell
nfi-bte system tune --output .nfi/execution-profile.json
nfi-bte system show .nfi/execution-profile.json
```

Docker Desktop and Docker Engine have a separate resource boundary. Inspect the memory
and CPU values visible to the daemon, the automatically reserved headroom, and only
containers owned by this project:

```powershell
nfi-bte system docker
```

Managed Freqtrade containers run one at a time, receive a limit derived from daemon
memory after subtracting current usage by other containers, and are reclaimed by exact
container ID even when the command times out or is interrupted. Stopped managed
containers are removed before the next run. To request the same narrowly scoped cleanup
explicitly:

```powershell
nfi-bte system docker --cleanup-stopped
```

This never prunes unrelated containers. Existing running managed containers are reported
and block a second Docker workload instead of being killed.

The fast engine wheel runs natively on Apple Silicon. Exact official fixtures remain
pinned to their captured `linux/amd64` Freqtrade image, which Docker Desktop may emulate
on an arm64 Mac; the tool does not exchange reference platforms without new parity
evidence.

Static strategy inspection is also available separately:

```powershell
nfi-bte strategy inspect `
  path\to\NostalgiaForInfinityX7.py `
  --class NostalgiaForInfinityX7 `
  --output artifacts\x7-strategy-analysis.json
```

For a newly downloaded NFI revision, run the native-compatibility preflight before
preparing years of data:

```powershell
nfi-bte strategy check `
  path\to\NostalgiaForInfinityX7.py `
  --class NostalgiaForInfinityX7 `
  --trading-mode spot `
  --output artifacts\x7-compatibility.json
```

This is a source and callback-compiler check, not a profitability test. A structurally
supported upstream patch passes immediately. A new stateful contract is reported as
`EXACT_LOWERING_REVIEW_REQUIRED` before any long backtest starts.

The profile normally reserves a physical core and host memory. Child processes limit
NumPy, Polars, Rayon, OpenMP, OpenBLAS, and MKL nesting to one thread, preventing nested
library threads from multiplying the selected process count.

Native vector and Rust execution still use safe host parallelism. The default project
timerange is five complete years, while release-grade performance evidence requires at
least four years. The one-container rule
applies only to managed Docker workloads such as the pinned official Freqtrade reference
and missing-data downloads. The engine does not silently split a timerange: independent
chunks reset wallet, open-trade, protection, and strategy state and therefore cannot be
presented as one exact monolithic backtest.

## Run outcomes

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

The advanced `backtest` command remains available for scripts that deliberately pass
every path and option on each invocation.

## Confirm against Freqtrade

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

Version 0.4.0 executes the source-pinned X7 v17.4.413 managed long routes,
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

- [Product contract and long-horizon goal](PROJECT_BRIEF.md)
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
install.ps1 / install.sh      verified one-command release installers
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
