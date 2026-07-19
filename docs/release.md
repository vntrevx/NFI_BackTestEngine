# Release and readiness

## Product boundary

Version 0.2.0 is an alpha release of the benchmark, exact-parity, native packaging,
hardware/data preparation, X7 vector, and checkpointed research infrastructure. It is
not a claim that an arbitrary NFI file, pair universe, or strategy revision can already
complete an exact Rust backtest.

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

The CI workflow runs the tests on Linux, Windows, and macOS and repeats native full
parity on Linux.

## Publishing

Pushing a `v*` tag builds ABI3 wheels and creates a GitHub release for:

- Linux x86_64 and aarch64, manylinux 2.17;
- Windows x86_64;
- macOS arm64;
- source distribution.

The release workflow verifies the Linux wheel before creating the GitHub release.
PyPI publication is deliberately a separate manual dispatch so missing registry
configuration cannot block the GitHub release. Before selecting
`publish_pypi: true` for the release tag, create the protected `pypi` environment and
configure PyPI trusted publishing for:

- owner: `vntrevx`
- repository: `NFI_BackTestEngine`
- workflow: `release.yml`
- environment: `pypi`

The PyPI job alone receives `id-token: write`; build jobs remain read-only. The GitHub
release job receives only `contents: write`.

## Full X7 v1 gates

A later release can claim full X7 support only when all of these are true:

- every active X7 strategy callback is executable in Rust with no per-candle Python;
- spot and futures market metadata, funding, liquidation events, fees, and precision are frozen;
- position adjustment covers rebuy, partial exits, derisk, and grind order history;
- protections and pair locks preserve global chronological order;
- every claimed pair/route combination has branch-reaching differential evidence;
- repeated runs are deterministic;
- exact normalized trade parity and full state parity pass on the supported certificate;
- an 80-pair, one-year fresh benchmark demonstrates at least 10x screening speed without
  exceeding the memory gate;
- finalists are reproducible with official Freqtrade.
