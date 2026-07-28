"""Result summary loading, verification binding, and evidence model."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..canonical import read_json
from ..errors import BenchmarkError
from ..fixture import sha256_file
from ..specs import validate_trade_surface
from .contracts import SUMMARY_FILENAME


def _with_adjacent_resource_measurement(
    root: Path,
    run_report: dict[str, Any],
) -> dict[str, Any]:
    """Attach certification RSS data to the derived view when it is available.

    A normal research run does not claim peak RSS because its parent process may
    have unobserved children.  The certification runner writes a measured sibling
    file after the process exits; that value is safe to display without rewriting
    the original ``run.json``.
    """

    measurement_path = root / "certification-measurement.json"
    if not measurement_path.is_file():
        return run_report
    measurement = read_json(measurement_path)
    if not isinstance(measurement, Mapping) or measurement.get("exit_code") != 0:
        return run_report
    peak = measurement.get("peak_rss_bytes")
    if not isinstance(peak, int) or isinstance(peak, bool) or peak < 0:
        return run_report
    enriched = dict(run_report)
    enriched["resource_usage"] = {
        "peak_rss_bytes": peak,
        "source": str(measurement_path.resolve()),
    }
    return enriched


def load_result_summary(run_directory: str | Path) -> dict[str, Any]:
    path = Path(run_directory).resolve() / SUMMARY_FILENAME
    if not path.is_file():
        raise BenchmarkError(f"result summary does not exist: {path}")
    summary = read_json(path)
    if not isinstance(summary, dict):
        raise BenchmarkError(f"result summary must be an object: {path}")
    return summary


def _load_bound_surface(
    root: Path,
    run_report: Mapping[str, Any],
) -> dict[str, Any] | None:
    result = run_report.get("result")
    if not isinstance(result, Mapping):
        return None
    record = result.get("trade_surface")
    if not isinstance(record, Mapping):
        return None
    expected_hash = record.get("sha256")
    if not isinstance(expected_hash, str):
        raise BenchmarkError("research trade-surface record has no SHA-256")

    # Prefer the portable sibling path.  Older reports may contain an absolute
    # path from the machine that produced the run, while the sealed artifact has
    # since been copied to another supported host.
    candidates = [root / "trade-surface.json"]
    recorded_path = record.get("path")
    if isinstance(recorded_path, str):
        candidates.append(Path(recorded_path))
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_file():
            continue
        if sha256_file(resolved) != expected_hash:
            continue
        surface = read_json(resolved)
        validate_trade_surface(surface)
        if not isinstance(surface, dict):
            raise BenchmarkError(f"trade surface must be an object: {resolved}")
        if surface.get("schema_version") != "2.0.0":
            raise BenchmarkError(
                "result presentation requires trade-surface schema 2.0.0; "
                "normalize or rerun this legacy result first"
            )
        return surface
    raise BenchmarkError("research trade-surface artifact failed its hash binding")


def _resolve_verification(
    root: Path,
    run_report: Mapping[str, Any],
    *,
    verification: Mapping[str, Any] | None,
    verification_path: str | Path | None,
) -> dict[str, Any] | None:
    """Load explicit proof or retain a still hash-valid previous proof link."""

    if verification is not None:
        document = dict(verification)
        if verification_path is not None:
            source = Path(verification_path).resolve()
            document["report_path"] = str(source)
            if source.is_file():
                document["report_sha256"] = sha256_file(source)
        _validate_verification_binding(run_report, document)
        return document

    previous_path = root / SUMMARY_FILENAME
    if not previous_path.is_file():
        return None
    previous = read_json(previous_path)
    if not isinstance(previous, Mapping):
        return None
    previous_run = previous.get("run")
    previous_verification = previous.get("verification")
    if (
        not isinstance(previous_run, Mapping)
        or previous_run.get("id") != run_report.get("run_id")
        or not isinstance(previous_verification, Mapping)
        or previous_verification.get("status") not in {"exact_match", "mismatch"}
    ):
        return None
    source_value = previous_verification.get("source")
    source_sha256 = previous_verification.get("source_sha256")
    if not isinstance(source_value, str) or not isinstance(source_sha256, str):
        return None
    source = Path(source_value)
    if not source.is_file() or sha256_file(source) != source_sha256:
        return None
    candidate = read_json(source)
    if not isinstance(candidate, dict):
        return None
    candidate["report_path"] = str(source.resolve())
    candidate["report_sha256"] = source_sha256
    _validate_verification_binding(run_report, candidate)
    return candidate


def _validate_verification_binding(
    run_report: Mapping[str, Any],
    verification: Mapping[str, Any],
) -> None:
    verification_run_id = verification.get("run_id")
    if (
        "equal" in verification
        and isinstance(verification_run_id, str)
        and verification_run_id != run_report.get("run_id")
    ):
        raise BenchmarkError("confirmation report belongs to a different research run")
    result = run_report.get("result")
    if not isinstance(result, Mapping):
        return
    surface = result.get("trade_surface")
    if not isinstance(surface, Mapping):
        return
    current_hash = surface.get("sha256")
    if not isinstance(current_hash, str):
        return
    engine = verification.get("engine")
    inputs = verification.get("inputs")
    reference_candidate = (
        inputs.get("engine_trade_surface") if isinstance(inputs, Mapping) else None
    )
    confirmed_records: tuple[Mapping[str, Any], Mapping[str, Any]] = (
        engine if isinstance(engine, Mapping) else {},
        reference_candidate if isinstance(reference_candidate, Mapping) else {},
    )
    for record in confirmed_records:
        confirmed_hash = record.get("sha256")
        if isinstance(confirmed_hash, str) and confirmed_hash != current_hash:
            raise BenchmarkError("confirmation report belongs to a different trade surface")
