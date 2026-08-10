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

## Current claim boundary

M20-03 provides the engine, typed columns, generic scalar operations, causal
shift, rolling storage, and source-located fail-closed errors. TA-Lib indicators,
rolling reducers, EWM algorithms, and the complete latest-NFI kernel set belong
to M20-04 and require official Python/TA-Lib column-exact fixtures before Native
promotion. An unimplemented reachable opcode stops; it is never guessed.

The Rust boundary can validate a compiled contract directly:

```bash
cargo run --manifest-path rust/Cargo.toml -p nfi-vector-core \
  --example validate_indicator_program -- .nfi/indicator-program.json
```
