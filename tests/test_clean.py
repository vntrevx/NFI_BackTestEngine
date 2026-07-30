from __future__ import annotations

import json
import os
from pathlib import Path

import psutil
import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.clean import _pid_active, create_clean_audit, format_clean_audit
from nfi_backtest_engine.clean_apply import apply_clean
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.evidence_bundle import write_evidence_bundle


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(value)}\n", encoding="utf-8")


def _no_activity() -> dict:
    return {
        "services": {"status": "available", "active": [], "detail": None},
        "containers": {"status": "available", "active": [], "detail": None},
    }


def _entries_by_path(audit: dict) -> dict[str, dict]:
    return {entry["path"]: entry for entry in audit["entries"]}


def test_dry_run_classifies_disk_usage_without_deleting_files(tmp_path: Path) -> None:
    root = tmp_path / ".nfi"
    (root / "cache" / "entry").mkdir(parents=True)
    (root / "cache" / "entry" / "payload").write_bytes(b"cache")
    _write_json(root / "failed-run" / "run.json", {"status": "failed", "complete": False})
    (root / "spool").mkdir()
    (root / "spool" / "chunk.arrow").write_bytes(b"arrow")
    (root / "build-old").mkdir()
    (root / "build-old" / "binary").write_bytes(b"build")
    _write_json(
        root / "completed-run" / "run.json",
        {"run_id": "run-1", "status": "complete", "complete": True},
    )
    (root / "preserved").mkdir()
    (root / "preserved" / ".nfi-preserve").write_text("", encoding="utf-8")
    (root / "unknown").mkdir()
    (root / "unknown" / "notes.txt").write_text("keep", encoding="utf-8")
    output = root / "audit" / "clean.json"

    audit = create_clean_audit(
        root,
        output_path=output,
        activity_probe=_no_activity,
        created_at="2026-07-28T00:00:00Z",
    )

    entries = _entries_by_path(audit)
    assert entries["cache"]["category"] == "regenerable_vector_cache"
    assert entries["cache"]["deletable"] is True
    assert entries["failed-run"]["category"] == "interrupted_failed_run"
    assert entries["spool"]["category"] == "temporary_arrow_docker_spool"
    assert entries["build-old"]["category"] == "old_build_calibration"
    assert entries["completed-run"]["category"] == "user_preserved_run"
    assert entries["completed-run"]["deletable"] is False
    assert entries["preserved"]["category"] == "user_preserved_run"
    assert entries["unknown"]["category"] == "unclassified_protected"
    assert audit["summary"]["logical_bytes"] == 5 + len(
        (root / "failed-run" / "run.json").read_bytes()
    ) + 5 + 5 + len((root / "completed-run" / "run.json").read_bytes()) + 4
    assert audit["summary"]["reclaimable_logical_bytes"] > 0
    assert audit["summary"]["protected_logical_bytes"] > 0
    assert audit["safety"]["deletion_performed"] is False
    assert output.is_file()
    assert (root / "cache" / "entry" / "payload").read_bytes() == b"cache"
    assert len(audit["categories"]) == 9


@pytest.mark.parametrize(
    "status",
    ["budget_exhausted", "external_data_deferred", "infrastructure_failed"],
)
def test_incomplete_discovery_work_is_reclaimable(
    tmp_path: Path,
    status: str,
) -> None:
    root = tmp_path / ".nfi"
    _write_json(
        root / f"futures-discovery-{status}" / "run.json",
        {"status": status, "complete": False},
    )
    _write_json(
        root / "futures-discovery-complete" / "run.json",
        {"status": "coverage_exhausted", "complete": True},
    )

    audit = create_clean_audit(
        root,
        activity_probe=_no_activity,
        created_at="2026-07-30T00:00:00Z",
    )

    entries = _entries_by_path(audit)
    interrupted = entries[f"futures-discovery-{status}"]
    assert interrupted["category"] == "interrupted_failed_run"
    assert interrupted["deletable"] is True
    assert entries["futures-discovery-complete"]["category"] == "user_preserved_run"
    assert entries["futures-discovery-complete"]["deletable"] is False


