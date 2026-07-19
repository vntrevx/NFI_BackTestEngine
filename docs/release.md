# Release and readiness

## Product boundary

Version 0.5.0 is an alpha release of the benchmark, exact-parity, native packaging,
hardware/data preparation, X7 vector, and checkpointed research infrastructure. It is
not a claim that an arbitrary NFI file, pair universe, or strategy revision can already
complete an exact Rust backtest.

The product target is nevertheless revision-independent: a user supplies the current
NFI file, the engine analyzes and compiles that exact source, the default run spans the
previous five complete calendar years, and official Freqtrade verifies the finalist.
The v17.4.413 boundary below describes current evidence, not the intended permanent
input version.

The current alpha executes the source-pinned X7 v17.4.413 managed long routes,
short-rebuy tags 561-563, constrained isolated-futures accounting with uniform 3x
leverage, and the tag-120 spot/backtest grind state machine. The sealed
APE/USDT:USDT annual futures run matches Freqtrade exactly at the final normalized
surface: 11 trades, 164 orders, 142 adjustment orders, one short trade, and eight
funded trades. It does not reach liquidation and does not certify arbitrary pairs,
protections, or pair locks.

The APE top-coins, tag-62 rebuy exit, ZEC tag-120, and APE/AAVE equal-timestamp
shared-slot fixtures provide additional exact spot evidence. In v17.4.413, tag 121
is a dormant route constant with no literal entry-condition branch. It remains
fail-closed so a future source that emits it cannot silently skip regular-mode
position adjustment.

The public runner returns one of:

- `prepared` — requested data and vectors are sealed; no trade result was requested;
- `blocked_unsupported_semantics` — simulation was requested but at least one callback
  or adapter has no exact lowering;
- `complete` — reserved for a fully simulated result whose supported contract passed.

Only `complete` may contain a result. A finalist still requires the official Freqtrade
confirmation lane.

`complete` describes the sealed run's declared scope, not full-X7 product readiness.
Unknown, tag-121, unsupported mixed tags, non-uniform futures leverage, or unsupported
callbacks must still produce `blocked_unsupported_semantics`.

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
9. Exact evidence-schema tests, including the annual X7 futures certificate
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
