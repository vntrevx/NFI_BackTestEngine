# Native Vector Core

`nfi-vector-core` is the safe Rust execution substrate for
`indicator-program-v1`. It is intentionally independent of strategy names,
Signal numbers, pairs, timeranges, upstream commits, and expected results.

## Responsibility boundaries

- `nfi-vector-core` owns program validation, output reachability, typed Arrow
  views, deterministic scalar behavior, bounded state, and batch execution.
- `nfi-vector-io` continues to own Feather paths, hashes, IPC decoding, and
  transport projection.
- `nfi-sim-core` continues to own trades, orders, wallets, callbacks, funding,
  liquidation, protections, and the event loop.
- The Python compiler remains the source-to-IR boundary.

The core uses Arrow2 0.18, matching the existing transport. It has no filesystem,
PyO3, simulator, Polars, ndarray, Rayon, or SIMD dependency. Workspace policy
forbids unsafe Rust and no fast-math option is enabled.

## Deterministic execution

Input columns are borrowed from Arrow buffers after exact physical-type checks.
Only columns reachable from requested outputs enter the plan. Unused source
columns and unrelated program branches are neither decoded nor allocated by the
core.

All NaN payloads are normalized to one quiet NaN at kernel boundaries. Null and
NaN remain distinct; signed zero and infinities are preserved. Arithmetic order
is explicit and comparisons use exact IEEE behavior. Approximate equality is not
an execution or promotion rule.

Shift and rolling-window state uses bounded ring buffers that survive record-batch
boundaries. Long-stream tests assert that retained state never exceeds the
compiled lag or window. A discard sink demonstrates this live-value bound:

```text
batch_rows × (projected + intermediate + final columns) + bounded state
```

## Exact indicator kernels

M20-04 adds operation-driven implementations for every TA-Lib function reachable
from NFI X7 at upstream `e857f9b6`: ADX, AROON, BBANDS, CCI, EMA, MAX, MFI, MIN,
MINUS_DI, OBV, PLUS_DI, ROC, RSI, SMA, STDDEV, STOCHF, SUM, ULTOSC, and WILLR.
Pandas rolling mean, sum, min, and max are also exact for every inventoried
window, including the 2,016-candle window.

Captured pre-Full-Native X7 sources also reach TA-Lib `STOCH` and an older
Chaikin helper whose numerator uses a NumPy prefix sum while its denominator
uses TA-Lib `SUM`. Both are separate operation contracts with exact warmup and
streaming tests; no strategy version or source hash selects their behavior.

The committed oracle contains 2,200 input rows, all 56 reachable TA-Lib parameter
variants, and 24 rolling variants. Finite values compare by exact f64 bits; NaN
warmup positions compare exactly and signal-boundary tests use no tolerance.
Moving, directional, oscillator, and rolling state continues across arbitrary
record-batch boundaries while retaining only period-scale memory.

Regenerate the oracle from an inventory with:

```bash
uv run python scripts/generate_indicator_kernel_fixture.py \
  .nfi/indicator-inventory.json \
  benchmarks/reference/indicator-kernels/nfi-x7-e857f9b6-talib-v0.6.4.json
```

TA-Lib multi-output calls are represented by generic output names such as
`aroondown`, `fastk`, or `upperband`; tuple assignment and constant output
subscripts compile without Signal- or strategy-specific branches.

## Informative frames and resampling

M20-05 pins informative alignment to Freqtrade 2026.5.1. Every source frame
has an explicit `(pair, timeframe)` identity; a BTC informative frame cannot be
silently substituted for the traded pair, even when its column names match.
For slower timeframes, visibility begins at:

```text
informative open + informative merge minutes - base merge minutes
```

Consequently, a 1h candle opened at `00:00` first becomes visible to a 5m base
frame at `00:55`, never earlier. Freqtrade obtains both merge durations with
CCXT seconds divided by 60, so sub-minute timeframes deliberately use its
floor-minute behavior while resampling retains their real seconds.
`ffill=False` performs an exact-key left join,
leaving missing matches null and preserving duplicate Cartesian rows.
`ffill=True` carries only an already-visible historical row. Monthly `1M`
visibility uses the next calendar month start minus the base duration.

The preprocessing contract mirrors the pinned Freqtrade anchors: ordinary
timeframes use fixed-second bins, weekly data uses Monday anchors, months use
month starts (`nMS`), and years use year starts (`nYS`). In particular, the
pinned multiweek branch maps `2w` to `1W-MON`. Cleanup and optional OHLCV gap
filling remain separate from informative merge; the merge does not synthesize
candles.

Complete-frame alignment reproduces the official missing, duplicate, unsorted,
suffix, monthly, and cross-pair cases. Streaming alignment requires ordered
chunks and explicit identities, retains at most one historical informative row,
and rejects future rows, timestamp regressions, schema drift, unsupported
faster-frame joins, and column collisions at the source location instead of
guessing. Because Freqtrade can repair the whole prefix before its first exact
match, a bounded `ffill=True` stream requires that match in its first non-empty
base chunk; otherwise it stops before emitting provisional rows.

Latest X7 performs its merges with `ffill=False` and then calls one final
source-order `DataFrame.ffill()`. The Rust fill primitive treats both null and
NaN as missing, preserves the bits of retained finite values, infinities, and
signed zero, and retains at most one value per column across chunks. It never
adds or removes candle rows.

The committed oracle was generated by the official Freqtrade helper at tag
2026.5.1 (commit `6fa47093`). Regeneration is an evidence-authoring command and
requires that exact checkout at
`.nfi/roadmap-acceptance/M20-05/freqtrade-2026.5.1`:

```bash
uv run python scripts/generate_informative_fixture.py
```

It writes
`benchmarks/reference/informative/freqtrade-2026.5.1.json`; both the Python
compatibility layer and Rust alignment tests replay the same cases exactly.

## Strategy execution boundary

The Full Native manifest seals three facts: strategy source is read through the
Python AST compiler only, no `populate_*` method was executed, and the runtime is
`rust-full-native`. Rust rejects the manifest before market-data decoding if any
of those fields is absent or different. The engine profile repeats the sealed
contract, and the vector checkpoint records zero Python workers and no analyzed
Feather outputs. A regression fixture places a deliberate exception at module
scope and still completes through Rust, proving that the strategy file is never
imported or executed in this lane.

This does not remove Python from source compilation, nor remove the explicit
official Freqtrade fallback. It removes execution of NFI strategy Python from
the Full Native runtime. Legacy captured Feather replay remains available as an
evidence/compatibility transport and is not relabeled as Full Native.

## Current claim boundary

M22-01 qualifies upstream X7 `1df961c0` through the complete Spot and Futures
pipeline. Indicator, Signal, Tag, stateful callbacks, trade surface, and full
state are exact for the committed dual-mode qualification, and the older public
Spot/Futures captures remain exact after the same runtime changes. M22-02 owns
the no-strategy-execution contract and fallback isolation; M22-03 still owns the
five-year performance and storage certificate, and M22-04 owns the v1.6.0
release. Unsupported constructs remain fail-closed and the announced official
Freqtrade fallback remains available. EWM remains fail-closed because it is not
reachable in the qualified upstream inventory. Unknown functions, non-SMA
TA-Lib MA types, centered rolling windows, Arrow null indicator inputs, and
unselected multi-output calls stop rather than being guessed.

The Rust boundary can validate a compiled contract directly:

```bash
cargo run --manifest-path rust/Cargo.toml -p nfi-vector-core \
  --example validate_indicator_program -- .nfi/indicator-program.json
```
