"""Release-grade parity, performance, and resource evidence packaging."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import write_json
from .certification_parts.gates import _full_state_equal
from .certification_parts.packaging import (
    _artifact_record,
    _bundle_files,
    _write_certification_publication,
)
from .errors import BenchmarkError
from .fixture import sha256_file
from .performance_gate import PerformanceLevel, run_performance_gate
from .portable_paths import validate_new_output_path
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
    try:
        output = validate_new_output_path(output_directory)
    except ValueError as exc:
        raise BenchmarkError(f"certification output is not publishable: {exc}") from exc
    output.mkdir(parents=True, exist_ok=False)

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
    bundle_record = _write_certification_publication(
        output,
        included,
        fixture_id=performance["fixture_id"],
        # The bundle publishes the combined verdict. The representative performance
        # fixture can pass while a branch-reaching full-state probe fails.
        release_certified=release_certified,
    )
    return {
        **report,
        "bundle": bundle_record,
    }
