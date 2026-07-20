# NFI Backtest Engine

A lightweight Rust/Python research backtester for NFI strategies, with an exact
comparison lane against official Freqtrade results.

The target is always the NFI file supplied today, not one permanently embedded
revision. A normal research run covers the previous five complete calendar years,
screens with the native engine, and confirms a finalist with official Freqtrade against
the same sealed inputs. See the [product contract](PROJECT_BRIEF.md).

The engine inspects the current computer, measures one full-range worst-footprint pair,
then admits worker processes from that observed peak, current free memory, CPU affinity,
and any explicit user cap. Shared wallet, slot, trade, and order events remain
deterministic in Rust.

> **Release status:** `v0.6.0` is an alpha release. The engine accepts the NFI file
> supplied to each run and has exact regression evidence through X7 v17.4.418. It does
> not claim exact support for every future NFI revision or every branch. New behavior
> that cannot be compiled exactly stops explicitly instead of falling back to an
> approximate result.

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

GitHub Releases is the supported distribution channel. PyPI, npm, and bun are not used:
this is a Python/Rust native application, and a second registry or runtime would not
simplify the platform package.

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

The hardware profile contains facts and explicit caps only. The first uncached run
executes one real full-timerange pair in an isolated process, keeps that useful vector,
and stores the measured peak under `.nfi/calibrations/`. Later runs reuse the peak only
when strategy, config, data, timerange, dependency, and hardware identities still
match; worker admission is recalculated from free memory every time. Use
`nfi-bte run --recalibrate` after an intentional environment change.

The Rust engine keeps Feather-derived rows in a disk-backed spool. Each pair retains
only one bounded read window with the callback lookback overlapped, so sequential
round-robin access avoids per-candle filesystem calls and boundary re-reads without
loading a multi-year vector into heap memory. The
OS-local temporary directory is normally fastest. If that directory is RAM-backed,
select an existing disk-backed directory explicitly:

```powershell
nfi-bte system tune --force `
  --spool-directory D:\nfi-spool `
  --output .nfi\execution-profile.json
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

The profile does not reserve a guessed percentage or assume a fixed GiB cost. It caps
CPU processes at the physical/logical/affinity limit, while workload calibration
derives the actual worker count from an OS-native peak measurement. Child processes
limit NumPy, Polars, Rayon, OpenMP, OpenBLAS, and MKL nesting to one thread, preventing
nested library threads from multiplying the selected process count.

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

- `run.json` — final status, run identity, and end-to-end stage timings;
- `execution-profile.json` — factual CPU limits and explicit memory cap;
- `engine-profile.json` — measured hash/decode/event-loop/serialization phases;
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

For a completed research run, the reference lane can materialize the sealed strategy
and effective config, capture the pinned raw market snapshot once, run official
Freqtrade offline, and compare the result in one command:

```powershell
nfi-bte reference research artifacts\x7-2025 `
  --output-dir artifacts\x7-2025-official
```

To make the entire operation offline, reuse a previously captured raw reference market
snapshot:

```powershell
nfi-bte reference research artifacts\x7-2025 `
  --output-dir artifacts\x7-2025-official `
  --markets artifacts\reference-markets.json `
  --no-market-capture
```

The completed research directory contains private copies of the exact strategy and
sanitized effective config under `sealed-inputs/`. Their hashes are checked before the
official run, so a daily NFI update cannot silently change an older result.

`run.json` records `pipeline_evidence.cold=true` only when neither data nor vector
checkpoints were resumed and no vector came from the content cache. Public performance
certification is stricter than parity alone:

```powershell
nfi-bte performance path\to\manifest.json `
  --output-dir artifacts\performance-cold `
  --runs 3
