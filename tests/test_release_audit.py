from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine import native_scorecard
from nfi_backtest_engine.combined_release import (
    RELEASE_CHECKSUMS_NAME,
    seal_combined_release_candidate,
)
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.release_audit import (
    REQUIRED_SOAK_CHECKS,
    record_operations_soak_cycle,
    seal_ten_of_ten_release_audit,
)
from provenance_support import TEST_POLICY
from test_combined_release import _combined_gate_inputs
from test_native_scorecard import _current_ref_proof

COMMIT = "1" * 40
TAG = "v1.0.0-rc.1"


@pytest.fixture(autouse=True)
def _stable_current_ref_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        native_scorecard,
        "begin_packaged_semantic_registry_authorization",
        _current_ref_proof,
    )
    monkeypatch.setattr(
        native_scorecard,
        "finalize_packaged_semantic_registry_authorization",
        lambda _proof: None,
    )
    monkeypatch.setattr(
        native_scorecard,
        "require_fresh_current_ref_for_authorization",
        lambda _evidence, _identity, _operation: None,
    )


def _checks(*, commit: str = COMMIT) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "run_id": index,
            "head_sha": commit,
            "conclusion": "success",
            "evidence_sha256": f"{index:064x}",
            **(
                {"latest_checked_age_seconds": 60}
                if name in {"compatibility", "discovery"}
                else {}
            ),
        }
        for index, name in enumerate(sorted(REQUIRED_SOAK_CHECKS), start=1)
    }


def test_record_operations_soak_cycle_is_fail_closed(tmp_path: Path) -> None:
    checks = _checks()
    checks["nightly"]["conclusion"] = "failure"

    with pytest.raises(SpecValidationError, match="nightly check is incomplete"):
        record_operations_soak_cycle(
            candidate_commit=COMMIT,
            release_tag=TAG,
            cycle=1,
            checked_at="2026-09-01T00:00:00Z",
            public_manifest_sha256="a" * 64,
            checks=checks,
            output_path=tmp_path / "receipt.json",
        )

    assert not (tmp_path / "receipt.json").exists()


def test_seal_ten_of_ten_release_audit_binds_one_fixed_public_candidate(
    tmp_path: Path,
) -> None:
    inputs = _combined_gate_inputs(tmp_path, slug_contract=True)
    seal_combined_release_candidate(**inputs)
    release = Path(inputs["output_directory"])
    manifest_sha256 = sha256_file(release / RELEASE_CHECKSUMS_NAME)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    receipts = []
    cycle_offsets = range(7)
    for cycle, day_offset in enumerate(cycle_offsets, start=1):
        path = tmp_path / "receipts" / f"cycle-{cycle}.json"
        record_operations_soak_cycle(
            candidate_commit=COMMIT,
            release_tag=TAG,
            cycle=cycle,
            checked_at=(start + timedelta(days=day_offset)).isoformat(),
            public_manifest_sha256=manifest_sha256,
            checks=_checks(),
            output_path=path,
        )
        receipts.append(path)

    report = seal_ten_of_ten_release_audit(
        release_directory=release,
        candidate_commit=COMMIT,
        release_tag=TAG,
        soak_receipt_paths=receipts,
        output_path=tmp_path / "ten-of-ten.json",
        provenance_policy=TEST_POLICY,
    )

    assert report["complete"] is True
    assert report["combined_full_x7_certified"] is True
    assert report["operations"]["cycles"] == 7
    assert report["supported_platform_slugs"] == [
        "linux-x86_64",
        "linux-aarch64",
        "macos-arm64",
        "windows-wsl2-x86_64",
    ]
    assert len(report["identity_graph"]["soak_receipts"]) == 7
    assert report["identity_graph"]["distributions"]
    assert (
        report["identity_graph"]["distribution_identity"]["file"]
        == "distribution-identity.json"
    )
    assert (
        report["identity_graph"]["sbom"]["file"]
        == "nfi-backtest-engine.spdx.json"
    )
    assert report["identity_graph"]["cross_channel_identity_sha256"]
    assert set(report["identity_graph"]["platform_provenance"]) == {
        "binance-spot",
        "binance-usdtm-isolated",
    }


def test_ten_of_ten_audit_rejects_same_day_cycle_stacking(tmp_path: Path) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    receipts = []
    for cycle in range(1, 8):
        path = tmp_path / f"cycle-{cycle}.json"
        record_operations_soak_cycle(
            candidate_commit=COMMIT,
            release_tag=TAG,
            cycle=cycle,
            checked_at=(start + timedelta(hours=cycle - 1)).isoformat(),
            public_manifest_sha256="a" * 64,
            checks=_checks(),
            output_path=path,
        )
        receipts.append(path)

    with pytest.raises(SpecValidationError, match="at least 24 hours apart"):
        seal_ten_of_ten_release_audit(
            release_directory=tmp_path / "unneeded",
            candidate_commit=COMMIT,
            release_tag=TAG,
            soak_receipt_paths=receipts,
            output_path=tmp_path / "audit.json",
        )


def test_ten_of_ten_audit_rejects_stale_discovery_receipt(tmp_path: Path) -> None:
    checks = _checks()
    checks["discovery"]["latest_checked_age_seconds"] = 86401
    receipt = {
        "schema_version": "operations-soak-cycle-v1",
        "candidate_commit": COMMIT,
        "release_tag": TAG,
        "cycle": 1,
        "checked_at": "2026-09-01T00:00:00Z",
        "public_manifest_sha256": "a" * 64,
        "checks": deepcopy(checks),
        "complete": True,
    }
    from nfi_backtest_engine.canonical import write_json

    path = tmp_path / "stale.json"
    write_json(path, receipt)
    with pytest.raises(SpecValidationError, match="discovery freshness exceeds"):
        seal_ten_of_ten_release_audit(
            release_directory=tmp_path / "missing",
            candidate_commit=COMMIT,
            release_tag=TAG,
            soak_receipt_paths=[path] * 7,
            output_path=tmp_path / "audit.json",
        )
