# Native In-Memory Vector Transport

M21-04 connects typed Rust vector output to the existing chronological simulator without writing
an intermediate Feather file. This is a transport change only: it does not change signal timing,
pair priority, wallet mutation, order IDs, or callback semantics.

## Runtime contract

`nfi-vector-io` accepts one `MutationFrame` per pair through `InMemoryVectorPair`. The frame carries
the same `date`, OHLCV, callback-feature, and `nfi_exec_*` columns as the sealed Feather manifest.
The adapter preserves:

- millisecond timestamps and exact Float64 bits, including signed zero and canonical NaN features;
- Freqtrade's exact numeric-`1`/Boolean-`true` signal gate;
- raw non-empty entry and exit tags, including trailing whitespace;
- previous-close, funding-event, precision, stake-limit, and execution-start metadata;
- caller pair order in the resulting `SimulationInput`.

Missing columns, mixed types, Boolean feature nulls, duplicate pairs, and invalid execution ranges
fail before simulation. Signal numbers, tags, pairs, strategy names, and source hashes remain data,
never runtime branches.

## Safe parallel boundary

`execute_in_memory_pair_dag_profiled` runs independent pair vector tasks with Rayon and collects
them by original index. Pair conversion is also independent and parallel. Both stages finish before
the returned input enters the simulator. The simulator continues to mutate one shared wallet in one
global timestamp stream; equal-timestamp pair order is unchanged.

Rayon uses its configured worker limit, including `RAYON_NUM_THREADS`; no machine-specific thread
count is compiled into the engine. `InMemoryVectorProfile` records vector execution, pair conversion,
row/column counts, estimated source-buffer and retained simulator bytes, and the configured worker
limit.

## Feather replay proof

The SHA-verified Feather path remains supported. A focused Rust test writes one typed frame to
Feather, loads its file-backed representation, and compares it with the direct owned representation.
Both are run through the simulator with an every-candle observer; `SimulationResult` and every full
state event must be exactly equal.

```bash
cargo test --manifest-path rust/Cargo.toml -p nfi-vector-io --locked
```

The direct path removes vector hashing, IPC decoding, and private spool I/O from a Native pipeline.
The Feather path remains the durable evidence and replay format and is still preferable when bounded
heap use matters more than transport latency.

## Claim boundary

This completes the generic Rust vector-to-simulator connection. X7 v17.4.581 now
compiles through the full Native pipeline, with independent bounded Spot and Futures
trade-surface and full-state qualification. That current-source proof does not expand
the sealed historical certificates or support unknown future callback shapes;
unsupported X7 constructs still fail closed and the visible Official Freqtrade
fallback remains available.
