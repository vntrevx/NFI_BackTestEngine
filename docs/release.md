# Release and readiness

## Product boundary

Version 0.6.0 is an alpha release of the benchmark, exact-parity, native packaging,
hardware/data preparation, X7 vector, and checkpointed research infrastructure. It is
not a claim that an arbitrary future NFI file, pair universe, or strategy branch can
already complete an exact Rust backtest.

The product target is nevertheless revision-independent: a user supplies the current
NFI file, the engine analyzes and compiles that exact source, the default run spans the
previous five complete calendar years, and official Freqtrade verifies the finalist.
The versioned evidence below describes reproducible regressions, not a permanent input
allowlist.

The current alpha executes the source-compiled managed long routes, short-rebuy tags
561-563, tag-dependent futures leverage, Binance isolated-liquidation accounting,
tag-120 legacy grind, tag-121 regular adjustment, and four static Freqtrade protection
methods with deterministic pair-lock state. The X7 v17.4.421 branch matrix pins
upstream commit `5e168431991e05a889514eb1e16fdbebc6a09811` and reaches tag 121,
all four protection methods with real locks, a compound tag, variable leverage, and an
actual liquidation exit in seven official full-state fixtures. It does not certify an
arbitrary future X7 revision or replace the continuous representative run.

The v17.4.413 APE top-coins, tag-62 rebuy exit, ZEC tag-120, and APE/AAVE
equal-timestamp shared-slot fixtures remain additional exact spot evidence. In
v17.4.418, tag 121 has a compiled entry branch whose source switch is disabled; its
regular-mode path therefore has focused native proof but no branch-reaching official
trade.

The first large v0.6 native diagnostic covers 80 configured spot pairs and
`20210101-20260101`. It preserves the exact 750-trade result while reducing the native
process from 2,022.07 seconds to 763.70 seconds on the observed WSL2 host. This is not a
release speed certificate: vectors were reused, the available-history policy recorded
275 shortfalls, and the measurement has not yet been repeated across operating systems.

The continuous official run of that workload was OOM-killed at the enforced
21,852,071,527-byte Docker limit and produced no comparison result. The safe bounded
follow-up kept all 80 pairs but used `20250701-20260101` and ran sequentially. It
matches Freqtrade 2026.5.1 byte-for-byte at the normalized final surface: 167 trades,
402 orders, 23 rejected signals, and zero numeric tolerance. The native-core
observation was 58.93 seconds and 88,928,256 peak bytes versus 253.09 seconds and
9,450,651,648 peak bytes for the complete official container process. These 4.29x and
106.27x ratios are diagnostic because the native measurement begins with sealed
vectors, while Freqtrade performs its analysis inside the measured process.

The bounded result does not replace the representative four-year release gate.
Independent timerange chunks reset wallet, open positions, and protection state and
therefore cannot be joined into a continuous-state parity certificate.

The public runner returns one of:

- `prepared` — requested data and vectors are sealed; no trade result was requested;
- `blocked_unsupported_semantics` — simulation was requested but at least one callback
  or adapter has no exact lowering;
- `complete` — reserved for a fully simulated result whose supported contract passed.

Only `complete` may contain a result. A finalist still requires the official Freqtrade
confirmation lane.

`complete` describes the sealed run's declared scope, not full-X7 product readiness.
Unknown tags, unsupported mixed tags, new dynamic leverage/protection programs, or
unsupported callbacks must still produce `blocked_unsupported_semantics`.

## Required checks

Before tagging:

1. `uv lock --check`
2. `uv run pytest -q`
3. `uv run ruff check .`
4. `uv run basedpyright --level error python/nfi_backtest_engine`
5. `cargo fmt --all -- --check`
6. `cargo test --workspace --locked`
7. `cargo clippy --workspace --all-targets --locked -- -D warnings`
8. Both captured contract fixtures at `--level full`
9. Exact evidence tests, including both annual X7 futures revisions, the bounded
   80-pair spot result, and their narrow claim boundaries
10. `uv build --sdist --wheel`
11. Install the wheel into a clean Python 3.12 environment and rerun one full fixture
12. On a Docker host, verify daemon-resource inspection, one managed official fixture,
    cgroup memory reporting, and zero remaining owned containers
13. Run `nfi-bte strategy check` against the latest upstream X7 source
14. Dry-run the Windows and Unix release installers against the published assets
15. Run the representative workload with an empty vector cache and `--recalibrate`;
    retain its workload calibration, engine phase profile, process-tree peak, and exact
    official confirmation
16. Verify the representative run uses at least 80 pairs and 1,460 days before
    publishing any 10x or long-horizon memory claim
17. Run `nfi-bte certify` with at least three representative repetitions and one or
    more branch-reaching `--state-probe` fixtures; retain the reproducible bundle

The CI workflow runs the tests on Linux, Windows, and macOS and repeats native full
parity on Linux. Docker-free CI validates the portable resource and command contracts;
the release gate additionally exercises the managed container path on a real Docker
Desktop or Docker Engine host.

## Publishing

Pushing a `v*` tag builds ABI3 wheels and creates a GitHub release for:

- Linux x86_64 and aarch64, manylinux 2.17;
- Windows x86_64;
- macOS arm64;
- source distribution.

The release workflow verifies the Linux wheel before creating the GitHub release.
GitHub Releases is the only supported registry. `install.ps1` and `install.sh` select
the native wheel, verify its asset digest, and call `uv tool install`. Build jobs remain
read-only, while the GitHub release job receives only `contents: write`.

## Full X7 v1 gates

A later release can claim full X7 support only when all of these are true:

- every active X7 strategy callback is executable in Rust with no per-candle Python;
- spot and futures market metadata, funding, liquidation events, fees, and precision are frozen;
- position adjustment covers rebuy, partial exits, derisk, and grind order history;
- protections and pair locks preserve global chronological order;
- every claimed pair/route combination has branch-reaching differential evidence;
- repeated runs are deterministic;
- exact normalized trade parity and full state parity pass on the supported certificate;
- an 80-pair, four-year fresh benchmark demonstrates at least 10x screening speed without
  exceeding the memory gate;
- finalists are reproducible with official Freqtrade.
