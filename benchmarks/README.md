# Freqtrade benchmark fixture specification

The machine-readable contracts are:

- `benchmark-fixture.schema.json` for the original trade-only fixture;
- `benchmark-fixture-v2.schema.json` for trade plus exact state evidence;
- `trade-surface-v2.schema.json` for normalized result compatibility.

A fixture is one directory containing `manifest.json` and every relative file it
references.

## Evidence levels

- `contract-only` is synthetic and tests schemas/comparators only.
- `captured` is frozen from the pinned official Freqtrade reference and may support
  fixture-scoped correctness or performance evidence.

The repository includes two captured Freqtrade 2026.5.1 spot fixtures:

- `stops-only-spot-2025-01-01_04`;
- `normal-routing-spot-2025-01-01_04`.

Neither is representative 80-pair, four-year X7 evidence.

## Frozen inputs

A captured v2 fixture seals:

- exact strategy source and effective credential-free config;
- all base/detail/informative/funding/mark candle inputs used by its mode;
- frozen CCXT market metadata;
- pairlist or auxiliary inputs that can affect behavior;
- official raw ZIP result;
- normalized trade-surface-v2;
- full official state trace;
- compact common state projection for fast repeated full checks.

Every path is relative, cannot escape the fixture, and includes bytes plus SHA-256.
`.gitattributes` disables text conversion for the entire fixture tree.

The manifest also freezes Freqtrade version, Docker index and platform digest, exchange,
strategy, timerange, timeframes, modes, and argv as an array. A shell command string is
never stored.

## Validate a fixture

Strict validation checks schemas, boundaries, sizes, hashes, the normalized surface,
both trace streams, and trace-to-input bindings:

```powershell
uv run nfi-bte fixture validate path/to/manifest.json
```

`--skip-hashes` is diagnostic only.

## Capture procedure

1. Stage immutable inputs with `stage_fixture_v2`.
2. Capture exact CCXT markets through the pinned online reference.
3. Run official Freqtrade from the staged directory with explicit export output.
4. Normalize the export as trade-surface-v2.
5. Finalize through `finalize_fixture_v2`.

Finalization validates the full trace and automatically creates/seals the compact common
state projection.

The official runtime command for an already captured fixture is:

```powershell
uv run nfi-bte reference run path/to/manifest.json `
  --output-dir benchmarks/work/reference-proof `
  --trace full
```

Docker networking is disabled during the run. Frozen market metadata is injected into
Freqtrade so result truth does not depend on changing exchange metadata.

## Engine verification

Quick final-result parity:

```powershell
uv run nfi-bte engine fixture path/to/manifest.json `
  --output-dir benchmarks/work/engine-quick `
  --level quick
```

Full every-candle parity:

```powershell
uv run nfi-bte engine fixture path/to/manifest.json `
  --output-dir benchmarks/work/engine-full `
  --level full
```

The comparator fails on the first semantic difference. A failure retains a prefix-bounded
`mismatch-replay` directory.

## Same-fixture performance gate

```powershell
uv run nfi-bte performance path/to/manifest.json `
  --output-dir benchmarks/work/performance `
  --profile .nfi/execution-profile.json `
  --level full `
  --runs 1
```

The report includes:

- separate fresh CLI wall times;
- process-tree pipeline RSS;
- Rust core peak RSS and Docker cgroup peak memory;
- hardware and execution-profile identity;
- engine and official exact-parity reports;
- observed speed ratio and memory gate;
- automatic representative versus diagnostic-only scope.

Every speed statement requires a fresh report from the identical sealed fixture. Console
trade counts and nearby historical timings are not evidence.

## Phase 0 profiling

The official tracer aggregates exactly four categories:

- `indicators`;
- `callbacks`;
- `trade_scans`;
- `event_simulation`.

The benchmark is incomplete if any category is missing. Profiling output is aggregated
at the end of the run; it does not perform per-callback file I/O.
