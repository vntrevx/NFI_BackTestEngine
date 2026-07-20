from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest
from nfi_backtest_engine import certification, object_storage
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import BenchmarkError
from nfi_backtest_engine.fixture import sha256_file


def _performance_report(
    output: Path,
    *,
    certified: bool = True,
    full_state: bool = False,
) -> dict:
    output.mkdir(parents=True)
    report = {
        "fixture_id": "representative-fixture",
        "complete": certified,
        "release_certified": certified,
        "claim_scope": {
            "eligible": True,
            "actual_pairs": 80,
            "actual_days": 1826,
        },
        "gates": {
            "parity": {"met": True},
            "speed": {"met": True, "observed_speedup": 12.5},
            "memory": {"met": True},
        },
        "hardware": {"fingerprint": "host-a"},
        "execution_profile": {"hardware_fingerprint": "host-a"},
        "engine_build": {"source_fingerprint": "source-a"},
        "engine": {
            "runs": [
                {
                    "report": {
                        "verification_level": "full",
                        "parity": {"state_trace": {"equal": True}},
                    }
                }
            ]
            if full_state
            else [],
            "summary": {
                "wall_time_seconds": {"median": 8.0},
                "peak_rss_bytes": {"maximum": 1_000_000},
            }
        },
        "reference": {
            "runs": [
                {
                    "report": {
                        "trace_mode": "full",
                        "parity": {"state_trace": {"equal": True}},
                    }
                }
            ]
            if full_state
            else [],
            "summary": {
                "wall_time_seconds": {"median": 100.0},
                "peak_rss_bytes": {"maximum": 2_000_000},
            }
        },
    }
    write_json(output / "performance.json", report)
    (output / "engine-01.stdout.log").write_text("engine proof\n", encoding="utf-8")
    return report


def test_certification_packages_strict_repeated_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"fixture_id": "representative-fixture"})
    probe_manifest = tmp_path / "probe.json"
    write_json(probe_manifest, {"fixture_id": "state-probe"})

    def fake_gate(
        manifest_path: Path,
        output_directory: Path,
        **options: object,
    ) -> dict:
        if Path(manifest_path) == manifest:
            assert options["verification_level"] == "quick"
            assert options["repetitions"] == 5
            return _performance_report(Path(output_directory))
        assert Path(manifest_path) == probe_manifest
        assert options["verification_level"] == "full"
        assert options["repetitions"] == 1
        return _performance_report(Path(output_directory), full_state=True)

    monkeypatch.setattr(certification, "run_performance_gate", fake_gate)

    report = certification.run_certification(
        manifest,
        tmp_path / "certificate",
        state_probe_manifests=[probe_manifest],
        repetitions=5,
    )

    assert report["release_certified"] is True
    assert report["bundle"]["release_certified"] is True
    assert report["measurements"]["observed_speedup"] == 12.5
    assert report["bundle"]["archive"]["sha256"] == sha256_file(
        tmp_path / "certificate" / "certification-bundle.zip"
    )
    with zipfile.ZipFile(tmp_path / "certificate" / "certification-bundle.zip") as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert "certification.json" in archive.namelist()
        assert "measurements/performance.json" in archive.namelist()
        assert "bundle-manifest.json" in archive.namelist()
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())


def test_certification_bundle_uses_combined_state_probe_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"fixture_id": "representative-fixture"})
    probe_manifest = tmp_path / "probe.json"
    write_json(probe_manifest, {"fixture_id": "state-probe"})

    def fake_gate(
        manifest_path: Path,
        output_directory: Path,
        **options: object,
    ) -> dict:
        if Path(manifest_path) == manifest:
            return _performance_report(Path(output_directory))
        report = _performance_report(Path(output_directory), full_state=True)
        report["engine"]["runs"][0]["report"]["parity"]["state_trace"]["equal"] = False
        return report

    monkeypatch.setattr(certification, "run_performance_gate", fake_gate)

    report = certification.run_certification(
        manifest,
        tmp_path / "certificate",
        state_probe_manifests=[probe_manifest],
    )

    assert report["release_certified"] is False
    assert report["gates"]["state_probes"]["met"] is False
    assert report["bundle"]["release_certified"] is False


def test_certification_rejects_a_probe_missing_one_execution_lane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"fixture_id": "representative-fixture"})
    probe_manifest = tmp_path / "probe.json"
    write_json(probe_manifest, {"fixture_id": "state-probe"})

    def fake_gate(
        manifest_path: Path,
        output_directory: Path,
        **options: object,
    ) -> dict:
        if Path(manifest_path) == manifest:
            return _performance_report(Path(output_directory))
        report = _performance_report(Path(output_directory), full_state=True)
        report["reference"]["runs"] = []
        return report

    monkeypatch.setattr(certification, "run_performance_gate", fake_gate)

    report = certification.run_certification(
        manifest,
        tmp_path / "certificate",
        state_probe_manifests=[probe_manifest],
    )

    assert report["release_certified"] is False
    assert report["gates"]["state_probes"]["met"] is False


def test_certification_rejects_single_run_missing_probe_or_full_representative_trace(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"fixture_id": "fixture"})

    with pytest.raises(BenchmarkError, match="at least 3 repetitions"):
        certification.run_certification(manifest, tmp_path / "one", repetitions=1)
    with pytest.raises(BenchmarkError, match="full-state probe"):
        certification.run_certification(
            manifest,
            tmp_path / "missing-probe",
        )
    with pytest.raises(BenchmarkError, match="representative fixture at quick level"):
        certification.run_certification(
            manifest,
            tmp_path / "full-representative",
            verification_level="full",
            state_probe_manifests=[manifest],
        )


def test_s3_transport_verifies_upload_and_download_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"sealed evidence")
    digest = sha256_file(source)

    def fake_aws(
        arguments: list[str],
        *,
        endpoint_url: str | None,
    ) -> subprocess.CompletedProcess[str]:
        assert endpoint_url == "https://objects.example.test"
        if arguments[:2] == ["s3api", "head-object"]:
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=(
                    f'{{"ContentLength": {source.stat().st_size}, '
                    f'"Metadata": {{"sha256": "{digest}"}}}}'
                ),
                stderr="",
            )
        if arguments[:2] == ["s3", "cp"] and arguments[2].startswith("s3://"):
            Path(arguments[3]).write_bytes(source.read_bytes())
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(object_storage, "_run_aws", fake_aws)

    uploaded = object_storage.upload_artifact(
        source,
        "s3://research-bucket/certificates/bundle.zip",
        endpoint_url="https://objects.example.test",
    )
    destination = tmp_path / "downloaded.zip"
    downloaded = object_storage.download_artifact(
        "s3://research-bucket/certificates/bundle.zip",
        destination,
        expected_sha256=digest,
        endpoint_url="https://objects.example.test",
    )

    assert uploaded["verified"] is True
    assert downloaded["verified"] is True
    assert destination.read_bytes() == source.read_bytes()
