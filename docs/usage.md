# Usage Guide

[한국어](usage-ko.md) · [Ελληνικά](usage-el.md) · [Türkçe](usage-tr.md)

This guide covers installation, the first NFI X7 / X8 backtest, exact market-count selection, saved-project reuse, and common recovery commands.

## 1. Requirements

Use one of these supported environments:

- Linux x86_64 or ARM64;
- macOS on Apple Silicon;
- Windows through a WSL2 Linux shell.

Docker is required when the engine must rank current Binance markets, download public candles, or run an official Freqtrade comparison. Native Windows and PowerShell are not supported.

## 2. Install or update the CLI

Install the latest checksum-verified public release:

```bash
curl -LsSf https://raw.githubusercontent.com/vntrevx/NFI_BackTestEngine/main/install.sh | sh
```

Open a new terminal, then check the installation:

```bash
nfi-bte --version
nfi-bte doctor
```

Update an existing installation:

```bash
nfi-bte update
```

### X8 in stable v1.16.0

As of 2026-09-08, **v1.16.0 is the latest stable release** and includes Native X8
support alongside X7. The default installer above installs it. To upgrade an older
installation, use:

```bash
nfi-bte update --check
nfi-bte update
nfi-bte --version
```

The package reports `1.16.0`. Stable distributions are byte-for-byte identical to
the validated `v1.16.0-rc.1` assets; existing RC installations need no reinstall.

This release adds Native X8 support alongside X7, including the qualified
`short_exit_top_coins`, grind/rebuy, buyback, v3/v3.2/v4 systems, virtual fees,
stake/leverage, stop/ROI/trailing and trade-slot settings. Admission depends on the
supplied source and configuration. Unsupported active behavior stops with a diagnostic.
Installed wheels passed bounded original-X8 Spot/Futures trade and full-state parity
checks on all four supported platforms. This does not certify every configuration,
every exchange, or a five-year X8 workload. See the [release notes](releases/v1.16.0.md)
and [tested X8 scope](x8-support.md).

## 3. Download NFI

Create a working directory and clone the official NFI repository:

```bash
mkdir -p ~/nfi-backtest
cd ~/nfi-backtest
git clone --depth 1 https://github.com/iterativv/NostalgiaForInfinity.git
cd NostalgiaForInfinity
```

Run `nfi-bte` from this NFI directory. Numeric market selection uses the current volume and filter policy in its `configs/` directory.

Running `nfi-bte` without arguments opens a contextual start screen in a terminal. In
scripts it prints the compact beginner help and exits without waiting for input. Use
`nfi-bte help --all` to see the unchanged expert command surface.

Only Native-compatible sources are shown by the guided strategy picker. Inspect legacy,
invalid, and currently unsupported sources without executing them:

```bash
nfi-bte strategy list --show-unsupported
```

Every hidden entry includes a blocker code derived from its location or analyzed
semantics. A filename alone never grants or denies support, so legacy symlinks and
look-ahead implementations cannot appear as valid choices merely because of a name.

## 4. Recommended first run

Start the interactive setup and backtest:

```bash
nfi-bte run NostalgiaForInfinityX8.py
```

Press Enter to accept the recommended Spot mode, Binance exchange, BTC quick test, managed candle directory, and most recent seven complete days.

The market-count prompt accepts:

| Input | Result |
| --- | --- |
| `1` | BTC quick test; recommended for the first run |
| `10`, `20`, `40`, `80`, `100` | That exact number of current markets ranked by NFI's Binance policy |
| `all` | NFI's complete static backtest list |
| `custom` | A comma-separated list entered manually |

Numeric selections are resolved once through the pinned Freqtrade image. Their ordered symbols are then stored in `.nfi/project.json`, so the saved project remains reproducible if exchange volumes later change.

### First X8 run

From a fresh NFI checkout without a saved engine project, use a small Spot run:

```bash
nfi-bte run NostalgiaForInfinityX8.py \
  --trading-mode spot --pair ETH/USDT \
  --timerange 20250405-20250408 --workers 2
```

For isolated Futures, use `--trading-mode futures --pair ETH/USDT:USDT` in a
separate project. New NFI revisions are checked again; the filename alone does not
guarantee compatibility. The remaining examples also use X8. For X7, substitute
`NostalgiaForInfinityX7.py`. Give new runs distinct output directories.
For an existing project, use the explicit reconfiguration steps in section 5.

