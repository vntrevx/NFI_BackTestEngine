# Release and readiness

## Product boundary

Versions 1.0.0 and 1.1.0 are supported by their published continuous five-year Spot
and Futures certificates. Their historical combined-release gate required both mode
certificates to use the same strategy and candidate wheel and preserved sealed Windows,
Linux, and macOS native evidence. That historical evidence does not extend native
Windows support to the current product: v1.9 supports Linux and macOS; Windows users
run the Linux build and ABI under WSL2, and native Windows fails closed with `native
Windows is unsupported; run nfi-bte under WSL2 (Linux)`.

Each mode certificate binds the benchmark, exact parity, native package,
hardware/data preparation, X7 vectors, checkpointed research pipeline, strategy
commit, and sealed input. It is not a claim that an arbitrary future NFI file,
pair universe, or strategy branch can complete an exact Rust backtest.

The product target is nevertheless revision-independent: a user supplies the current
NFI file, the engine analyzes and compiles that exact source, the default run spans the
previous five complete calendar years, and official Freqtrade verifies the finalist.
The versioned evidence below describes reproducible regressions, not a permanent input
allowlist.

The v1.1.0 Futures release executes the source-compiled managed long routes, short-rebuy tags
561-563, tag-dependent futures leverage, Binance isolated-liquidation accounting,
tag-120 spot/futures grind, tag-121 regular adjustment, and four static Freqtrade
protection methods with deterministic pair-lock state. Its X7 v17.4.435 branch matrix pins
upstream commit `2bc3058ed4f8480ed7498efca49b5195c7b47e9b` and reaches tag 121,
all four protection methods with real locks, a compound tag, variable leverage, and an
actual liquidation exit in nine official full-state fixtures. It does not certify an
arbitrary future X7 revision or replace the continuous representative run.

The v17.4.413 APE top-coins, tag-62 rebuy exit, ZEC tag-120, and APE/AAVE
equal-timestamp shared-slot fixtures remain additional exact spot evidence. In
v17.4.418, tag 121 has a compiled entry branch whose source switch is disabled; its
regular-mode path therefore has focused native proof but no branch-reaching official
trade.

The v17.4.435 bounded Futures portfolio proof covers ten pairs and six months. Its
63 trades and 296 orders, including long, short, funded, compound-tag, and tag-120
routes, match the immutable Freqtrade 2026.5.1 export exactly. It remains a useful
wide differential regression but is no longer the release performance boundary.

The continuous Futures proof covers 80 listing-aware Binance USDT-M pairs,
`20210726-20260726`, all five required timeframes, funding-rate data, and mark data.
Pinned Freqtrade 2026.5.1 completed once in 6,430.90 seconds. Three fresh final-wheel
processes using the content-addressed vectors produced the identical 174-trade,
795-order surface in a 529.38-second median, an observed 12.148x speedup. The cold
strategy-to-vector seed completed in 807.01 seconds. All native, official, and probe
surfaces share SHA-256
`99fc0bd3f7622ba7feb0d16f3f76d5053b16c15db80568c257d29d9ee3af4ed5`.
The cold process-tree peak was 25,485,381,632 bytes; the measured reuse peak was
929,001,472 bytes.

The continuous representative proof covers 80 configured spot pairs,
`20210101-20260101`, and all five required timeframes. Pinned Freqtrade 2026.5.1
completed once in 221,661.91 seconds with a 16,636,026,880-byte container peak and no
OOM kill. Its final surface is byte-identical to the native result: 927 trades, 11,783
orders, 4,499 rejected signals, final balance `14779.379760020001`, and SHA-256
`8ae4fe84eaf869904cc8a26056f08218548546b316f620441e57417c24cac38c`.
The release certificate repeats only the installed native candidate and reports its
median wall time and maximum process-tree RSS.

The release reference now defaults to a hash-guarded spooled datastore. It
preserves the pinned Freqtrade strategy/callback/order/trade methods and their
pair order, while indicator results and DataProvider frames move through Arrow
record batches one pair at a time. Flushed Arrow files are released from Linux
page cache so on-disk evidence is not charged as resident cgroup memory. On the
current X7 v17.4.421,
80-pair `20250701-20260101` proof, this final representation retained exact
native/Freqtrade surface parity, used zero swap, and reduced the official
container peak from 7,236,866,048 bytes before pairwise indicator spooling to
3,637,440,512 bytes. Wall time increased from 570.19 to 658.06 seconds. This
bounded proof validates the representation but does not replace the published
continuous five-year Spot oracle.

The older bounded result remains a regression but does not replace the representative
five-year release gate. Independent timerange chunks reset wallet, open positions, and
protection state and therefore cannot be joined into a continuous-state parity
certificate.

Release input selection is strict over the declared timerange: all 80 pairs must have
every required timeframe at both interval edges, with duplicate and reversed
timestamps rejected. Startup context before the interval follows official Freqtrade
semantics. A pair's available pre-listing prefix is consumed and its shortfall is
sealed, while the five-year interval itself remains complete and unsplit.

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

## Versioned regression contract

The v1.1 contract is a read-only manifest bundled with the package. It pins the public
command tree and exit-code meanings, stable fail-closed diagnostics, representative
documents and schemas, bounded Spot/Futures surfaces, nine Futures full-state
projections, resume and mutation scenarios, result artifacts, both release
certificates, and every v1.0.0/v1.1.0 release asset.

From a source checkout, run:

```bash
uv run nfi-bte contract verify
```

