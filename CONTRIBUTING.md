# Contributing

Keep semantic changes small and prove them against an exact fixture before claiming a
speed or parity improvement.

Run:

```text
uv sync --extra dev --frozen
uv run maturin develop --locked
uv run pytest -q
uv run ruff check .
uv run basedpyright --level error python/nfi_backtest_engine
cd rust
cargo fmt --all -- --check
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
```

New simulator behavior needs a focused Rust unit test and an official Freqtrade captured
fixture before it can be marked certified. Unsupported behavior must fail before
simulation; do not add approximate fallbacks to the exact lane.
