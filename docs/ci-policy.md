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
- `automation-only`: the allowlisted NFI compatibility routing, issue reconciler,
  workflow, and focused tests; one Ubuntu job runs only their tests, lint, and type
  check;
- `code`: every unlisted path, runtime source, tests, schemas, fixtures, build
  inputs, and installers. Ordinary pull requests receive a second, fail-closed
  affected-path plan instead of automatically starting every code job.

The affected-path plan always runs the Ubuntu 3.12 Python lane and Python quality
checks for Python runtime changes. Python runtime changes also run the two native
parity fixtures. Rust changes add Rust format, test, and Clippy checks; AST-sensitive
indicator/strategy changes add the Ubuntu 3.13 and 3.14 identity lanes; and the
allowlisted platform-boundary files add Windows and macOS 3.12. General Python tests
run the Ubuntu 3.12 Python and quality lanes, while parity fixtures and reference
inputs also add native parity.

Build-identity changes (`pyproject.toml`, `uv.lock`, the Rust workspace manifests and
lockfile, or the Rust toolchain file), an empty or unavailable diff, manual dispatch,
and every unknown path fail closed to the full five-entry Python matrix, Python and
Rust quality jobs, and native parity. Mixed changes take the union of capabilities.
`Required CI` authenticates the emitted plan by recomputing it from the changed paths,
then requires every selected job to succeed and every unselected conditional job to
be skipped. Timing validation uses the exact report identities selected by that same
plan rather than requiring evidence from jobs that intentionally did not run.

Operational discovery configuration, release contracts, non-CI workflows, runtime
schemas, and fixtures are not covered by broad directory exceptions. They remain
fail-closed unless an exact affected-path rule covers them.

The separate branch-discovery workflow keeps external-data policy in the mode-specific
JSON files. A declared provider HTTP restriction is recorded as deferred, does not
advance Native qualification, and is retried only for a new immutable identity or an
explicit manual request. Scheduled runs reuse that compact state instead of rebuilding
or downloading data. Unknown infrastructure failures still fail and remain visible.
Non-candidate artifacts expire after one day; raw candles, caches, container layers,
and traces are never uploaded.

Blocked generic semantics are retained in the append-only compatibility ledger and
one automatically reconciled `nfi-compatibility` issue. They do not create evidence-
only Draft PRs. Pull requests are reserved for a compact independently exact fixture
candidate or an implementation change, so a scheduled watcher cannot grow the PR
queue. The removed publisher and test paths remain explicit classification tombstones
so the deletion commit itself also stays on this focused automation lane.

Pull requests are the normal required-check surface. A protected merge is not tested
a second time on `main`; push-triggered CI is limited to version and product-release
contract paths that require a same-commit release check. Manual dispatch remains
available for an explicit full rerun.

Fast-lane acceptance requires `Required CI` to succeed while every unselected job is
reported as `skipped`. A documentation-only pull request must not start Python,
Rust, native parity, or operating-system matrix runners.

Rust compilation in the Python native-build, Rust-quality, and parity jobs uses the
pinned `sccache` v0.10.0 GitHub Actions backend. Release wheel builds enable the same
compiler-object cache through the pinned maturin action; source-distribution builds
do not. The cache implementation literal is part of timing evidence and candidate
identity, so stale or differently configured evidence is rejected. Final wheels,
source distributions, parity results, timing reports, and release evidence are never
restored from this compiler cache.

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
