# Native Tag Program

`tag-program-v1` is the versioned contract for exact NFI entry and exit tag generation. It
compiles strategy source into ordered data; it does not execute NFI Python in the Native lane.

## Exact contract

The runtime mirrors Freqtrade 2026.5.1 by initializing `enter_tag` to `""` immediately before
`populate_entry_trend`, then initializing `exit_tag` immediately before
`populate_exit_trend`. Strategy writes retain source order and each write depends on the prior
DataFrame version. Mask overlap, last-write-wins assignment, and compound append therefore
cannot be reordered.

Raw strings are preserved byte-for-byte, including repeated and trailing whitespace. NFI route
lookup uses Python `str.split()` token order without replacing the stored value. For example,
`"101 562 "` remains the trade's original tag while its canonical route is `("101", "562")`.
Signal numbers and tag strings are program data, never Python or Rust execution branches.
Formatted fragments such as `f"{signal_id} "` lower to a generic `format-string` node followed
by an ordered append, so adding another numeric Signal does not require a runtime code branch.

`tag-program-v1` also records numeric signal writes that share the same source function. This
keeps tag masks and read-after-write behavior exact while `signal-program-v1` remains the public
raw-signal contract. The M21-03
[`Native Vector Shadow`](native-vector-shadow.md) runs the compiled program independently in Rust
and compares every Indicator, Signal, Tag, and execution-index output.

## Evidence and limits

The committed oracle at `benchmarks/reference/tags/freqtrade-2026.5.1.json` executes the exact
pinned `IStrategy.advise_entry` and `advise_exit` methods. It covers simultaneous Long/Short
matches, assignment priority, literal replacement, compound appends, nullable masks, wrapper
initialization, and original trailing whitespace.

```bash
uv run python scripts/generate_tag_fixture.py
uv run pytest -q tests/test_tag_program.py tests/test_tag_fixture.py
nfi-bte strategy tag-program strategy.py --class Strategy --output tag-program.json
```

Dynamic configuration and loop lowering not yet represented by the source compiler fail closed
with an exact source location. They are not inferred from Signal IDs, source hashes, pairs, or
expected results.