```

`release_certified=true` requires a representative sealed fixture (at least 80 pairs
and 1,460 days), exact parity on every run, at least 10x median screening speed, and a
passing memory gate. Short fixtures can complete as diagnostics but can never set that
release flag.

The current large native diagnostic uses the supplied X7 v17.4.418 source, 80 configured
spot pairs, five timeframes, and `20210101-20260101`. On one WSL2 host, the same sealed
21,102,441-row manifest produced byte-identical results before and after the hot-loop
changes. Native process time fell from 2,022.07 seconds to 763.70 seconds (2.65x), while
the chronological event loop fell from 1,638.36 seconds to 474.86 seconds (3.45x);
optimized peak RSS was 100,286,464 bytes. This is deliberately labeled diagnostic:
the run reused vector checkpoints, used `history-coverage=available` with 275 recorded
coverage shortfalls, and has one observation on one host. See
[`benchmarks/evidence/x7-80pair-spot-5y-native-2026-07-20.json`](benchmarks/evidence/x7-80pair-spot-5y-native-2026-07-20.json).
It is not an official Freqtrade, cold-pipeline, repeated-median, or cross-platform speed
certificate.

The pinned official lane could not complete that continuous five-year workload inside
Docker's enforced 21.85 GB limit; it was OOM-killed before producing a result. A
strictly sequential, bounded `20250701-20260101` verification did complete over the
same 80-pair universe. Its 167 trades and 402 orders match the native normalized
surface byte-for-byte at zero tolerance, including final balance, rejected signals,
order IDs, tags, and export order. The official container took 253.09 seconds and
peaked at 9,450,651,648 bytes; the native core starting from sealed vectors took
58.93 seconds and peaked at 88,928,256 bytes. The observed 4.29x wall and 106.27x
memory ratios are diagnostic execution comparisons, not cold end-to-end or
cross-platform certificates. See
[`benchmarks/evidence/x7-80pair-spot-2025h2-parity-2026-07-20.json`](benchmarks/evidence/x7-80pair-spot-2025h2-parity-2026-07-20.json)
and
[`benchmarks/evidence/x7-80pair-spot-5y-official-oom-2026-07-20.json`](benchmarks/evidence/x7-80pair-spot-5y-official-oom-2026-07-20.json).
Independent bounded runs are not concatenated into a continuous five-year result
because wallet, open-trade, and protection state reset at each boundary.

Release certification keeps the large performance workload at final-surface parity and
uses smaller branch-reaching fixtures for every-candle state parity. This avoids
creating a multi-year, multi-pair JSON trace merely to prove the same hot-loop speed:

```powershell
nfi-bte certify path\to\representative-manifest.json `
  --state-probe benchmarks\fixtures\captured\normal-routing-spot-2025-01-01_04\manifest.json `
  --state-probe benchmarks\fixtures\captured\stops-only-spot-2025-01-01_04\manifest.json `
  --output-dir artifacts\release-certificate `
  --runs 5
```

The bundle is certified only when the repeated representative gate and every full-state
probe pass. Median wall time and maximum memory are retained with immutable hashes.

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

Version 0.6.0 compiles the supplied X7 source rather than selecting a whole-file version.
The current native contracts cover the managed long routes, short-rebuy tags 561-563,
tag-dependent leverage, Binance isolated-futures liquidation inputs, the tag-120 legacy
grind route, tag-121 regular-mode adjustment followed by legacy grind, and the static
Freqtrade protections `CooldownPeriod`, `StoplossGuard`, `MaxDrawdown`, and
`LowProfitPairs` with deterministic pair locks.

The latest sealed APE/USDT:USDT annual futures evidence uses X7 v17.4.418 from
2022-04-01 through 2023-01-01 and matches official Freqtrade 2026.5.1 exactly:
11 trades, 164 orders, 142 adjustment orders, one short trade, and eight funded trades.
It also protects the official stop-loss-before-liquidation collision behavior that
previously exposed a divergence. The older v17.4.413 fixtures remain useful independent
regressions for APE top-coins, an APE rebuy exit, ZEC tag 120, and an APE/AAVE
equal-timestamp slot conflict.

The broadest current spot proof is the bounded 80-pair
`20250701-20260101` differential: 167 trades and 402 orders are byte-identical to
Freqtrade at zero tolerance. It reaches a much wider real portfolio surface than the
small route fixtures, but it still does not certify routes absent from that interval,
enabled lock generation, continuous five-year official execution, or arbitrary future
X7 source changes.

Implementation is not the same as branch-reaching certification. The v17.4.418 annual
run has no liquidation exit, no enabled protections or generated pair lock, and its tag
121 entry switch is disabled. Those paths have focused native tests but still require
small official full-state fixtures before they become public exact-parity claims.
Unknown tags, unsupported mixed tags, dynamic protections, unsupported exchanges or
margin modes, and new stateful callback shapes remain fail-closed.

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
cd rust
cargo fmt --all -- --check
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
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
