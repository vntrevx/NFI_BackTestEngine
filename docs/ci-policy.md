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
