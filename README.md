**How to use:** [English](docs/usage.md) | [한국어](docs/usage-ko.md) | [Ελληνικά](docs/usage-el.md) | [Türkçe](docs/usage-tr.md)

# NFI Backtest Engine

**Backtest years of NFI in minutes, then prove the result against Freqtrade.**

[![Release](https://img.shields.io/github/v/release/vntrevx/NFI_BackTestEngine?display_name=tag&sort=semver)](https://github.com/vntrevx/NFI_BackTestEngine/releases/latest) [![CI](https://github.com/vntrevx/NFI_BackTestEngine/actions/workflows/ci.yml/badge.svg?event=pull_request)](https://github.com/vntrevx/NFI_BackTestEngine/actions/workflows/ci.yml) [![Python](https://img.shields.io/badge/Python-3.12--3.14-3776AB?logo=python&logoColor=white)](pyproject.toml) [![Rust](https://img.shields.io/badge/Rust-1.83%2B-000000?logo=rust&logoColor=white)](rust/Cargo.toml) [![License](https://img.shields.io/github/license/vntrevx/NFI_BackTestEngine)](LICENSE)

NFI Backtest Engine is a native Rust/Python research backtester for
[NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity).
It runs supported NFI workloads in parallel and compares the final trade and state
surface with official Freqtrade at zero tolerance.

The engine compiles the strategy file and inputs supplied at runtime. It does not
hard-code a pair list, timerange, strategy hash, or expected result. Unknown active
semantics stop with a clear fail-closed verdict instead of being approximated. Python
parses and compiles the supplied strategy; the supported Native runtime does not
import or execute that strategy Python.

## Release status

| Scope | Status |
| --- | --- |
| Latest public release | [v1.9.1](https://github.com/vntrevx/NFI_BackTestEngine/releases/tag/v1.9.1) |
| Five-year Spot | Certified independently by v1.0.0 |
| Five-year Futures | Certified independently by v1.1.0 |
| Current `main` | v1.9.1 stable release line; Linux/macOS verified |

The Spot and Futures certificates remain valid for their own sealed strategy,
configuration, data, wheel, and host. They are not a same-candidate Spot-versus-Futures
benchmark, and they must not be combined into a newer full-certification claim.

v1.9.1 preserves current-source compatibility for X7 v17.4.587 at upstream commit
`95b76043c3f610e0760e191deebd12304bfadbf8` and the compact Freqtrade-style
`report.md` introduced in v1.9.0. It fixes reuse of completed research evidence after
a presentation-only package update: when every strategy, config, pair, data, runtime,
and semantic pipeline identity is unchanged, `nfi-bte run` now validates and returns
the completed run instead of reporting an unexplained resume-identity mismatch.

The v1.8.4 first-run market contract remains unchanged: BTC is the quick default;
users can type `1`, `10`, `20`, `40`, `80`, `100`, `all`, or `custom`; numeric
portfolios execute NFI's current Binance volume and filter policy in pinned Freqtrade,
then freeze the ranked result in the saved project. Large selections show the measured
long-run memory warning before data preparation. This presentation release does not
claim a new continuous performance certificate.

Current `main` tracks the v1.9.1 stable release and is usable from source for supported
X7 workloads on Linux, macOS, and Windows through WSL2. Its public wheels and sdist
complete exact Spot and Futures trade/state regression checks, and the release commit
must pass same-commit Required CI on Linux and macOS. v1.9.1 publication remains
checksum-sealed and promotes the release-candidate assets byte-for-byte to stable.

## Certified performance

| Mode | Official Freqtrade | Full/cold native | Preserved-vector reuse | Certified speedup |
| --- | ---: | ---: | ---: | ---: |
| Spot v1.0.0 | 61 h 34 m 21.9 s | 26 m 24.1 s median, 5 runs | Not measured | **139.927×** |
| Futures v1.1.0 | 1 h 47 m 10.9 s | 13 m 27.0 s, 1 cold seed | 8 m 49.4 s median, 3 runs | **12.148× reuse** · **7.969× cold** |

Each speedup compares runs inside one certificate only:

- Spot: `221,661.915 s / 1,584.127 s = 139.927×`
- Futures reuse: `6,430.897 s / 529.381 s = 12.148×`
- Futures cold: `6,430.897 s / 807.005 s = 7.969×`

### Why the Spot and Futures times differ

Five years describes calendar coverage, not a fixed amount of simulation work. The
sealed Oracle logs show that the gap did not come from loading or calculating the
roughly 42.1 million indicator rows:

| Official Freqtrade phase | Spot | Futures | Spot / Futures |
| --- | ---: | ---: | ---: |
| Indicator calculation | 16 m 20 s | 11 m 58 s | 1.36× |
| Stateful simulation after indicators | 61 h 11 m 14 s | 1 h 28 m 29 s | **41.49×** |
| Total Oracle wall time | 61 h 34 m 21.9 s | 1 h 47 m 10.9 s | **34.47×** |

The stateful workload was radically different:

| Sealed workload fact | Spot | Futures | Spot / Futures |
| --- | ---: | ---: | ---: |
| `max_open_trades` | 6 | 1 | 6× limit |
| Average open positions across five years | 4.490 | 0.882 | 5.09× |
| Open-position 5m intervals | 2,361,434 | 463,826 | 5.09× |
| Tracer rows loaded for callbacks | 6,179,752 | 1,279,689 | 4.83× |
| Trades / filled orders | 927 / 11,783 | 174 / 795 | 5.33× / 14.82× |
| Maximum orders in one trade | 2,108 | 167 | 12.62× |
| Container CPU time | 222,441 s | 7,337 s | **30.32×** |

Official Freqtrade invokes NFI's `custom_exit` for every open trade and five-minute
step. That callback builds a snapshot by scanning the trade's filled-order history.
Spot therefore had about five times as many open-trade callback opportunities, while
several long-lived grind trades accumulated hundreds or thousands of orders. The
cumulative order-history exposure was 70.42× larger than Futures. This repeated
Python callback and order-list work is the dominant explanation for the Oracle gap; the nearly equal
indicator volume and 1.43× spool-write difference rule out data size or disk I/O as
the primary cause.

The Native cold pipelines differed by only 1.97× because the lowered Rust state loop
does not pay the same Python callback overhead. The historical benchmark environments
differed in X7 revision and host configuration, including Windows/Docker Desktop versus
native Linux.

Both certificates still prove exact parity for their sealed workloads. They do not
form a controlled Spot-versus-Futures performance test, so this project makes no
cross-mode speed ratio claim.

The v1.6.0 Full Native path completed a latest-X7 Spot development
qualification over 80 pairs and 2021-01-01 through 2026-01-01. Three fresh
processes finished in 63:17, 62:51, and 62:44 with byte-identical 1,162-trade
results (0.88% wall spread). Median peak RSS was 38.9 GiB. The 38.47 GB temporary
row spool remained below its pre-admitted bound and was reclaimed after every
run. This is a single-host performance/storage certificate, not an official
Freqtrade five-year parity or cross-platform performance claim. See the
[M22 performance evidence](benchmarks/evidence/m22/full-native-performance-storage.json).

For a fresh-run expectation, compare Spot's full native median with Futures' cold seed.
Use the Futures reuse number only after its content-addressed vectors already exist.
Actual runtime still depends on strategy behavior, data, hardware, and memory limits.

Evidence:

- [v1.6.0 release and notes](https://github.com/vntrevx/NFI_BackTestEngine/releases/tag/v1.6.0)
- [v1.6.0 product checksums](https://github.com/vntrevx/NFI_BackTestEngine/releases/download/v1.6.0/SHA256SUMS.txt)
- [Spot v1.0.0 release](https://github.com/vntrevx/NFI_BackTestEngine/releases/tag/v1.0.0)
- [Futures v1.1.0 release](https://github.com/vntrevx/NFI_BackTestEngine/releases/tag/v1.1.0)
- [Futures certificate](https://github.com/vntrevx/NFI_BackTestEngine/releases/download/v1.1.0/full-x7-futures-certification.json)
- [Futures evidence bundle](https://github.com/vntrevx/NFI_BackTestEngine/releases/download/v1.1.0/full-x7-futures-certification-evidence.zip)

## How it works

1. Inspect the supplied strategy, config, market data, and machine.
2. Run the deterministic native engine when active behavior can be lowered exactly.
3. If Native safely blocks, optionally run pinned official Freqtrade without modifying
   the Native run.
4. Confirm a Native result against an independent official result.
5. Report exact parity or the first exact difference.

The native lane is for fast research. The official lane is independent and slower by
design; it is the final semantic authority.

## Full Native algorithm map

| Stage | Semantic source | Native algorithm | Exactness and failure boundary |
| --- | --- | --- | --- |
| Raw market data | Freqtrade OHLCV preparation | SHA-bound Arrow decode, stable duplicate reduction, anchored resampling, gap filling, and timerange/startup slicing | Rejects invalid schemas, timestamps, identities, and missing required frames before simulation |
| Informative frames | NFI calls using Freqtrade merge rules | Explicit `(pair, timeframe)` catalog with causal visibility, suffixing, `ffill`, calendar boundaries, and no-lookahead alignment | Pinned Freqtrade merge fixtures are column-exact; unsupported frame operations fail closed |
| Indicators | NFI expressions, TA-Lib, qtpylib, NumPy, and pandas helpers | `IndicatorProgram` DAG with bounded streaming TA-Lib/native kernels, lookback planning, constant folding, and live-column projection | Arrow null, IEEE NaN, infinity, and signed zero remain distinct where the source distinguishes them |
| Entry and exit signals | NFI source-ordered dataframe mutations | Typed `SignalProgram` masks and assignments using Freqtrade's exact numeric-`1`/Boolean-`true` gate | Missing bodies, unknown operations, invalid types, or different signal surfaces stop Native |
| Tags | NFI tag literals, append order, and route keys | `TagProgram` string interning and masked source-order append, preserving compound tags and whitespace | Tag values are data, never Signal-number branches; raw and adopted tag surfaces must match exactly |
| Grind and callbacks | NFI adjustment, Derisk, Buyback, rebuy, stop, and managed-exit logic | Generic scalar/callback IR plus bounded state-machine programs over trade, order, wallet, and custom state | A new behavior is Native only when representable by reviewed opcodes and full-state parity evidence |
| Orders and portfolio | Freqtrade callback order, fill rules, wallet, stake, fee, precision, slots, protections, and pair locks | One deterministic timestamp-ordered Rust event loop; pair-local preparation may run in parallel | Shared wallet and order mutation never runs in parallel; equal-timestamp ordering is stable |
| Futures | Freqtrade and exchange leverage, funding, mark price, and liquidation contracts | Exact sparse funding/mark joins, leverage and liquidation state, side-aware PnL, and recalculation after fills | Funding is never forward-filled; invalid or incomplete economics fail before a result is claimed |
| Result proof | Official Freqtrade export and captured full state | Canonical trade surface plus every-candle state projection and stream hash | Zero tolerance: Native promotion requires both trade-surface and full-state equality |

Official and Native traces retain complete materialized records at their own event
granularities; their raw event counts and schemas are not a one-to-one parity claim.
Zero-tolerance comparison is defined on the common every-candle projection.

The Rust core keeps these responsibilities in separate vector, simulation, portfolio,
Futures, NFI state-machine, protection, I/O, profiling, and result modules. Optimization
is driven by IR structure and measured profiles, never by strategy version, pair,
timerange, SHA, Signal number, or expected output.

## Install

Installation targets advanced users who are comfortable with the shell and, for
data downloads or Freqtrade confirmation, with Docker and basic Ubuntu/Linux
commands.

Supported hosts are Linux and macOS. On Windows, install a WSL2 Linux distribution,
open its Linux shell, and use the Linux installer below: WSL2 runs the Linux build and
ABI. Native Windows and PowerShell are unsupported; product execution fails closed
with `native Windows is unsupported; run nfi-bte under WSL2 (Linux)`.

Linux x86_64/aarch64 (including WSL2) or macOS Apple Silicon:

```bash
curl -LsSf https://raw.githubusercontent.com/vntrevx/NFI_BackTestEngine/main/install.sh | sh
```

The installer downloads the matching supported wheel from the latest public GitHub
release, checks its published SHA-256 digest, and installs `nfi-bte` in an isolated
`uv` environment.

Close and reopen your terminal (or open a new shell) after installation so the
`nfi-bte` command is picked up on `PATH`, then verify:

```text
nfi-bte --version
nfi-bte doctor
```

The latest public installer currently returns `nfi-bte 1.9.1`.

### Keep the CLI updated

Update an installed CLI to the latest public release with one command:

```text
nfi-bte update
```

Successful commands check GitHub Releases at most once every 24 hours. When a newer release is
available, the CLI prints one line to stderr without changing the command result:

```text
Update available: 1.9.0 -> 1.9.1. Run `nfi-bte update`.
```

The updater reuses the active `uv tool`, `pipx`, or Python environment. Source
checkouts remain developer-managed and must be updated through Git and `uv sync`.
Set `NFI_BTE_DISABLE_UPDATE_CHECK=1` to disable the automatic version check.

## Quick start

Start from an empty working directory:

```bash
mkdir -p ~/nfi-backtest
cd ~/nfi-backtest
git clone --depth 1 https://github.com/iterativv/NostalgiaForInfinity.git
cd NostalgiaForInfinity
nfi-bte run NostalgiaForInfinityX7.py
```

The first-run wizard explains that Enter accepts each recommended value. It creates a
credential-free dry-run config without modifying NFI's modular files and asks for a
market count: `1`, `10`, `20`, `40`, `80`, `100`, `all`, or `custom`. The quick
default is BTC. Numeric portfolios use NFI's current Binance volume/filter policy and
freeze the resulting order in the saved project; large selections print a resource
warning. Managed candle storage and the most recent seven complete days remain the
defaults. During the run it shows the current stage, percentage, elapsed time, and
results folder. Completion prints a compact ASCII result and the `report.md` location.

For a non-interactive 80-market Spot setup with an explicit bounded period:

```bash
nfi-bte run NostalgiaForInfinityX7.py \
  --trading-mode spot \
  --pair-count 80 \
  --timerange 20260101-20260108 \
  --yes
```

Resume the saved project after its first run:

```bash
nfi-bte run
```

Use explicit inputs when discovery is not appropriate:

```bash
nfi-bte run path/to/NostalgiaForInfinityX7.py \
  --class NostalgiaForInfinityX7 \
  --config user_data/config.json \
  --datadir user_data/data/binance \
  --timerange 20210101-20260101 \
  --output-dir artifacts/x7-research \
  --yes
```

Missing public candles are downloaded through the pinned Freqtrade container by
default. Add `--no-download` for an offline, fail-if-missing run.

### Check a newly downloaded NFI version

A new NFI version string or source SHA does not by itself disable Native execution.
The engine structurally compiles the supplied file; it does not maintain a
strategy-version allowlist. Before spending time on a long run, check Spot and Futures
separately because their active callbacks and state routes differ:

```bash
nfi-bte strategy check path/to/NostalgiaForInfinityX7.py \
  --class NostalgiaForInfinityX7 \
  --trading-mode spot \
  --output compatibility-spot.json

nfi-bte strategy check path/to/NostalgiaForInfinityX7.py \
  --class NostalgiaForInfinityX7 \
  --trading-mode futures \
  --output compatibility-futures.json
```

For durable automation evidence, also supply `--upstream-repository`,
`--upstream-commit`, `--strategy-version`, and `--verification-ledger`. These fields
record the checked identity; they never grant compatibility.

Exit code 0 with `native_compatible=true` means every active callback required by that
mode compiled into the supported Native contracts. It is permission to attempt the
Native workload, not an Official parity certificate. Exit code 1 with
`native_compatible=false` identifies the blocking source construct; do not bypass it.
Use `--fallback disabled` when an unattended job must either remain Native or stop:

```bash
nfi-bte run path/to/NostalgiaForInfinityX7.py \
  --fallback disabled \
  --yes
```

For a material result, request the pinned Official Freqtrade comparison after Native
completion:

```bash
nfi-bte run path/to/NostalgiaForInfinityX7.py --verify
```

The release status above names the newest source with preserved zero-tolerance
Official/Native evidence. A later source may already pass structural checks and bounded
Native smokes without yet inheriting that evidence claim. Unknown semantics stop before
simulation instead of silently using behavior from an older NFI version.

For a newer NFI revision whose active callbacks are not yet supported by Native, run
the exact sealed workload through official Freqtrade:

```bash
nfi-bte run path/to/NostalgiaForInfinityX7.py --fallback official
```

The default `--fallback ask` requests consent only in an interactive terminal;
`--yes` does not grant fallback consent. An official-only result is kept separately
and is not labeled as Native parity. See
[Future NFI Compatibility](docs/future-nfi-compatibility.md) for the compiler,
upstream monitoring, and promotion contracts.

The scheduled watcher checks NFI, engine, pinned Freqtrade, and semantic-profile
identities every four hours. Spot and Futures run independently, then an atomic hosted
canary verifies both result sets and their fail-closed automation decisions before the
ledger identity can advance. Changed branches are promoted only after independent
official/Native trade-surface and full-state equality. Missing exact coverage enters a
bounded Spot or Futures discovery lane; blocked generic lowering updates one reconciled
compatibility Issue while execution stays official-only. Only an exact, size-bounded
discovery hit may open a Draft fixture PR, and it is never auto-approved or auto-merged.

For supported X7 stateful callbacks, `run.json` records the
`x7-generic-stateful` Native lane and every source-compiled primary program. The
Rust vector core can now pass typed pair output directly into the chronological simulator;
independent pair preparation is parallel, while wallet and order mutation remains one stable
timestamp stream. SHA-verified Feather remains the durable evidence/replay path, and both transports
must produce the same trade surface and every-candle state. Current manager payloads execute generic
programs without a handwritten X7 shadow; a missing or retired execution mode blocks Native before
simulation and leaves the visible official fallback available. Historical schema readers remain
available for sealed evidence replay. See
[Native In-Memory Vector Transport](docs/native-in-memory-vector.md).

## Confirm with official Freqtrade

Run the pinned official reference from a completed native result:

```bash
nfi-bte reference research artifacts/x7-research \
  --output-dir artifacts/x7-research-official
```

Or compare an existing Freqtrade export:

```bash
nfi-bte confirm \
  artifacts/x7-research \
  path/to/backtest-result.zip \
  --strategy NostalgiaForInfinityX7 \
  --output-dir artifacts/x7-confirmation
```

The comparison has no floating-point tolerance. It never concatenates independent
timerange chunks into one claimed result because chunk boundaries reset wallet,
open-trade, protection, and strategy state.

## Core guarantees

- Official Freqtrade is the final oracle.
- Unknown active semantics fail closed.
- Official fallback results remain distinct from Native parity evidence.
- Pair lists, dates, strategy hashes, and expected outputs come from sealed inputs,
  never runtime special cases.
- Hardware calibration caps workers by visible CPUs and measured memory.
- Shared wallet, order, trade, protection, and pair-lock state remains chronological
  and deterministic.
- Resume reuses only size- and SHA-verified checkpoints.
- Original evidence is not silently overwritten or recomputed.

## Outputs

Every run is an ordinary hash-linked directory:

| Path | Purpose |
| --- | --- |
| `run.json` | Run identity, status, timings, and evidence index |
| `simulation-result.json` | Deterministic native result |
| `trade-surface.json` | Exact-parity authority |
| `summary.json` | Compact research summary |
| `trades.csv` | Full spreadsheet-ready trade export |
| `orders.csv` | Normalized order and position-change export |
| `equity.csv` | Closed-trade equity series |
| `report.md` | Compact Freqtrade-style report with portable ASCII tables |
| `verification.json` | Official verification status and bound identities |
| `evidence/index.json` | Hash-indexed result artifact inventory |
| `selected-result.json` | Immutable Native or official-only result selection |
| `official-fallback/attempt-N/` | Preserved official fallback attempts |
| `checkpoints/` | Hash-validated resumable stages |

Regenerate presentation files without rerunning the simulation:

```bash
nfi-bte report artifacts/x7-research
nfi-bte report artifacts/x7-research --full-report
```

## Storage

New wizard-created runs live under `.nfi/runs`; rerunning the same saved project
resumes that stable directory. The reusable cache has a disk-aware ceiling and can be
set explicitly with `NFI_BTE_CACHE_MAX_BYTES`.

Inspect reclaimable data without changing anything:

```text
nfi-bte clean --dry-run
```

`nfi-bte clean --apply` creates a fresh audit before deletion and writes a durable
receipt. It removes only regenerable cache, failed/incomplete runs, temporary spool,
and rebuildable data by default. Completed runs require `--include-completed`;
preserve markers, certificates, evidence bundles, Oracle data, ZIP archives, active
runs, and unclassified files remain protected. See [managed storage and safe
cleanup](docs/clean.md).

## Key commands

| Command | Purpose |
| --- | --- |
| `nfi-bte run` | Run or resume native research |
| `nfi-bte update` | Update the installed CLI to the latest release |
| `nfi-bte strategy check ...` | Check a newly downloaded NFI revision |
| `nfi-bte doctor` | Inspect the current machine |
| `nfi-bte reference research ...` | Run official Freqtrade |
| `nfi-bte confirm ...` | Compare an existing Freqtrade export |
| `nfi-bte report RUN_DIR` | Rebuild the human-readable report |
| `nfi-bte runs list` | Inspect the durable run index |
| `nfi-bte clean --dry-run` | Audit managed storage without deleting |
| `nfi-bte performance ...` | Repeat a sealed performance gate |
| `nfi-bte certify ...` | Create release-grade evidence |
| `nfi-bte contract verify` | Verify the sealed regression contract |

Use `nfi-bte COMMAND --help` for the complete interface.

## Platforms and requirements

Native wheels are built for supported hosts:

- Linux x86_64
- Linux aarch64
- macOS Apple Silicon

On Windows, use a WSL2 Linux distribution; it runs the Linux wheel and ABI. Native
Windows product execution fails closed with `native Windows is unsupported; run nfi-bte
under WSL2 (Linux)`.

Requirements:

- Python 3.12, 3.13, or 3.14
- an NFI/Freqtrade strategy, config, candle directory, and timerange
- Docker only for data downloads or official Freqtrade confirmation. Install Docker
  for your distribution yourself
  ([Docker Engine docs](https://docs.docker.com/engine/install/) cover Ubuntu,
  Debian, Fedora, and more); install commands differ per distro and the installer
  does not manage Docker

Public market metadata needs no exchange API credentials. Never commit private keys or
live-trading secrets.

## Development

```bash
git clone https://github.com/vntrevx/NFI_BackTestEngine.git
cd NFI_BackTestEngine
uv sync --extra dev --frozen
uv run maturin develop --release --locked
uv run pytest -q
```

Required CI is risk-tiered. README, documentation, and roadmap bookkeeping use a
fast text/JSON lane (29 seconds observed); CI-policy changes took 44 seconds in the
rollout acceptance run. Runtime changes still require the full Linux/macOS native
parity matrix; WSL2 exercises the Linux ABI. Protected merges are not tested a second
time on `main`; release paths retain same-commit checks. See the
[CI policy](docs/ci-policy.md).

Architecture, contracts, and detailed workflows:

- [Architecture](docs/architecture.md)
- [X7 support boundary](docs/x7-support.md)
- [Result report contract](docs/result-report.md)
- [Release policy](docs/release.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)

## Project boundary

NFI Backtest Engine is a research accelerator, not a live trading bot or a promise of
profitability. Confirm material results with official Freqtrade before using them for
deployment.

MIT licensed.
