# Native Signal Program

`signal-program-v1` is the versioned contract between NFI's Python signal source and the
Native vector runtime. It compiles source; it does not execute strategy Python.

## Exact boundary

The compiler processes `populate_entry_trend` and then `populate_exit_trend`, matching
Freqtrade 2026.5.1. It records every full-column or masked `.loc` write as an ordered
`frame-write` node. Each node depends on the prior DataFrame version, so overlapping masks,
read-after-write, and last-write-wins behavior cannot be reordered by an optimizer.

M21-01 owns only the four raw numeric columns:

- `enter_long`
- `enter_short`
- `exit_long`
- `exit_short`

Signal values remain raw through this stage. Freqtrade opens a new position only when the
corresponding value is exactly numeric `1`; arbitrary nonzero values are not promoted to
orders. Same-candle long/short/exit conflicts are resolved later by the Freqtrade-compatible
simulation kernel, never by the compiler.

Tag initialization, literal and compound tag generation, and original whitespace belong to the
separate [`tag-program-v1`](native-tag-program.md) contract. A tag write encountered by the
signal-only compiler still fails closed instead of being discarded or guessed.

## Evidence and regeneration

The committed oracle at
`benchmarks/reference/signals/freqtrade-2026.5.1.json` executes the exact pinned
`IStrategy.advise_entry` and `advise_exit` source around the compact contract strategy. It
covers entry-to-exit dependencies, nullable Boolean masks, overlapping writes, all four
directions, and raw dtype preservation.

```bash
uv run python scripts/generate_signal_fixture.py
uv run pytest -q tests/test_signal_program.py tests/test_signal_fixture.py
nfi-bte strategy signal-program strategy.py --class Strategy --output signal-program.json
```

Unsupported masks, assignments, phase-crossing writes, and dynamic behavior stop with a
source location. Signal numbers, strategy names, pairs, timeranges, and source hashes are
never runtime branches; the source hash is evidence identity only.
