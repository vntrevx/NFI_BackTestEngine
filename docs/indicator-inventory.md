# Indicator Operation Inventory

`indicator-operation-inventory-v1` is the source contract used before compiling
NFI indicator code into Native Vector IR. It parses the supplied strategy without
importing or executing it.

```bash
nfi-bte strategy indicator-inventory NostalgiaForInfinityX7.py \
  --class NostalgiaForInfinityX7 \
  --upstream-repository https://github.com/iterativv/NostalgiaForInfinity.git \
  --upstream-commit <40-character-commit> \
  --output .nfi/indicator-inventory.json
```

The report is bound to the exact source SHA-256 and contains:

- the reachable call graph from `populate_indicators` and `informative_pairs`;
- strategy methods, module helpers, callable parameters, and local lambda helpers;
- every reachable TA-Lib, qtpylib, NumPy, pandas, rolling, and primitive operator;
- literal and dynamic dataframe column access;
- lookback arguments and conservative causality classification;
- library-owned NaN behavior that later exact fixtures must capture;
- informative timeframes, dataframe requests, merges, and source-ordered fill calls;
- a family coverage matrix and a path-independent deterministic fingerprint.

An absent family is recorded as `present=false`; it is not silently omitted. Unknown
call targets remain `unresolved`, which makes `inventory_complete=false`. Deep source
expressions that exceed Python's recursive unparser are represented by a deterministic
structural SHA rather than truncated or executed.

This inventory is not a Native-support claim. `native_status=inventory-only` means the
operation is required by the observed source but still needs a typed opcode, an exact
kernel, and independent Python/TA-Lib comparison in later M20 tasks. The upstream commit
is evidence identity only and is never used for runtime dispatch.

## Typed indicator program

`indicator-program-v1` is the causal execution contract built from a supported source
subset. It uses generic nodes for parameters, column reads and writes, arithmetic,
comparisons, selection, TA-Lib/qtpylib calls, rolling/shift/EWM, helper calls, and
informative merges.

```bash
nfi-bte strategy indicator-program NostalgiaForInfinityX7.py \
  --class NostalgiaForInfinityX7 \
  --output .nfi/indicator-program.json
```

Every node has a value type, source-order index, source location, inputs, and causal
lookback. Function and node IDs are structural data; strategy and Signal names never
become opcodes. Negative shifts, centered rolling windows, backward fill, dynamic
window sizes, and prefilled informative merges fail closed. An unsupported source
construct must be generalized deliberately before it can enter the Native lane.
