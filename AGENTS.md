# Repository Guidelines

## Project Structure & Module Organization

Python orchestration, contracts, schemas, and CLI code live in
`python/nfi_backtest_engine/`. The Rust workspace is under `rust/`; its main crates
are `nfi-sim-core`, `nfi-vector-core`, `nfi-vector-io`, `nfi-sim-cli`, and the PyO3
bridge `nfi-py`.
Python tests are in `tests/`, with parity-focused cases in `tests/parity/`. Captured
fixtures and published evidence belong in `benchmarks/fixtures/` and
`benchmarks/evidence/`. Keep user documentation in `docs/` and roadmap state in
`planning/`. Treat `.nfi/`, `artifacts/`, `dist/`, and `rust/target/` as generated
data unless a task explicitly requires preserved evidence.

## Build, Test, and Development Commands

```bash
uv sync --extra dev --frozen
uv run maturin develop --release --locked
uv run pytest -q
uv run ruff check .
uv run basedpyright --level error python/nfi_backtest_engine
cd rust
cargo fmt --all -- --check
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
```

The first two commands install locked dependencies and build the native extension.
Run the full Python and Rust suites before opening a PR. Use `nfi-bte engine fixture
<manifest> --level full` when changing simulation behavior or parity-sensitive code.

## Coding Style & Naming Conventions

Use four-space indentation, type hints, `snake_case` functions, and `PascalCase`
classes in Python. Ruff enforces imports and a 100-column limit. Rust must remain
`rustfmt`-clean, use standard Rust naming, pass pedantic Clippy checks, and contain no
`unsafe` code. Keep modules responsibility-focused. Never add runtime branches keyed
to a strategy name, pair, timerange, SHA, or expected result; unsupported exact-lane
behavior must fail closed.

## Testing Guidelines

Pytest files and functions follow `test_*.py` and `test_*`. Add Rust `#[test]` cases
beside the responsible module. New simulator semantics require a focused unit test
and an official captured Freqtrade fixture. Parity comparisons are zero tolerance:
prove both trade-surface and full-state equality. Preserve existing certificates,
Oracle exports, release assets, and golden evidence.

## Commit & Pull Request Guidelines

Follow the existing concise prefixes: `feat(scope):`, `fix(scope):`, `refactor:`,
`docs:`, `chore:`, and `release:`. Keep commits small and separate refactors from
optimizations. PRs should explain intent, behavior risk, validation commands, and any
performance or parity evidence; link an issue when one exists. Runtime changes must
pass Required CI on Linux, Windows, and macOS before merge. Documentation-only and
CI-policy-only changes use the risk-tiered checks in `docs/ci-policy.md`.

## Security & Repository Safety

Never commit exchange credentials, API keys, live-trading secrets, or untracked user
files. Inspect cleanup with `nfi-bte clean --dry-run` before applying it. For roadmap
work, use `planning/roadmap-state.json` and `planning/acceptance-commands.json` as the
authoritative sources.
