# Managed storage and safe cleanup

NFI Backtest Engine keeps reusable state under `.nfi`. New wizard-created runs use
`.nfi/runs/<strategy>-<timerange>`; an existing saved project keeps its recorded output
path unchanged.

The content cache is bounded without using strategy-, pair-, date-, SHA-, or
result-specific rules. Its default ceiling is the smallest of:

- 50 GiB
- 10% of the cache filesystem
- 25% of currently available space

Set an exact positive byte ceiling with `NFI_BTE_CACHE_MAX_BYTES`. Code embedding the
engine can instead pass `max_bytes` to `ContentCache`; an explicit value takes
precedence over the environment.

## Audit first

The dry run classifies storage and never deletes files:

```bash
nfi-bte clean --dry-run
```

The managed root must be a real directory named `.nfi`. A different directory,
symlinked root, output outside the root, escaping nested symlink, or special
filesystem entry is rejected or protected.

The default audit is `.nfi/clean-audit.json`. A custom destination must remain inside
the managed root:

```bash
nfi-bte clean --dry-run \
  --root work/.nfi \
  --output work/.nfi/audits/clean.json
```

Logical bytes count path names. Allocated bytes count each `(device, inode)` once, so
hard-linked vectors are not multiplied. Allocated bytes are reported as reclaimable
only when every filesystem link to that inode was observed inside selected deletion
units. A hard link in a protected run or outside `.nfi` therefore contributes zero
expected physical reclamation.

## Apply safely

Apply mode does not consume an old audit. It acquires a cleanup lock, creates a fresh
audit with runtime probes, and deletes only entries that fresh audit marks deletable:

```bash
nfi-bte clean --apply
```

The default selection includes only:

- regenerable vector/cache data
- failed, interrupted, or incomplete runs
- temporary Arrow/Docker spool data
- rebuildable build/calibration data

Completed runs are protected unless selected explicitly:

```bash
nfi-bte clean --apply --include-completed
```

Use `.nfi-preserve` or `.nfi-keep` inside a run, or repeat `--preserve`, to override
that selection:

```bash
nfi-bte clean --apply --include-completed \
  --preserve runs/important-result
```

Release certificates, evidence bundles, Oracle data, and ZIP archives remain
protected even with `--include-completed`. Unclassified data is protected.
Incomplete certification identity is reported as an issue and its entire unit stays
protected; it does not make an unrelated, fully classified cache unit deletable or
block that cache unit's reclamation.

Before each deletion the root identity, target boundary, symlink status, and special
filesystem entries are checked again. The audit or result path cannot be placed
inside a selected target. Cleanup stops on the first validation or filesystem
failure.

Every apply writes:

- `.nfi/clean-audit.json`: the exact fresh selection and measured byte accounting
- `.nfi/clean-result.json`: audit SHA-256, selection, deleted units, estimated
  physically reclaimed bytes, and complete or partial-failure status

Both paths can be changed with `--output` and `--result`, but must remain inside the
managed root. Deletion is not reversible; regenerate cache/vector data or rerun a
deleted result when needed.

## Activity guards

PID files and held lock files are inspected per unit. The command also checks managed
Docker containers and running `nfi-*` user services whose process or working
directory owns the selected `.nfi` workspace. A power-policy or other unrelated
service does not block cleanup merely because its name starts with `nfi-`.

An active or unknown managed runtime makes the audit fail closed and prevents apply.
`--no-runtime-probes` is diagnostic dry-run only and cannot be combined with
`--apply`.
