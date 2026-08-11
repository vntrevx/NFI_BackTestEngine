# Native Vector Shadow

M21-03 proves that Python and Rust independently calculate the same vector surface. The two
lanes share only immutable program contracts and input values; neither lane reads or reuses the
other lane's output.

## Compared surface

The committed bundle at `benchmarks/reference/vector-shadow/` contains path-independent
Indicator, Signal, and Tag programs plus a pinned Python oracle. Rust parses and validates each
program independently, executes it, and compares:

- Indicator Float64 values by exact IEEE-754 bits, including canonical NaN warmup
- raw `enter_long`, `enter_short`, `exit_long`, and `exit_short` values and nulls
- raw `enter_tag` and `exit_tag` strings, including empty and trailing whitespace
- Freqtrade's one-row signal/tag shift and the first executable row
- enabled indexes using exact numeric `== 1`, never truthiness or epsilon comparison

The Rust mutation engine applies full-column and nullable masked writes in original source order.
Overlapping masks remain last-write-wins. `Int64`, Boolean, Float64, UTF-8, Arrow null, NaN, and
raw tag strings stay distinct. Signal IDs and tags remain program data; no strategy, pair, SHA,
Signal number, or expected result is an execution branch.

## Reproduction

```bash
uv run python scripts/generate_vector_shadow_fixture.py
uv run pytest -q tests/test_vector_shadow_fixture.py
cargo test --manifest-path rust/Cargo.toml -p nfi-vector-core mutation::tests
```

Regeneration re-executes the Python contracts and must reproduce every committed program and
fixture byte-for-byte. Rust rejects stale fingerprints, numeric masks, malformed route contracts,
unsupported helper behavior, and any exact-value difference.

## Claim boundary

This proves independent Rust vector semantics for the committed reachable contract. M21-04 still
owns the production in-memory connection from Rust vector output to the simulator and retained
Feather replay. The latest X7 source also remains fail-closed at its separately recorded dynamic
configuration boundary until the remaining source compiler lowering is complete. Full Native
latest-NFI qualification therefore remains M22-01, not an M21-03 claim.
