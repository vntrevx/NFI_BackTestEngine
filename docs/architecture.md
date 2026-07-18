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
Polars batched signal arrays
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
core for the host, caps working memory, and derives independent engine/reference job
counts. The global simulator remains intentionally single-threaded for deterministic
shared-state order; it is small enough that independent jobs are the useful CPU
parallelism boundary.

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

## Explicitly unsupported

The engine fails before simulation instead of approximating:

- arbitrary NFI X7 hot callback bodies;
- short trades and futures mode;
- leverage changes, funding, mark prices, and liquidation;
- partial exits, derisk/grind routing beyond the contract rule;
- protections, pair locks, dynamic pairlists, and live exchange behavior;
- runtime `eval`, `exec`, dynamic imports, hot-path dynamic attributes;
- negative shifts or centered rolling windows that can introduce lookahead.

X7 AST preflight is implemented, including exact diagnostics and timeframe discovery.
That does not mean X7 execution parity is complete.

## Performance claims

The performance gate always measures the engine and official reference against the same
sealed manifest and fresh processes. It records pipeline wall time, process-tree RSS,
Rust `/usr/bin/time` peak RSS, Docker cgroup peak memory, hardware, and parity.

A measured ratio is labeled `diagnostic-only` unless the fixture contains at least 80
pairs and 365 days. Build/compilation time is an installation concern and is recorded
separately from the installed execution pipeline.
