# Verification state ledger

The verification ledger preserves strategy and run history without rewriting a
previous success. It is separate from the research-run registry: the registry is a
current-state index, while the ledger is an append-only audit trail.

## States

Strategy revision records use:

- `latest_checked`
- `quick_verified`
- `release_certified`

Run records use:

- `prepared`
- `native_complete`
- `quick_verified`
- `release_certified`
- `blocked_unsupported_semantics`
- `failed`

Every record carries an `outcome`. A failed compatibility check is appended as a new
`latest_checked` event with `outcome=failure`; it does not remove the most recent
successful `quick_verified` or `release_certified` event.

## Identity

The record schema requires one explicit fingerprint object. Unknown values are `null`;
they are not guessed. A `release_certified` event is rejected unless every release
identity is present, including source and IR hashes, config and ordered pairlist,
sealed data and market snapshot, mode and timerange, Freqtrade image identities, and
package, wheel, and native binary hashes.

The ledger stores no credentials or free-form metadata. Evidence entries contain only
a kind, location, byte size, and SHA-256. Both the fingerprint and complete event have
canonical SHA-256 identities.

## Append-only enforcement

SQLite triggers reject every `UPDATE` and `DELETE` on verification records. Successful
states cannot move backward for the same subject and fingerprint. A changed
fingerprint starts a new state chain, while the old records remain queryable.
Re-appending the byte-identical event is idempotent.

## CLI and derived report

Append a compatibility preflight as `latest_checked` while preserving the exact
upstream identity supplied by the caller:

```bash
nfi-bte strategy check path/to/Strategy.py \
  --output artifacts/compatibility.json \
  --verification-ledger .nfi/verification-ledger.sqlite \
  --upstream-repository https://github.com/iterativv/NostalgiaForInfinity.git \
  --upstream-commit "$UPSTREAM_COMMIT" \
  --strategy-version "$STRATEGY_VERSION"
```

The repository, commit, and version options are recorded as identity fields; none is
inferred from a strategy name, path, or current Git checkout. An omitted value remains
`null`. Supplying `--output` also binds the durable compatibility report into the
event's evidence list by path, byte size, and SHA-256.

Attach the ledger to the existing run listing without changing its default output:

```bash
nfi-bte runs list --verification-ledger .nfi/verification-ledger.sqlite
```

The terminal prints separate `Latest checked`, `Quick verified`, and
`Release certified` lines. JSON mode returns the run list and the same projection as
separate fields.

To add the projection to a regenerated result presentation:

```bash
nfi-bte report artifacts/run \
  --verification-ledger .nfi/verification-ledger.sqlite
```

This writes `verification-status.html` as a derived file. It does not modify the
ledger, `run.json`, trade surface, official ZIP, or any certificate.
