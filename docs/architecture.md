# Architecture and semantic boundary

## Proof boundary

This engine accelerates research. Official Freqtrade remains the source of truth for a
final candidate.

A result is supported only when all of these identities are sealed:

- strategy, effective config, candles, market metadata, and auxiliary input SHA-256;
- exact Freqtrade version and Docker image index/platform digests;
- trading mode, margin mode, timerange, timeframe, and command argv;
- normalized trade-surface schema version;
- reference state-trace input, strategy, and config hashes.

Synthetic contract fixtures test tooling only. Performance and parity evidence comes
from `evidence_status: captured` fixtures.

## End-to-end data flow

```text
strategy.py + config + candles
          |
          v
AST preflight + frozen input/data identity
          |
          v
SHA-bound Feather vectors + projected callback columns
          |
          v
Rust global chronological portfolio loop
          |
          +------> Freqtrade-compatible trade-surface-v2 ------> quick exact check
          |
          +------> compact every-candle JSONL state
                              |
                              v
                 common canonical state trace -----------------> full exact check
```

Python is not called once per candle. Python owns preparation, columnar transforms,
reports, and proof artifacts. Rust owns mutable trade/order/wallet state and the hot
chronological loop.

The compact vector manifest stores a relative Feather path, file SHA-256, row count,
feature names, pair limits, precision, and historical price-step changes. Rust
canonicalizes the path below the manifest directory and verifies the file hash before
trusting Arrow schema metadata. Required feature projections are derived from the
immutable scalar-program arenas at runtime; an input cannot provide a wider projection.

## Global event model

The Rust core keeps one cursor per pair and repeatedly selects the smallest next
timestamp. All pairs at that timestamp are processed in stable whitelist order.

For each pair event it:

1. checks deterministic entry eligibility and shared slot/wallet availability;
2. updates an existing trade's extrema and position-adjustment rule;
3. evaluates stop, explicit exit, and timed custom exit in the supported order;
4. fills market orders with frozen amount/price precision and fee rules;
5. recomputes the dry-run wallet from realized profit and tied-up stake;
6. emits one compact state after the Freqtrade-visible candle.

The first dataframe row is reserved for shifted signals. The final candle is observed
before the separate force-exit pass, matching the reference fixture.

Pairs are never simulated independently and merged. That would change slot
competition, shared stake, rebuy timing, rejected signals, and equal-timestamp order.

## Safe parallelism

Parallel work is limited to operations without shared mutable portfolio state:

- indicators by pair;
- independent strategies, timeranges, fixtures, and candidate jobs;
- offline scoring and report generation.

The hardware profile detects physical/logical CPU and available RAM, reserves a physical
core for the host on ordinary multicore systems, reserves 15% of total memory within
bounded limits, and derives independent engine/reference/research process counts.
Research jobs and pair workers additionally use a conservative 3 GiB
per-indicator-process memory assumption, which can be replaced with a measured value.
Spawned workers set Polars, Rayon, OpenMP, OpenBLAS, and MKL nesting to one thread.
The global simulator remains intentionally single-threaded for deterministic
shared-state order; independent candidates are the useful simulator parallelism
boundary.

Host-native work and container work are separate resource domains. The host profile
controls Python pair workers and the Rust engine. Docker workloads instead inspect the
daemon's own `MemTotal`, CPU count, architecture, and cgroup-limit support. The container
policy reserves 20% of daemon memory within 1-6 GiB bounds, caps the one active managed
container to the remaining budget after subtracting live usage reported by every other
container, and disables additional managed Docker concurrency with an operating-system
file lock. This applies uniformly to Docker Desktop VMs and native Linux daemons rather
than detecting one laptop model.

Every managed container receives an ownership and role label plus a Docker CID file.
The exact CID is force-removed in a `finally` boundary after completion, interruption,
or timeout. Before a new workload starts, only stopped containers with the ownership
label are removed; unrelated containers are never pruned, and an existing running
managed container blocks a second workload.

On the development host visible to WSL (5 physical cores, 10 logical CPUs, 27.3 GiB
RAM), the automatic profile selected four independent research processes. A four-job
annual X7 vector-preparation diagnostic used four distinct worker PIDs, completed in
35.67 seconds versus 106.93 aggregate job-seconds, and therefore observed 3.00x
effective parallelism and 75% four-process efficiency. This is host-specific diagnostic
evidence, not the public 80-pair performance certificate.
The raw boundaries and timings are pinned in
[`benchmarks/evidence/host-scaling-x7-prepare-2026-07-19.json`](../benchmarks/evidence/host-scaling-x7-prepare-2026-07-19.json).

## Exact trade surface

`trade-surface-v2` preserves:

- source trade and order order;
- pair, long/short direction, open/close timestamps and rates;
- amount, stake, maximum stake, leverage, tags, and exit reason;
- open/close fees, funding, profit, liquidation, and stop state;
- every entry, adjustment, and exit order;
- starting/final balance, volume, rejected signals, timeouts, and maximum concurrency.

Financial values are finite canonical decimal strings. The comparator uses no epsilon
and fails on the first deterministic JSON path.

## Full state surface

