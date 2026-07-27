# Evidence-aware clean dry-run

`nfi-bte clean` currently implements classification and auditing only. It has no
deletion path. The `--dry-run` flag is mandatory:

```bash
nfi-bte clean --dry-run
```

The managed root must be a real directory named `.nfi`. A different directory,
a symlinked root, an audit path outside that root, or any nested symlink that resolves
outside the root is rejected before the audit is written.

The default audit is `.nfi/clean-audit.json`. A different destination must still be
inside the selected root:

```bash
nfi-bte clean --dry-run \
  --root work/.nfi \
  --output work/.nfi/audits/clean.json
```

## Classification

The report groups each top-level managed entry into one of these categories:

- active run and checkpoint
- release certificate and evidence bundle
- official Oracle and Freqtrade ZIP
- explicitly or conservatively preserved run
- regenerable vector and cache data
- interrupted or failed run
- temporary Arrow and Docker spool
- old build and calibration data
- unclassified protected data

Every category reports file count, logical bytes, allocated bytes, and reclaimable
bytes. Every entry includes its protection reason. Protected evidence includes
path, byte size, and SHA-256 identity; a certification-like directory with incomplete
identity remains protected and raises a fail-closed issue.

Create an empty `.nfi-preserve` or `.nfi-keep` marker inside a run to preserve it, or
select it explicitly:

```bash
nfi-bte clean --dry-run --preserve results/important-run
```

Relative `--preserve` paths are resolved from the managed `.nfi` root. Paths outside
that root are rejected.

## Activity guards

PID files and held lock files are inspected inside each managed entry. By default the
command also queries running `nfi-*` user services and Docker containers carrying the
engine's managed label. Active PIDs, locks, services, or containers block associated
reclamation.

`--no-runtime-probes` exists for isolated diagnostics, but it does not weaken the
verdict: reclaimable entries remain protected and the audit is marked fail-closed
because service and container activity is unknown.

The JSON field `safety.deletion_performed` is always `false`. A future deletion
implementation requires a separate roadmap task and is not implied by a dry-run
classification.
