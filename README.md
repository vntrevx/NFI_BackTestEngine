# NFI Backtest Engine

**Backtest years of NFI in minutes, then prove the result against Freqtrade.**

[![Release](https://img.shields.io/github/v/release/vntrevx/NFI_BackTestEngine?display_name=tag&sort=semver)](https://github.com/vntrevx/NFI_BackTestEngine/releases/latest)
[![CI](https://github.com/vntrevx/NFI_BackTestEngine/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/vntrevx/NFI_BackTestEngine/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12--3.14-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/github/license/vntrevx/NFI_BackTestEngine)](LICENSE)

NFI Backtest Engine is a native Rust/Python research backtester for
[NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity).
It runs supported NFI workloads in parallel and compares the final trade and state
surface with official Freqtrade at zero tolerance.

The engine compiles the strategy file and inputs supplied at runtime. It does not
hard-code a pair list, timerange, strategy hash, or expected result. Unknown active
semantics stop with a clear fail-closed verdict instead of being approximated.

## Release status

| Scope | Status |
| --- | --- |
| Latest public release | [v1.1.0](https://github.com/vntrevx/NFI_BackTestEngine/releases/tag/v1.1.0) |
| Five-year Spot | Certified independently by v1.0.0 |
| Five-year Futures | Certified independently by v1.1.0 |
| Current `main` | v1.2.0 source candidate validated by Required CI; not published as a combined certified release |

The Spot and Futures certificates remain valid for their own sealed strategy,
configuration, data, wheel, and host. They are not a same-candidate Spot-versus-Futures
benchmark, and they must not be combined into a newer full-certification claim.

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
does not pay the same Python callback overhead. The remaining differences include X7
v17.4.421 versus v17.4.435 and Windows/Docker Desktop versus native Linux.

Both certificates still prove exact parity for their sealed workloads. They do not
form a controlled Spot-versus-Futures performance test, so this project makes no
cross-mode speed ratio claim.

For a fresh-run expectation, compare Spot's full native median with Futures' cold seed.
Use the Futures reuse number only after its content-addressed vectors already exist.
Actual runtime still depends on strategy behavior, data, hardware, and memory limits.

Evidence:

- [Spot v1.0.0 release](https://github.com/vntrevx/NFI_BackTestEngine/releases/tag/v1.0.0)
- [Futures v1.1.0 release](https://github.com/vntrevx/NFI_BackTestEngine/releases/tag/v1.1.0)
- [Futures certificate](https://github.com/vntrevx/NFI_BackTestEngine/releases/download/v1.1.0/full-x7-futures-certification.json)
- [Futures evidence bundle](https://github.com/vntrevx/NFI_BackTestEngine/releases/download/v1.1.0/full-x7-futures-certification-evidence.zip)
- [Published SHA-256 checksums](https://github.com/vntrevx/NFI_BackTestEngine/releases/download/v1.1.0/SHA256SUMS.txt)

## How it works

1. Inspect the supplied strategy, config, market data, and machine.
2. Stop if active behavior cannot be lowered exactly.
3. Run the deterministic native engine with calibrated CPU and memory limits.
4. Confirm a chosen result with pinned official Freqtrade.
5. Report exact parity or the first exact difference.

The native lane is for fast research. The official lane is independent and slower by
design; it is the final semantic authority.

## Native core structure

The Rust core keeps the public API in a thin facade and separates domain contracts,
vector-backed I/O, scalar callbacks, execution, validation, portfolio/futures rules,
NFI routing and adjustments, protections, chronological simulation, profiling, and
result assembly. The Feather boundary separately owns manifest verification, Arrow
schema projection, fixed-width row encoding, and typed scalar decoding.

This layout is a behavior-preserving refactor. It does not add strategy-, pair-,
timerange-, SHA-, or expected-result branches, and optimization work remains a
separate measured stage.

## Install

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/vntrevx/NFI_BackTestEngine/main/install.ps1 | iex
```

Linux x86_64/aarch64 or macOS Apple Silicon:

```bash
curl -LsSf https://raw.githubusercontent.com/vntrevx/NFI_BackTestEngine/main/install.sh | sh
```

The installer downloads the matching wheel from the latest public GitHub release,
checks its published SHA-256 digest, and installs `nfi-bte` in an isolated `uv`
environment.

```text
nfi-bte --version
nfi-bte doctor
```

The latest public installer currently returns `nfi-bte 1.1.0`. A source checkout of
`main` reports the validated 1.2.0 candidate version.

## Quick start

Run the first-time wizard with an NFI strategy:

```powershell
nfi-bte run path\to\NostalgiaForInfinityX7.py
```

The wizard discovers the class, Freqtrade config, candle directory, pair whitelist,
and hardware limits. It proposes the previous five complete calendar years and saves
reusable project settings under `.nfi/project.json`.

Accept every safely discovered value:

```powershell
nfi-bte run path\to\NostalgiaForInfinityX7.py --yes
```

Resume the saved project:

```powershell
nfi-bte run
```

Use explicit inputs when discovery is not appropriate:

```powershell
nfi-bte run path\to\NostalgiaForInfinityX7.py `
  --class NostalgiaForInfinityX7 `
  --config user_data\config.json `
  --datadir user_data\data\binance `
  --timerange 20210101-20260101 `
  --output-dir artifacts\x7-research `
  --yes
```

Missing public candles are downloaded through the pinned Freqtrade container by
default. Add `--no-download` for an offline, fail-if-missing run.

## Confirm with official Freqtrade

Run the pinned official reference from a completed native result:

```powershell
nfi-bte reference research artifacts\x7-research `
  --output-dir artifacts\x7-research-official
```

Or compare an existing Freqtrade export:

```powershell
nfi-bte confirm `
  artifacts\x7-research `
  path\to\backtest-result.zip `
  --strategy NostalgiaForInfinityX7 `
  --output-dir artifacts\x7-confirmation
```

The comparison has no floating-point tolerance. It never concatenates independent
timerange chunks into one claimed result because chunk boundaries reset wallet,
open-trade, protection, and strategy state.

## Core guarantees

- Official Freqtrade is the final oracle.
- Unknown active semantics fail closed.
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
| `report.html` | Self-contained visual report |
| `checkpoints/` | Hash-validated resumable stages |

Regenerate presentation files without rerunning the simulation:

```powershell
nfi-bte report artifacts\x7-research
nfi-bte report artifacts\x7-research --full-report
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

Native wheels are built for:

- Windows x64
- Linux x86_64
- Linux aarch64
- macOS Apple Silicon

Requirements:

- Python 3.12, 3.13, or 3.14
- an NFI/Freqtrade strategy, config, candle directory, and timerange
- Docker only for data downloads or official Freqtrade confirmation

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