The default command verifies repository evidence and downloads the public release
assets to hash their bytes without retaining or rewriting them. For a disconnected
repository audit, use `--offline`; this still verifies all local evidence and reports
the release records as `identity-pinned`. Existing downloaded assets can be supplied
without network access:

```bash
uv run nfi-bte contract verify \
  --release-assets v1.0.0=/path/to/v1.0.0 \
  --release-assets v1.1.0=/path/to/v1.1.0
```

An intentional public contract change requires a new contract version. Golden hashes
are consumed only by this verifier and tests; simulation and strategy routing never
read them.

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
14. Dry-run the supported Linux/macOS release installer, including the Linux ABI path
    inside WSL2
15. Run the representative workload with an empty vector cache and `--recalibrate`;
    retain its workload calibration, engine phase profile, process-tree peak, and exact
    official confirmation
16. Verify the representative run uses at least 80 pairs and 1,825 days before
    publishing any 10x or long-horizon memory claim
17. Run the continuous 80-pair, five-year official Freqtrade oracle once, then run
    `nfi-bte certify` with a sealed positive `--swap-cap-gib`, at least three fresh
    native candidate repetitions, and one or more branch-reaching `--state-probe`
    fixtures; retain the reproducible bundle. Missing Native process-tree or Official
    cgroup swap measurements fail the release gate.
    Extend the native candidate to five repetitions when its wall-time spread exceeds
    5%; never repeat the multi-year official oracle merely to calculate native timing
    variance.

The CI workflow runs tests on Linux and macOS and repeats native full parity on Linux.
Release-candidate wheels additionally run sealed Spot and Futures full-state fixtures on
Linux x86_64, Linux aarch64, and macOS arm64 with one excluded warmup and three measured
fresh processes, extending to five above 5% spread. This `exact-fixture` lane records
each supported-platform median and peak RSS and proves wheel portability; WSL2 uses the
Linux ABI rather than a separate native Windows lane. Its Windows-hosted job must
prove distribution version 2 before running the same wheel and fixture commands
inside WSL2. Platform reports seal the guest kernel identity; WSL1 and ambiguous
Microsoft-kernel identities cannot satisfy the WSL2 release gate. It is not presented as the
representative five-year speed claim. Docker-free CI validates portable resource and
command contracts, while the release gate additionally exercises the managed container
path on a real Docker Engine host.

## Publishing

The `Build release candidate` workflow builds three wheels and one source
distribution once, verifies the Linux wheel, and stores a SHA-256-sealed candidate
bundle for:

- Linux x86_64 and aarch64, manylinux 2.17;
- macOS arm64;
- source distribution.

The same workflow also runs an x86_64 clean-room job with no source checkout. It
installs only the downloaded wheel and executes doctor, strategy discovery, init,
Native run, report, status, clean dry-run, and a non-mutating update check against a
sealed one-pair scenario. Its hash-bound report is retained with the candidate.

After the downloaded candidate passes both Full X7 host certificates, the
`Publish release candidate` workflow binds that successful build run to an `-rc.N`
tag and publishes the already-built files. Run `Audit fixed release candidate` once
per day for cycles 1 through 7, using same-commit successful compatibility, discovery,
and nightly run IDs. Each cycle re-installs the public Linux wheel and restores and
revalidates the complete public asset set. Cycle 7 accepts exactly the six earlier
receipt run IDs and seals `ten-of-ten-release-audit.json`.

`Promote stable release` requires that cycle-7 audit run ID before it creates the
stable tag at the same commit. It copies the prerelease assets without rebuilding,
publishes those exact distributions to PyPI through the protected `pypi` environment
and OIDC Trusted Publishing, downloads the PyPI hashes, and runs a post-release
installed-tool smoke test. A failed audit or byte comparison leaves the RC published
but does not create the stable release.

GitHub Releases and PyPI are the supported distribution channels. `install.sh` selects the supported
Linux or macOS wheel, verifies its asset digest, and calls `uv tool install`. On
Windows, run that installer inside WSL2, which uses the Linux build and ABI; native
Windows fails closed. Candidate build and audit jobs remain read-only; only the
explicitly dispatched publishing workflows receive `contents: write`, and only the
protected PyPI job receives `id-token: write`.

## Full X7 v1 gates

A later release can claim full X7 support only when all of these are true:

- spot uses Binance `BASE/USDT`; futures uses Binance USDT-M
  `BASE/USDT:USDT` with isolated margin; cross margin and other exchanges fail
  before certification;
- each mode has exactly 80 pairs with complete continuous five-year coverage;
- futures seals one candle file per pair/timeframe plus one funding-rate and one
  mark series per pair, including required interval edges;
- every active X7 strategy callback is executable in Rust with no per-candle Python;
- spot and futures market metadata, funding, liquidation events, fees, and precision are frozen;
- position adjustment covers rebuy, partial exits, derisk, and grind order history;
- protections and pair locks preserve global chronological order;
- every claimed pair/route combination has branch-reaching differential evidence;
- repeated runs are deterministic;
- exact normalized trade parity and full state parity pass on the supported certificate;
- the futures fixture matrix reaches tag 121, long and short lifecycle trades,
  non-zero funding, compound tags, variable leverage, a liquidation exit, all
  four protection methods, real locks, and at least one locked-entry rejection;
- an 80-pair, five-year fresh benchmark demonstrates at least 10x screening speed without
  exceeding the memory gate;
- finalists are reproducible with official Freqtrade.