def test_release_bundle_and_official_zip_are_identity_bound_and_protected(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".nfi"
    release = root / "release"
    release.mkdir(parents=True)
    report = release / "report.json"
    _write_json(report, {"schema_version": "1.0.0", "status": "test"})
    write_evidence_bundle(
        release,
        evidence_id="release-test",
        release_certified=True,
        include_paths=[report],
    )
    oracle = root / "oracle"
    oracle.mkdir()
    (oracle / "freqtrade-result.zip").write_bytes(b"official")

    audit = create_clean_audit(
        root,
        activity_probe=_no_activity,
        created_at="2026-07-28T00:00:00Z",
    )

    entries = _entries_by_path(audit)
    release_entry = entries["release"]
    assert release_entry["category"] == "release_certificate_bundle"
    assert release_entry["deletable"] is False
    assert release_entry["identity_complete"] is True
    assert {item["path"] for item in release_entry["evidence_identity"]} >= {
        "release/bundle.json",
        "release/bundle-manifest.json",
        "release/certification-bundle.zip",
    }
    oracle_entry = entries["oracle"]
    assert oracle_entry["category"] == "official_oracle_freqtrade_zip"
    assert oracle_entry["deletable"] is False
    assert oracle_entry["evidence_identity"][0]["sha256"]


def test_incomplete_certification_identity_is_protected_without_blocking_other_units(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".nfi"
    _write_json(root / "candidate" / "full-x7-certification.json", {})
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "payload").write_bytes(b"cache")

    audit = create_clean_audit(
        root,
        activity_probe=_no_activity,
        created_at="2026-07-28T00:00:00Z",
    )

    entry = _entries_by_path(audit)["candidate"]
    assert entry["category"] == "release_certificate_bundle"
    assert entry["deletable"] is False
    assert entry["identity_complete"] is False
    assert audit["safety"]["fail_closed"] is False
    assert audit["issues"][0]["code"] == "CERTIFICATION_IDENTITY_INCOMPLETE"

    result = apply_clean(root, activity_probe=_no_activity)

    assert (root / "candidate" / "full-x7-certification.json").is_file()
    assert not (root / "cache").exists()
    assert {item["path"] for item in result["deleted"]} == {"cache"}


def test_active_pid_protects_its_run_and_checkpoints(tmp_path: Path) -> None:
    root = tmp_path / ".nfi"
    active = root / "active-run"
    active.mkdir(parents=True)
    (active / "run.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (active / "checkpoints").mkdir()
    (active / "checkpoints" / "vectors.json").write_text("{}\n", encoding="utf-8")

    audit = create_clean_audit(
        root,
        activity_probe=_no_activity,
        created_at="2026-07-28T00:00:00Z",
    )

    entry = _entries_by_path(audit)["active-run"]
    assert entry["category"] == "active_run_checkpoint"
    assert entry["deletable"] is False
    assert entry["active_pids"][0]["pid"] == os.getpid()
    assert audit["safety"]["active_pid_count"] == 1
    assert audit["safety"]["fail_closed"] is True


def test_pid_liveness_uses_a_cross_platform_process_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int] = []

    def pid_exists(pid: int) -> bool:
        observed.append(pid)
        return pid == 42

    monkeypatch.setattr(psutil, "pid_exists", pid_exists)

    assert _pid_active(42) is True
    assert _pid_active(43) is False
    assert _pid_active(0) is False
    assert observed == [42, 43]


@pytest.mark.skipif(os.name != "posix", reason="fcntl lock assertion is POSIX-specific")
def test_held_lock_protects_its_run(tmp_path: Path) -> None:
    import fcntl

    root = tmp_path / ".nfi"
    active = root / "active-run"
    active.mkdir(parents=True)
    lock_path = active / "pipeline.lock"
    lock_path.write_bytes(b"lock")

    with lock_path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        audit = create_clean_audit(
            root,
            activity_probe=_no_activity,
            created_at="2026-07-28T00:00:00Z",
        )

    entry = _entries_by_path(audit)["active-run"]
    assert entry["category"] == "active_run_checkpoint"
    assert entry["deletable"] is False
    assert entry["active_locks"][0]["status"] == "active"
    assert audit["safety"]["active_lock_count"] == 1


def test_active_service_blocks_otherwise_reclaimable_entries(tmp_path: Path) -> None:
    root = tmp_path / ".nfi"
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "payload").write_bytes(b"cache")

    def activity() -> dict:
        return {
            "services": {
                "status": "available",
                "active": [
                    {
                        "kind": "service",
                        "identity": "nfi-full-certification.service",
                    }
                ],
                "detail": None,
            },
            "containers": {"status": "available", "active": [], "detail": None},
        }

    audit = create_clean_audit(
        root,
        activity_probe=activity,
        created_at="2026-07-28T00:00:00Z",
    )

    entry = _entries_by_path(audit)["cache"]
    assert entry["category"] == "regenerable_vector_cache"
    assert entry["deletable"] is False
    assert "active NFI service" in entry["protection_reason"]
    assert audit["safety"]["active_service_count"] == 1


