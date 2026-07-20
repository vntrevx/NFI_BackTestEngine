"""Release-grade parity, performance, and resource evidence packaging."""

from __future__ import annotations

import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import write_json
from .errors import BenchmarkError
from .fixture import sha256_file
from .performance_gate import PerformanceLevel, run_performance_gate
from .product_contract import (
    CERTIFICATION_SPREAD_THRESHOLD,
    DEFAULT_CERTIFICATION_REPETITIONS,
    DEFAULT_CERTIFICATION_WARMUPS,
    MAX_CERTIFICATION_REPETITIONS,
    MIN_CERTIFICATION_REPETITIONS,
)
from .specs import CERTIFICATION_REPORT_SCHEMA, validate_schema

CERTIFICATION_REPORT_VERSION = "1.1.0"
def run_certification(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    profile_path: str | Path | None = None,
    verification_level: PerformanceLevel = "quick",
    state_probe_manifests: list[str | Path] | None = None,
    repetitions: int = DEFAULT_CERTIFICATION_REPETITIONS,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Run the strict gate and package every proof file into one immutable bundle."""
    if verification_level != "quick":
        raise BenchmarkError(
            "release certification measures the representative fixture at quick level; "
            "use branch-reaching --state-probe fixtures for full-state verification"
        )
    if repetitions < MIN_CERTIFICATION_REPETITIONS:
        raise BenchmarkError(
            f"release certification requires at least {MIN_CERTIFICATION_REPETITIONS} repetitions"
        )
    probe_manifests = [Path(path).resolve() for path in state_probe_manifests or []]
    if not probe_manifests:
        raise BenchmarkError("release certification requires at least one full-state probe")
    manifest = Path(manifest_path).resolve()
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"certification output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    performance_directory = output / "measurements"
    performance = run_performance_gate(
        manifest,
        performance_directory,
        profile_path=profile_path,
        verification_level=verification_level,
        repetitions=repetitions,
        timeout_seconds=timeout_seconds,
        warmup_runs=DEFAULT_CERTIFICATION_WARMUPS,
        adaptive=True,
        max_repetitions=MAX_CERTIFICATION_REPETITIONS,
        spread_threshold=CERTIFICATION_SPREAD_THRESHOLD,
        alternate_order=True,
    )
    probe_reports = []
    for index, probe_manifest in enumerate(probe_manifests, start=1):
        probe_output = output / "state-probes" / f"probe-{index:02d}"
        probe_performance = run_performance_gate(
            probe_manifest,
            probe_output,
            profile_path=profile_path,
            verification_level="full",
            repetitions=1,
            timeout_seconds=timeout_seconds,
        )
        probe_reports.append(
            {
                "fixture_id": probe_performance["fixture_id"],
                "manifest_sha256": sha256_file(probe_manifest),
                "complete": probe_performance["complete"],
                "trade_surface_equal": probe_performance["gates"]["parity"]["met"],
                "full_state_equal": _full_state_equal(probe_performance),
                "performance_report": _artifact_record(
                    probe_output / "performance.json",
                    relative_to=output,
                ),
            }
        )
    performance_path = performance_directory / "performance.json"
    engine_summary = performance["engine"]["summary"]
    reference_summary = performance["reference"]["summary"]
    state_probes_met = all(
        probe["complete"]
        and probe["trade_surface_equal"]
        and probe["full_state_equal"]
        for probe in probe_reports
    )
    release_certified = performance["release_certified"] and state_probes_met
    gates = {
        **performance["gates"],
        "state_probes": {
            "met": state_probes_met,
            "required": len(probe_reports),
            "completed": sum(1 for probe in probe_reports if probe["complete"]),
            "rule": "every branch-reaching probe must pass exact full-state parity",
        },
    }
    report = {
        "schema_version": CERTIFICATION_REPORT_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "fixture": {
            "id": performance["fixture_id"],
            "manifest_sha256": sha256_file(manifest),
        },
        "verification_level": "quick+full-probes",
        "repetitions": int(performance.get("repetitions", repetitions)),
        "status": "certified" if release_certified else "failed",
        "release_certified": release_certified,
        "claim_scope": performance["claim_scope"],
        "gates": gates,
        "state_probes": probe_reports,
        "measurements": {
            "engine_wall_time_median_seconds": engine_summary["wall_time_seconds"]["median"],
            "reference_wall_time_median_seconds": reference_summary["wall_time_seconds"]["median"],
            "observed_speedup": performance["gates"]["speed"]["observed_speedup"],
            "engine_peak_rss_bytes": engine_summary["peak_rss_bytes"]["maximum"],
            "reference_peak_rss_bytes": reference_summary["peak_rss_bytes"]["maximum"],
        },
        "environment": {
            "hardware": performance["hardware"],
            "execution_profile": performance["execution_profile"],
            "engine_build": performance["engine_build"],
        },
        "performance_report": _artifact_record(performance_path, relative_to=output),
    }
    validate_schema(report, CERTIFICATION_REPORT_SCHEMA)
    report_path = output / "certification.json"
    write_json(report_path, report)

    included = _bundle_files(output)
    bundle_manifest = {
        "schema_version": "1.0.0",
        "fixture_id": performance["fixture_id"],
        "files": [
            _artifact_record(path, relative_to=output)
            for path in included
        ],
    }
    bundle_manifest_path = output / "bundle-manifest.json"
    write_json(bundle_manifest_path, bundle_manifest)
    included.append(bundle_manifest_path)

    archive_path = output / "certification-bundle.zip"
    _write_reproducible_zip(archive_path, output, included)
    bundle_record = {
        "schema_version": "1.0.0",
        "fixture_id": performance["fixture_id"],
        # The bundle publishes the combined verdict. The representative performance
        # fixture can pass while a branch-reaching full-state probe fails.
        "release_certified": release_certified,
        "archive": _artifact_record(archive_path, relative_to=output),
        "manifest": _artifact_record(bundle_manifest_path, relative_to=output),
    }
    write_json(output / "bundle.json", bundle_record)
    return {
        **report,
        "bundle": bundle_record,
    }


def _full_state_equal(performance: dict[str, Any]) -> bool:
    reports: list[Any] = []
    for lane in ("engine", "reference"):
        lane_record = performance.get(lane)
        runs = lane_record.get("runs") if isinstance(lane_record, dict) else None
        if not isinstance(runs, list) or not runs:
            return False
        reports.extend(
            run.get("report") if isinstance(run, dict) else None
            for run in runs
        )
    return all(
        isinstance(report, dict)
        and report.get("verification_level", report.get("trace_mode")) == "full"
        and isinstance(report.get("parity"), dict)
        and isinstance(report["parity"].get("state_trace"), dict)
        and report["parity"]["state_trace"].get("equal") is True
        for report in reports
    )


def _bundle_files(root: Path) -> list[Path]:
    excluded = {
        root / "bundle-manifest.json",
        root / "bundle.json",
        root / "certification-bundle.zip",
    }
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path not in excluded
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _write_reproducible_zip(destination: Path, root: Path, sources: list[Path]) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for source in sorted(sources, key=lambda path: path.relative_to(root).as_posix()):
            relative = source.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            with source.open("rb") as input_file, archive.open(info, "w") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)


def _artifact_record(path: Path, *, relative_to: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(relative_to).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