The official tracer records detailed callback and candle state. A smaller sealed
projection is used for fast repeated full checks:

- free quote balance and non-zero base balances;
- open and closed trade counts;
- cumulative realized profit;
- rejected signal count;
- trade and order ID counters.

Both sides use the same event key:

`sequence + timestamp_ms + phase + pair + callback`

Each state and event is BLAKE3 hashed in a length-framed canonical binary stream. The
comparator validates framing, canonical encoding, event hashes, stream hash, and then
returns the first differing event and field using bounded memory.

## Mismatch replay

On the first mismatch the runner writes:

- a simulation input trimmed through the mismatched candle/trade;
- the first expected and actual trade fragment, when applicable;
- the first expected and actual state event, when applicable;
- a machine-readable difference and reproduction command;
- byte counts and SHA-256 for every replay artifact.

No full-year trace must be loaded merely to inspect one failure.

## Official reference lane

The reference runner uses Freqtrade `2026.5.1` and a pinned linux/amd64 image digest.
Market metadata is captured once and injected offline with Docker networking disabled.
The low-overhead tracer aggregates indicators, callbacks, trade scans, and event
simulation and writes only final JSONL aggregates.

The macOS application wheel and fast engine are native Apple Silicon builds. The
official fixture reference remains the canonical linux/amd64 digest and may be emulated
by Docker Desktop on an arm64 Mac. It is not silently replaced with an arm64 image:
another platform digest becomes an exact reference only after it has its own captured
identity and parity evidence.

Reference reports include daemon resources, the enforced container budget, cgroup peak
memory, `memory.events`, and a bounded memory verdict. The scheduler does not
automatically concatenate timerange chunks because Freqtrade wallet, open-trade,
protection, and strategy state reset at each independent invocation.

`quick` compares the complete final trade surface. `full` additionally runs the official
full trace and compares every common candle state.

## Currently supported simulator subset

The included captured spot fixtures prove:

- one global pair-ordered event loop;
- spot-long market entry and force exit;
- fixed intrabar stop loss;
- one positive position adjustment/rebuy rule;
- timed custom exit;
- exchange amount and price precision;
- open/close fees;
- wallet, slots, rejected signals, trade IDs, and order IDs.

The stops fixture has two exact trades and 861 exact common state events. The
normal-routing fixture has six trades, three rebuys, timed exits plus force exit, and 859
exact common state events.

The source-bound X7 adapter additionally executes:

- managed long exits and shared system-v3.2 adjustment for 57 tags across normal,
  pump, quick, rebuy, high-profit, rapid, top-coins, and scalp profiles;
- managed short-rebuy tags 561-563 with ordered short exits and adjustment;
- the separate rebuy entry/de-risk ladder before its source-defined transition
  into the shared grind-v3 adjustment;
- ordered per-pair target-cache mutation for mixed supported tags;
- custom stake, entry/exit confirmation, lifecycle no-op, and order-filled writes;
- the tag-120 spot/backtest legacy grind state machine, including source-ordered
  de-risk, six grind levels, partial exits, stops, and the `d1` buyback cycle;
- constrained isolated-futures accounting with uniform source-compiled leverage,
  long/short direction, funding events, fees, mark-price transport, precision,
  and position-adjustment order replay.

The annual APE/USDT:USDT futures certificate covers 2022-04-01 through 2023-01-01
and exactly matches Freqtrade's final normalized surface: 11 trades, 164 orders,
142 adjustment orders, one short trade, and eight funded trades. It reaches derisk
levels 1-3 and grind levels 1-5. The APE spot top-coins path, a separate tag-62 rebuy
exit, and a ZEC tag-120 grind path also have captured official final-surface
certificates. The rebuy fixture does not reach an adjustment order. The ZEC fixture
reaches `gm0`, `gd1`, and `gd2`; deeper legacy branches have source hashes and focused
native tests, not a branch-reaching Freqtrade differential certificate.

## Explicitly unsupported

The engine fails before simulation instead of approximating:

- live-only tag-120 partial-fill retry and dormant tag-121 entry/regular-mode adjustment;
- per-entry futures leverage when source branches are not uniform;
- liquidation-event parity outside the annual no-liquidation certificate;
- protections, pair locks, dynamic pairlists, and live exchange behavior;
- arbitrary NFI strategy shapes or X7 source revisions that do not match the compiled
  source and callback fingerprints;
- runtime `eval`, `exec`, dynamic imports, hot-path dynamic attributes;
- negative shifts or centered rolling windows that can introduce lookahead.

X7 AST preflight and constrained spot/futures callback compilation are implemented.
The annual APE certificate proves one combined futures event sequence; it does not
mean arbitrary X7 execution parity is complete.

## Performance claims

The performance gate always measures the engine and official reference against the same
sealed manifest and fresh processes. It records pipeline wall time, process-tree RSS,
Rust `/usr/bin/time` peak RSS, Docker cgroup peak memory, hardware, and parity.

A measured ratio is labeled `diagnostic-only` unless the fixture contains at least 80
pairs and 365 days. Build/compilation time is an installation concern and is recorded
separately from the installed execution pipeline.