def test_skipped_runtime_probes_block_reclamation_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / ".nfi"
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "payload").write_bytes(b"cache")

    audit = create_clean_audit(
        root,
        inspect_runtime=False,
        created_at="2026-07-28T00:00:00Z",
    )

    entry = _entries_by_path(audit)["cache"]
    assert entry["deletable"] is False
    assert "probes are unavailable or were skipped" in entry["protection_reason"]
    assert audit["safety"]["fail_closed"] is True


def test_external_symlink_is_rejected_without_writing_an_audit(tmp_path: Path) -> None:
    root = tmp_path / ".nfi"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload").write_bytes(b"outside")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    output = root / "audit.json"

    with pytest.raises(SpecValidationError, match="symlink escapes"):
        create_clean_audit(
            root,
            output_path=output,
            activity_probe=_no_activity,
        )

    assert not output.exists()
    assert (outside / "payload").read_bytes() == b"outside"


def test_root_output_and_preserve_paths_cannot_escape_managed_nfi(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "ordinary"
    outside.mkdir()
    with pytest.raises(SpecValidationError, match="named .nfi"):
        create_clean_audit(outside, activity_probe=_no_activity)

    root = tmp_path / ".nfi"
    (root / "cache").mkdir(parents=True)
    with pytest.raises(SpecValidationError, match="audit path must stay"):
        create_clean_audit(
            root,
            output_path=tmp_path / "audit.json",
            activity_probe=_no_activity,
        )
    with pytest.raises(SpecValidationError, match="preserved path must identify"):
        create_clean_audit(
            root,
            preserve=[tmp_path],
            activity_probe=_no_activity,
        )


def test_cli_requires_dry_run_writes_audit_and_keeps_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / ".nfi"
    (root / "cache").mkdir(parents=True)
    payload = root / "cache" / "payload"
    payload.write_bytes(b"cache")
    output = root / "reports" / "clean-audit.json"

    exit_code = cli.main(
        [
            "clean",
            "--dry-run",
            "--root",
            str(root),
            "--output",
            str(output),
            "--no-runtime-probes",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "Clean dry-run           no files deleted" in captured.out
    assert f"Audit JSON              {output}" in captured.out
    assert output.is_file()
    assert payload.read_bytes() == b"cache"
    assert "clean-audit.json" not in {
        entry["path"] for entry in json.loads(output.read_text(encoding="utf-8"))["entries"]
    }


def test_cli_rejects_skipped_runtime_probes_in_apply_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / ".nfi"
    (root / "cache").mkdir(parents=True)
    payload = root / "cache" / "payload"
    payload.write_bytes(b"cache")

    exit_code = cli.main(
        [
            "clean",
            "--apply",
            "--root",
            str(root),
            "--no-runtime-probes",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--no-runtime-probes cannot be used with --apply" in captured.err
    assert payload.read_bytes() == b"cache"


def test_terminal_projection_includes_every_category(tmp_path: Path) -> None:
    root = tmp_path / ".nfi"
    root.mkdir()
    audit = create_clean_audit(
        root,
        activity_probe=_no_activity,
        created_at="2026-07-28T00:00:00Z",
    )

    snapshot = format_clean_audit(audit)

    assert "Expected reclaim" in snapshot
    assert "Active guards" in snapshot
    for category in (
        "active_run_checkpoint",
        "release_certificate_bundle",
        "official_oracle_freqtrade_zip",
        "regenerable_vector_cache",
    ):
        assert category in snapshot


def test_hard_links_are_counted_once_as_physical_reclaimable_storage(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".nfi"
    cache = root / "cache"
    cache.mkdir(parents=True)
    first = cache / "first"
    second = cache / "second"
    first.write_bytes(b"x" * 8192)
    os.link(first, second)

    audit = create_clean_audit(root, activity_probe=_no_activity)
    entry = _entries_by_path(audit)["cache"]
    metadata = first.stat()
    blocks = getattr(metadata, "st_blocks", None)
    expected_allocated = blocks * 512 if isinstance(blocks, int) else metadata.st_size

    assert entry["logical_bytes"] == 16_384
    assert entry["allocated_bytes"] == expected_allocated
    assert entry["reclaimable_allocated_bytes"] == entry["allocated_bytes"]
    assert audit["summary"]["allocated_bytes"] == entry["allocated_bytes"]


def test_hard_link_shared_with_a_protected_run_is_not_claimed_reclaimable(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".nfi"
    cache = root / "cache"
    completed = root / "runs" / "done"
    cache.mkdir(parents=True)
    completed.mkdir(parents=True)
    payload = cache / "vector.feather"
    payload.write_bytes(b"x" * 8192)
    os.link(payload, completed / "vector.feather")
    _write_json(
        completed / "run.json",
        {"run_id": "done", "status": "complete", "complete": True},
    )

    audit = create_clean_audit(root, activity_probe=_no_activity)
    entries = _entries_by_path(audit)

    assert entries["cache"]["deletable"] is True
    assert entries["cache"]["reclaimable_allocated_bytes"] == 0
    assert entries["runs/done"]["deletable"] is False
    assert audit["summary"]["allocated_bytes"] < audit["summary"]["logical_bytes"]


def test_hard_link_outside_managed_root_is_not_claimed_reclaimable(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".nfi"
    cache = root / "cache"
    cache.mkdir(parents=True)
    payload = cache / "vector.feather"
    payload.write_bytes(b"x" * 8192)
    outside = tmp_path / "outside-vector.feather"
    os.link(payload, outside)

    audit = create_clean_audit(root, activity_probe=_no_activity)
    entry = _entries_by_path(audit)["cache"]

    assert entry["deletable"] is True
    assert entry["allocated_bytes"] > 0
    assert entry["reclaimable_allocated_bytes"] == 0


def test_apply_deletes_only_fresh_audit_candidates_and_writes_receipt(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".nfi"
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "payload").write_bytes(b"cache")
    _write_json(root / "failed" / "run.json", {"status": "failed", "complete": False})
    _write_json(
        root / "runs" / "complete" / "run.json",
        {"run_id": "complete", "status": "complete", "complete": True},
    )
    (root / "oracle").mkdir()
    (root / "oracle" / "freqtrade-result.zip").write_bytes(b"official")

    result = apply_clean(root, activity_probe=_no_activity)

    assert result["status"] == "complete"
    assert {item["path"] for item in result["deleted"]} == {"cache", "failed"}
    assert not (root / "cache").exists()
    assert not (root / "failed").exists()
    assert (root / "runs" / "complete" / "run.json").is_file()
    assert (root / "oracle" / "freqtrade-result.zip").is_file()
    assert (root / "clean-audit.json").is_file()
    receipt = json.loads((root / "clean-result.json").read_text(encoding="utf-8"))
    assert receipt["audit"]["sha256"]
    assert receipt["summary"]["deleted_unit_count"] == 2


def test_include_completed_never_overrides_preservation_or_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".nfi"
    _write_json(
        root / "runs" / "delete-me" / "run.json",
        {"run_id": "delete-me", "status": "complete", "complete": True},
    )
    _write_json(
        root / "runs" / "keep-me" / "run.json",
        {"run_id": "keep-me", "status": "complete", "complete": True},
    )
    (root / "runs" / "keep-me" / ".nfi-preserve").write_text("", encoding="utf-8")
    _write_json(
        root / "runs" / "keep-explicit" / "run.json",
        {"run_id": "keep-explicit", "status": "complete", "complete": True},
    )
    evidence = root / "runs" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "result.zip").write_bytes(b"official")

    result = apply_clean(
        root,
        include_completed=True,
        preserve=["runs/keep-explicit"],
        activity_probe=_no_activity,
    )

    assert {item["path"] for item in result["deleted"]} == {"runs/delete-me"}
    assert not (root / "runs" / "delete-me").exists()
    assert (root / "runs" / "keep-me" / "run.json").is_file()
    assert (root / "runs" / "keep-explicit" / "run.json").is_file()
    assert (evidence / "result.zip").is_file()


def test_apply_refuses_a_fail_closed_audit_without_deleting(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".nfi"
    (root / "cache").mkdir(parents=True)
    payload = root / "cache" / "payload"
    payload.write_bytes(b"cache")

    def active_service() -> dict:
        return {
            "services": {
                "status": "available",
                "active": [{"kind": "service", "identity": "nfi-run.service"}],
                "detail": None,
            },
            "containers": {"status": "available", "active": [], "detail": None},
        }

    with pytest.raises(SpecValidationError, match="fail-closed"):
        apply_clean(root, activity_probe=active_service)

    assert payload.read_bytes() == b"cache"
    assert not (root / "clean-result.json").exists()


def test_apply_refuses_control_output_inside_a_deletion_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".nfi"
    (root / "cache").mkdir(parents=True)
    payload = root / "cache" / "payload"
    payload.write_bytes(b"cache")

    with pytest.raises(SpecValidationError, match="control output"):
        apply_clean(
            root,
            audit_path=root / "cache" / "audit.json",
            activity_probe=_no_activity,
        )

    assert payload.read_bytes() == b"cache"
