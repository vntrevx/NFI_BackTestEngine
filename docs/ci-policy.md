# Required CI and branch protection

The machine-readable policy is `.github/ci-contract.json`. The `CI` workflow always
runs a change-classification job and a stable aggregate job named `Required CI`.
Branch protection requires that aggregate name instead of matrix-generated names.

## Path policy

A change is documentation-only only when every changed path is an explicitly listed
root documentation file or is below `docs/`. An empty diff, an unavailable base
commit, a manual dispatch, or any unlisted path is treated as a code change.

Documentation-only changes run the classifier, text validation, and aggregate check.
The Python, quality/Rust, and full-parity jobs must be skipped. Code changes run the
existing full Python matrix on Linux, Windows, and macOS, static and Rust checks, and
native full-parity fixtures. The aggregate check fails unless every selected job has
the expected result.

The workflow uses read-only repository contents, explicit job timeouts, and
per-PR/ref concurrency with older runs cancelled. It never uses
`pull_request_target`.

## Nightly boundary

`.github/workflows/nightly.yml` is scheduled and manually dispatchable, but is not
part of the pull-request required-check surface. It also has read-only repository
permissions and receives no secrets. Long release certification remains in its
separate self-hosted manual workflow.

The nightly fixture inventory is generated from the glob in
`.github/ci-contract.json`. Manifest identity and measured logical bytes drive a
deterministic greedy assignment across the configured shard count. No fixture name
is embedded in the sharding implementation. The generated matrix fails closed when
the inventory is empty or fixture IDs repeat.

Each shard retains one JSON outcome report plus command diagnostics. The final
`Deduplicated nightly result` job verifies that every discovered manifest occurred
exactly once, reports missing or duplicate assignments, and groups repeated failures
by a normalized fingerprint. Stateful resume/mutation/clean/report tests and a
pinned, network-disabled Docker reference smoke run execute in separate jobs, so
their trust and failure boundaries remain visible.

The matrix commands support a dry-run that generates all shard reports and the same
aggregate report without executing a fixture. This is the acceptance path for
workflow and inventory changes; scheduled runs execute every assigned fixture at
full parity.

## Main branch protection

The policy targets `vntrevx/NFI_BackTestEngine` branch `main` with:

- strict, up-to-date status checks;
- `Required CI` as the required status context;
- enforcement for administrators;
- no actor restrictions or review-count policy introduced by this task.

Before any API update, the current branch and protection responses are retained in
the task acceptance evidence. The post-update response is also retained and compared
to `.github/ci-contract.json`. Restoring the pre-change response or deleting the new
protection rule is the rollback path.