Worker capacity is derived from the host's CPU and available memory. `--workers 2`
only limits this run's parallel workers. The qualification's 4 GiB / two-CPU limits
are not global defaults. Explicit profiles can be created with
`nfi-bte system tune --memory-cap-gib 4 --cpu-process-limit 2`; existing files require
`--force` to replace them. Saved projects use their `runtime.profile_path`.
Native memory caps control planning; they are not operating-system RSS limits.
Official comparisons inherit the run's recorded CPU and memory caps. To re-detect
host capacity without explicit caps, use `nfi-bte system tune --force` on the profile
used by that project (use `--output PATH` for a non-default path).
See [execution profiles](x8-support.md#bounded-execution-profiles).

## 5. Start an exact 80-market run

For a new non-interactive Spot project using the recommended seven-day period:

```bash
nfi-bte run NostalgiaForInfinityX8.py \
  --trading-mode spot \
  --pair-count 80 \
  --yes
```

A historical X7 workload with 80 markets over five years used about 39 GiB of memory; this is not an X8 memory estimate. Confirm the seven-day run first before selecting a long timerange.

### Replace an existing saved project

If `.nfi/project.json` already exists, reconfigure it explicitly. A new output directory prevents old one-pair results from being resumed:

```bash
nfi-bte init --force NostalgiaForInfinityX8.py \
  --trading-mode spot \
  --pair-count 80 \
  --output-dir .nfi/runs/x8-80-pairs \
  --yes

nfi-bte run
```

Candle storage remains shared under `.nfi/data/binance`; hash-valid existing downloads can be reused.

## 6. Choose explicit markets

Repeat `--pair` for each Spot market:

```bash
nfi-bte run NostalgiaForInfinityX8.py \
  --trading-mode spot \
  --pair BTC/USDT \
  --pair ETH/USDT \
  --timerange 20260101-20260108 \
  --output-dir .nfi/runs/x8-btc-eth \
  --yes
```

For isolated Futures, use the Futures mode and canonical settlement suffixes for explicit symbols:

```bash
nfi-bte run NostalgiaForInfinityX8.py \
  --trading-mode futures \
  --pair BTC/USDT:USDT \
  --pair ETH/USDT:USDT \
  --timerange 20260101-20260108 \
  --output-dir .nfi/runs/x8-futures-btc-eth \
  --yes
```

For automatic Futures selection, replace the explicit `--pair` arguments with `--pair-count 10`, `20`, `40`, `80`, or `100`.

## 7. Select a timerange

Use `YYYYMMDD-YYYYMMDD`. The stop date is exclusive:

```bash
nfi-bte run NostalgiaForInfinityX8.py \
  --pair-count 20 \
  --timerange 20260101-20260201 \
  --yes
```

Omitting `--timerange` in interactive mode offers the recent seven-day default. With `--yes`, that recent seven-day period is selected automatically.

## 8. Resume and inspect results

After setup, run or resume the saved project with:

```bash
nfi-bte run
```

Inspect the latest checkpointed run from another terminal:

```bash
nfi-bte status
nfi-bte status --json
```

The status record reports measured elapsed time. ETA remains `estimating` until a
measured estimate is available; the CLI never invents an ETA from fixed stage weights.
For actionable prerequisite diagnostics or a local support bundle, use:

```bash
nfi-bte doctor --json
nfi-bte doctor --export-diagnostics .nfi/diagnostics.json
```

Diagnostic bundles never include environment variables or credentials and are never
transmitted automatically.

Before any preparation or simulation starts, the CLI displays the planned CPU worker
limit and asks for confirmation; Enter defaults to No. Use `--workers N` to lower the
parallel worker count. Non-interactive jobs must pass `--yes`, which is the explicit
consent that bypasses this prompt.

When a saved project exists, interactive `nfi-bte run` first asks whether to continue
it or start a new guided setup. Enter continues the saved project. Yes displays a
numbered strategy menu discovered from the NFI checkout, with the current strategy
marked and selected by default. After the strategy choice, trading mode, exchange,
market count, data, and timerange are configured again. Guided setup writes a separate
safe config and output; prior evidence remains untouched. `--new-run --yes` remains the
unattended option for fresh output with the same saved settings.

The terminal starts with a compact NFI header and one-line system readiness, then keeps
one rotating progress line in place. Completion prints the full Freqtrade-style result
tables and concise relative artifact paths. Use `--no-full-report` for the compact card.
The human-readable result is `report.md`; JSON and CSV files remain the machine-readable
contracts:

```text
.nfi/runs/<strategy-and-timerange>/
├── report.md
├── summary.json
├── trades.csv
├── orders.csv
├── equity.csv
├── verification.json
└── evidence/index.json
```

`report.md` uses portable Freqtrade-style ASCII tables and explicitly distinguishes a
valid zero-trade result from an execution error. Regenerating a report removes a stale
`report.html`; the CLI no longer opens or prompts for a browser.

Regenerate the Markdown report and machine exports without rerunning the simulation:

```bash
nfi-bte report .nfi/runs/<strategy-and-timerange>
```

After `nfi-bte update`, a completed run is reusable when only the package version
changed. The engine verifies its original identity and artifacts without rewriting
them. Any changed strategy, config, pairs, timerange, or semantic pipeline still
requires a different `--output-dir`.

The engine resumes only hash-valid completed stages. Use a different `--output-dir` when intentionally changing pairs, timerange, mode, or other run inputs.

## 9. Prepare data without starting Native simulation

To rank pairs, download required public candles, and prepare inputs without running the backtest:

```bash
nfi-bte run NostalgiaForInfinityX8.py \
  --pair-count 20 \
  --prepare-only \
  --yes
```

Run `nfi-bte run` afterward to continue the saved project.

## 10. Common recovery commands

Show all setup and execution options:

```bash
nfi-bte run --help
nfi-bte init --help
```

If the CLI reports that a saved project already exists:

```bash
nfi-bte run
```

or deliberately replace its setup:

```bash
nfi-bte init --force NostalgiaForInfinityX8.py
```

If Docker or Binance is temporarily unavailable, retry the same command. Pair-ranking failures preserve technical details in `.nfi/pair-selection-error.log`; exhausted candle-download failures print the exact `download-error.log` path. Partial valid candle downloads are reusable.
