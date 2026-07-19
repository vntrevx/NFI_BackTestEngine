# NFI Backtest Engine product contract

## Mission

NFI Backtest Engine is a moving-target research accelerator for
NostalgiaForInfinity strategies. The strategy file supplied by the user is the input;
one historical NFI revision must never become the product boundary.

The normal workflow is:

1. inspect and hash the supplied NFI source;
2. compile every supported vector and callback operation from that exact source;
3. prepare four to five years of immutable candle and market data;
4. run the deterministic Rust engine as quickly as the current CPU and memory allow;
5. shortlist candidates;
6. rerun a finalist with official Freqtrade against the same sealed inputs;
7. accept the result only when the normalized trades and required state are exact.

Official Freqtrade remains the final oracle. The fast engine is not allowed to hide a
semantic mismatch behind numeric tolerances or an approximate fallback.

## Non-negotiable principles

### The current NFI source is always the target

- Every run hashes and analyzes the file supplied by the user.
- A known source revision is evidence, not an allowlist.
- Structurally supported changes are compiled without waiting for a new engine release.
- A genuinely new stateful behavior must stop before simulation with a precise blocker.
- Daily upstream compatibility CI makes such a blocker visible immediately.
- Expanding the structural compiler is preferred over adding another whole-file version
  switch.

No finite engine can promise exact execution of an arbitrary future Python program
before that program exists. The operational promise is stronger and testable: accept
new revisions automatically when their semantics fit the compiled contracts, and fail
closed with an actionable difference when they introduce a new contract.

### Long-horizon results are the default

- The setup wizard defaults to the previous five complete calendar years.
- Four years is the minimum duration for a release-grade performance claim.
- Short fixtures remain useful for debugging and branch evidence, but are labelled
  diagnostic-only.
- Timeranges are not silently split and concatenated. A split Freqtrade run resets
  wallet, open-trade, protection, and strategy state and therefore is not equivalent to
  one continuous run.

### Parallelism must preserve portfolio semantics

- Pair indicators and independent candidates may use multiple processes.
- Nested numeric libraries use one thread per worker.
- Shared wallet, slot, trade, order, and equal-timestamp decisions retain one global
  deterministic order.
- Docker reference work is budgeted from Docker daemon resources, not host resources,
  and is serialized when memory is the limiting resource.

### Evidence precedes claims

A public speed or full-support claim requires all of the following:

- the exact supplied strategy, config, data, market metadata, dependency versions, and
  Freqtrade image identity are sealed;
- at least 80 pairs and 1,460 days are measured in fresh processes;
- the median screening speed is at least 10x the official reference;
- the memory gate passes;
- the engine and official Freqtrade surfaces match exactly;
- the result is reproducible from retained artifacts.

## Release direction

The existing X7 v17.4.413 certificates remain regression evidence. They are not the
destination. Each release must also check the latest upstream X7 source and report
whether it is immediately executable or which new semantic contract still needs to be
lowered.
