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

## Current claim boundary

The latest X7 indicator operation set now has exact Native kernels. This does not
yet claim that the entire latest X7 source compiles: compilation currently stops
at `strategy.py:3168:21` on an informative/config attribute that belongs to the
source and multi-timeframe lowering work in M20-05. EWM remains fail-closed
because it is not reachable in this upstream inventory. Unknown functions,
non-SMA TA-Lib MA types, centered rolling windows, Arrow null indicator inputs,
and unselected multi-output calls also stop rather than being guessed.

The Rust boundary can validate a compiled contract directly:

```bash
cargo run --manifest-path rust/Cargo.toml -p nfi-vector-core \
  --example validate_indicator_program -- .nfi/indicator-program.json
```
