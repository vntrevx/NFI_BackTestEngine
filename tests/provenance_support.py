from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.release_provenance import (
    ProvenancePolicy,
    workload_identity,
    write_signed_platform_provenance,
)

TEST_KEY_ID = "test-release-ed25519"
TEST_PRIVATE_KEY = Ed25519PrivateKey.generate()
TEST_PUBLIC_KEY = TEST_PRIVATE_KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
TEST_POLICY = ProvenancePolicy(
    policy_id="test-github-release-dsse-v2",
    repository="example/NFI_BackTestEngine",
    repository_ref="refs/heads/main",
    workflow="Build release candidate",
    workflow_ref=".github/workflows/release.yml@refs/heads/main",
    job="provenance-signing",
    keys={TEST_KEY_ID: TEST_PUBLIC_KEY},
)
TEST_COMMIT = "1" * 40
TEST_CANDIDATE_ID = "2" * 64
TEST_CHALLENGE = "3" * 64
TEST_BUNDLE_ID = hashlib.sha256(
    f"{TEST_CANDIDATE_ID}:{TEST_CHALLENGE}".encode()
).hexdigest()


def sign_report(
    path: Path,
    *,
    run_id: int,
    commit: str = TEST_COMMIT,
    run_attempt: int = 1,
    repository: str = TEST_POLICY.repository,
    repository_ref: str = TEST_POLICY.repository_ref,
    workflow: str = TEST_POLICY.workflow,
    workflow_ref: str = TEST_POLICY.workflow_ref,
    job: str = TEST_POLICY.job,
    candidate_id: str = TEST_CANDIDATE_ID,
    bundle_id: str | None = None,
    challenge: str = TEST_CHALLENGE,
    nonce: str | None = None,
    issued_at: str | None = None,
    expires_at: str | None = None,
) -> dict:
    report = read_json(path)
    package = report["package"]
    package.setdefault("installed_extension_sha256", package["native_extension_sha256"])
    measurement = report["measurement"]
    result_sha = measurement["result_sha256"][0]
    measurement.setdefault(
        "runs",
        [
            {
                "complete": True,
                "exit_code": 0,
                "timed_out": False,
                "result_sha256": result_sha,
            }
            for _index in range(3)
        ],
    )
    report["workload"]["identity_sha256"] = workload_identity(report["workload"])
    write_json(path, report)
    resolved_bundle_id = bundle_id or hashlib.sha256(
        f"{candidate_id}:{challenge}".encode()
    ).hexdigest()
    return write_signed_platform_provenance(
        path,
        Path(f"{path}.provenance.json"),
        repository=repository,
        repository_ref=repository_ref,
        workflow=workflow,
        workflow_ref=workflow_ref,
        job=job,
        commit=commit,
        run_id=str(run_id),
        run_attempt=run_attempt,
        candidate_id=candidate_id,
        bundle_id=resolved_bundle_id,
        challenge=challenge,
        nonce=nonce or hashlib.sha256(f"{run_id}:{path.name}".encode()).hexdigest(),
        key_id=TEST_KEY_ID,
        private_key=TEST_PRIVATE_KEY,
        issued_at=issued_at,
        expires_at=expires_at,
    )
