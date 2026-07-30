# Required CI and branch protection

The machine-readable policy is `.github/ci-contract.json`. The `CI` workflow always
runs a change-classification job and a stable aggregate job named `Required CI`.
Branch protection requires that aggregate name instead of matrix-generated names.

## Risk-tiered path policy

The classifier selects the cheapest lane that covers every changed path:

- `docs-only`: root documentation, `docs/`, `examples/`, and the two roadmap
  bookkeeping files in `planning/`; text validation and the aggregate check only;
- `policy-only`: documentation plus explicitly listed repository metadata and the
  required-CI implementation itself; a dependency-free policy self-test runs on
  Ubuntu;
- `code`: every unlisted path, runtime source, tests, schemas, fixtures, build
  inputs, and installers; the full Python matrix on Linux, Windows, and macOS,
  static/Rust checks, and native full-parity fixtures run.

Mixed changes escalate to the highest-risk lane. An empty diff, unavailable base
commit, or manual dispatch fails closed to `code`. `Required CI` checks that selected
jobs succeeded and every unselected expensive job was skipped.

Operational discovery configuration, release contracts, non-CI workflows, runtime
schemas, and fixtures are not covered by broad directory exceptions. They remain in
the `code` lane unless individually reviewed and listed.

The separate branch-discovery workflow keeps external-data policy in the mode-specific
JSON files. A declared provider HTTP restriction is recorded as deferred, does not
advance Native qualification, and is retried only for a new immutable identity or an
explicit manual request. Scheduled runs reuse that compact state instead of rebuilding
or downloading data. Unknown infrastructure failures still fail and remain visible.
Non-candidate artifacts expire after one day; raw candles, caches, container layers,
and traces are never uploaded.

Pull requests are the normal required-check surface. A protected merge is not tested
a second time on `main`; push-triggered CI is limited to version and product-release
contract paths that require a same-commit release check. Manual dispatch remains
available for an explicit full rerun.

Fast-lane acceptance requires `Required CI` to succeed while every unselected job is
reported as `skipped`. A documentation-only pull request must not start Python,
Rust, native parity, or operating-system matrix runners.

The workflow uses read-only repository contents, explicit job timeouts, and
per-PR/ref concurrency with older runs cancelled. It never uses
`pull_request_target`.

Documentation is intentionally excluded from the runtime regression manifest.
Runtime schemas, fixtures, evidence, CLI behavior, and release identities remain
sealed; editing prose does not create a stale hash that blocks the next code change.

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

## Protected long certification

`.github/workflows/certify-release-candidate.yml` is manual-only and targets the
protected `nfi-certification` self-hosted runner. The selected mode is part of a
non-cancelling concurrency key, while a host file lock guards the long command
itself. A versioned contract and protected host configuration generate the command;
strategy, pair, period, Oracle path, and expected result are not embedded in the
workflow.

The workflow can only reuse an exact, immutable Oracle-index match. It validates the
sealed input fingerprint, full Oracle tree seal, and run-report hash and offers no
new-Oracle fallback. Interrupted output requires explicit resume and remains subject
to the certification state machine's hash checks.

Certificate bundles use short-lived environment-scoped OIDC credentials and
content-addressed object keys. Conditional creation plus metadata and byte-count
verification prevents overwriting an existing immutable object. GitHub artifacts
carry the same certificate, reuse plan, and storage receipt for the release gate.

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
