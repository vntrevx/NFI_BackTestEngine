# Host certificate release gate

The release gate binds a build-once candidate to a successful Full X7 host
certificate before any release is published. It does not build wheels, run an Oracle,
or publish a GitHub release.

```bash
nfi-bte release gate \
  --candidate-dir candidate \
  --certificate host/full-x7-futures-certification.json \
  --certificate-evidence host/full-x7-futures-certification-evidence.zip \
  --platform-evidence candidate/full-x7-futures-platform-evidence.json \
  --candidate-commit "$CANDIDATE_COMMIT" \
  --output-dir release
```

The command rejects the candidate unless all of these identities agree:

- `SHA256SUMS.txt` covers every original candidate asset and every digest verifies;
- the host report is a successful v2 Full X7 certificate, not a failed or preview
  report;
- its evidence ZIP contains exactly the same certificate document;
- the certificate wheel SHA identifies exactly one prebuilt candidate wheel;
- the certificate package version matches that wheel;
- the certificate portable-package SHA matches the sealed platform evidence;
- Linux x86_64/aarch64, macOS arm64, and Windows WSL2 x86_64 records are all present;
- one platform wheel SHA equals the host-certified wheel SHA;
- the platform evidence itself was already sealed by the candidate checksum manifest.

On success, the command copies the unchanged candidate and certificate assets to a
new output directory, writes `release-gate.json`, and writes
`RELEASE-SHA256SUMS.txt`. The original `SHA256SUMS.txt` stays byte-for-byte unchanged;
the release manifest additionally covers the host certificate, its evidence archive,
and the gate record.

## Workflow binding

`Publish release candidate` requires both the build run ID and a certificate run ID.
The certificate run is produced by the manually dispatched `Certify release
candidate` workflow on a protected runner labelled `nfi-certification`. It accepts
one protected host-side JSON file containing the selected mode, `release_lock`,
`execution_profile`, `strategy`, `strategy_class`, `config`, `data_directory`,
`engine_markets`, optional `reference_markets`, an Oracle index and fingerprint,
a host lock path, and a non-empty `state_probes` array. This keeps machine-specific
data, Oracle, strategy, pair, date, and output locations out of the workflow.

The planner derives a fingerprint from the sealed lock, strategy, config file, data,
market snapshots, and pinned reference image. Exactly one immutable Oracle-index
record must match that fingerprint and mode. Its run report and complete directory
tree are hash-checked before the generated command receives `--official-oracle`;
there is no fallback that starts a new official run. The persistent output directory
may be reused only with explicit `resume=true`, after which the certification
command's existing checkpoint identity validation still applies.

GitHub concurrency permits one job per mode with cancellation disabled. A
self-hosted `flock` guards the actual certification command against another process
on the host. The job uses the `full-x7-certification` environment and short-lived
OIDC credentials. Its certificate bundle is stored under a
mode/candidate/content-SHA key using a conditional create, so a conflicting object
cannot be overwritten. GitHub retains the same certificate plus the Oracle-reuse
plan and immutable-storage receipt for the publishing gate.

Before upload, that workflow downloads the exact candidate-run artifact, installs its
Linux wheel, runs Full X7 certification, and applies the same release gate against the
candidate's sealed platform evidence. The certificate run must then:

- be a successful `workflow_dispatch` run named `Certify release candidate`;
- have the exact same `head_sha` as the build run;
- publish an artifact named `host-certificate-COMMIT_SHA`;
- place exactly one `*certification.json` and one matching
  `*certification-evidence.zip` or `*certification-bundle.zip` in that artifact.

The publishing workflow installs the candidate wheel itself and runs its `release
gate` command. It publishes only the newly sealed output, downloads the resulting RC,
and byte-compares every file.

Stable promotion independently verifies both checksum manifests and requires
`release-gate.json` to say `release_certified=true`,
`status=release_certified`, and the same candidate commit before creating a stable
release. A missing certificate, failed/preview certificate, mismatched wheel, missing
platform, or mismatched commit therefore stops before the publish action.

Only the explicit publish and promote workflows retain `contents: write`; the
build-candidate workflow remains `contents: read`.
