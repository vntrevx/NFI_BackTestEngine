"""Measured worker admission for one sealed Full Native workload."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .engine_runtime import FULL_VECTOR_INPUT, build_engine, run_engine
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file
from .workload_calibration import (
    calibrated_admission,
    calibration_key,
    calibration_path,
    create_workload_calibration,
    load_workload_calibration,
)

_RunEngine = Callable[..., dict[str, Any]]


def resolve_full_native_pair_workers(
    manifest_path: str | Path,
    *,
    profile_path: str | Path,
    hardware_fingerprint: str,
    requested_workers: int,
    memory_cap_bytes: int | None,
    calibration_directory: str | Path,
    recalibrate: bool = False,
    run_engine_fn: _RunEngine = run_engine,
) -> dict[str, Any]:
    """Measure one worst-footprint pair, then admit workers from current memory.

    Calibration identity includes every manifest byte and the Rust source
    fingerprint. A source/data/config change therefore cannot inherit an old
    memory observation. The probe uses the same complete Rust pipeline with a
    single pair and one worker; its result is diagnostic and deleted.
    """
    if requested_workers <= 0:
        raise SpecValidationError("Full Native requested workers must be positive")
    manifest = Path(manifest_path).resolve()
    if not manifest.is_file():
        raise BenchmarkError(f"Full Native manifest does not exist: {manifest}")
    build = build_engine()
    source_fingerprint = build.get("source_fingerprint")
    if not isinstance(source_fingerprint, str) or len(source_fingerprint) != 64:
        raise BenchmarkError("Full Native engine source fingerprint is unavailable")
    identity = {
        "kind": "full-native-pair-worker-calibration-v1",
        "manifest_sha256": sha256_file(manifest),
        "engine_source_fingerprint": source_fingerprint,
    }
    key = calibration_key(identity)
    directory = Path(calibration_directory).resolve() / "full-native"
    directory.mkdir(parents=True, exist_ok=True)
    destination = calibration_path(directory, key)

    calibration = None
    reused = False
    if destination.is_file() and not recalibrate:
        calibration = load_workload_calibration(
            destination,
            expected_key=key,
            hardware_fingerprint=hardware_fingerprint,
        )
        reused = True
    if calibration is None:
        probe_manifest, probe_pair = _probe_manifest(manifest)
        result_path = _vacant_temporary(manifest.parent, ".full-native-probe-result.")
        profile_output = _vacant_temporary(manifest.parent, ".full-native-probe-profile.")
        try:
            execution = run_engine_fn(
                probe_manifest,
                result_path,
                profile_path=profile_path,
                input_kind=FULL_VECTOR_INPUT,
                engine_profile_path=profile_output,
                pair_worker_limit=1,
            )
            peak = execution.get("peak_rss_bytes")
            wall = execution.get("wall_time_seconds")
            if isinstance(peak, bool) or not isinstance(peak, int) or peak <= 0:
                raise BenchmarkError("Full Native calibration did not measure positive peak RSS")
            if isinstance(wall, bool) or not isinstance(wall, int | float) or wall <= 0:
                raise BenchmarkError("Full Native calibration did not measure positive wall time")
            calibration = create_workload_calibration(
                destination,
                key=key,
                identity=identity,
                hardware_fingerprint=hardware_fingerprint,
                probe_pair=probe_pair,
                probe_peak_rss_bytes=peak,
                probe_wall_time_seconds=float(wall),
                requested_cpu_processes=requested_workers,
                memory_cap_bytes=memory_cap_bytes,
            )
        finally:
            probe_manifest.unlink(missing_ok=True)
            result_path.unlink(missing_ok=True)
            profile_output.unlink(missing_ok=True)

    probe = calibration["probe"]
    admission = calibrated_admission(
        probe_peak_rss_bytes=int(probe["peak_rss_bytes"]),
        requested_cpu_processes=requested_workers,
        memory_cap_bytes=memory_cap_bytes,
    )
    return {
        "schema_version": "1.0.0",
        "key": key,
        "path": str(destination),
        "reused": reused,
        "probe_pair": probe["pair"],
        "probe_peak_rss_bytes": probe["peak_rss_bytes"],
        "probe_wall_time_seconds": probe["wall_time_seconds"],
        "worker_limit": admission["safe_processes"],
        "admission": admission,
    }


def _probe_manifest(source: Path) -> tuple[Path, str]:
    document = read_json(source)
    if not isinstance(document, dict) or document.get("schema_version") != (
        "full-native-vector-manifest-v1"
    ):
        raise BenchmarkError("Full Native calibration requires a v1 manifest")
    pairs = document.get("pairs")
    frames = document.get("frames")
    programs = document.get("programs")
    if not isinstance(pairs, list) or not pairs or not isinstance(frames, list):
        raise BenchmarkError("Full Native calibration manifest has no pair/frame inventory")
    if not isinstance(programs, dict):
        raise BenchmarkError("Full Native calibration manifest has no program inventory")
    frame_rows: dict[str, int] = {}
    frame_identities: set[tuple[str, str]] = set()
    for frame in frames:
        if not isinstance(frame, dict) or not isinstance(frame.get("identity"), dict):
            raise BenchmarkError("Full Native calibration frame inventory is invalid")
        identity = frame["identity"]
        pair = identity.get("pair")
        timeframe = identity.get("timeframe")
        rows = frame.get("rows")
        if (
            not isinstance(pair, str)
            or not isinstance(timeframe, str)
            or isinstance(rows, bool)
            or not isinstance(rows, int)
            or rows <= 0
        ):
            raise BenchmarkError("Full Native calibration frame identity/rows are invalid")
        frame_identities.add((pair, timeframe))
        frame_rows[pair] = frame_rows.get(pair, 0) + rows
    ranked: list[tuple[int, str, str, dict[str, Any]]] = []
    for pair_document in pairs:
        identity = pair_document.get("identity") if isinstance(pair_document, dict) else None
        if not isinstance(identity, dict):
            raise BenchmarkError("Full Native calibration pair identity is invalid")
        pair = identity.get("pair")
        timeframe = identity.get("timeframe")
        if not isinstance(pair, str) or not isinstance(timeframe, str):
            raise BenchmarkError("Full Native calibration pair identity is invalid")
        if (pair, timeframe) not in frame_identities:
            raise BenchmarkError("Full Native calibration pair has no base frame")
        ranked.append((frame_rows[pair], pair, timeframe, pair_document))
    _, probe_pair, _, pair_document = max(ranked, key=lambda item: (item[0], item[1]))

    literal_frames = _literal_frame_identities(source.parent, programs)
    selected_frames = [
        frame
        for frame in frames
        if frame["identity"]["pair"] == probe_pair
        or (frame["identity"]["pair"], frame["identity"]["timeframe"])
        in literal_frames
    ]
    probe = dict(document)
    probe["pairs"] = [pair_document]
    probe["frames"] = selected_frames
    futures = document.get("futures")
    if futures is None:
        probe["futures"] = None
    elif isinstance(futures, list):
        if not all(isinstance(item, dict) for item in futures):
            raise BenchmarkError("Full Native calibration futures inventory is invalid")
        probe["futures"] = [item for item in futures if item.get("pair") == probe_pair]
    else:
        raise BenchmarkError("Full Native calibration futures inventory is invalid")
    destination = _vacant_temporary(source.parent, ".full-native-probe-manifest.")
    write_json(destination, probe)
    return destination, probe_pair


def _literal_frame_identities(root: Path, programs: dict[str, Any]) -> set[tuple[str, str]]:
    indicator = programs.get("indicator")
    artifact = indicator.get("artifact") if isinstance(indicator, dict) else None
    relative = artifact.get("path") if isinstance(artifact, dict) else None
    if not isinstance(relative, str):
        raise BenchmarkError("Full Native indicator artifact is missing")
    program = read_json(root / relative)
    nodes = program.get("nodes") if isinstance(program, dict) else None
    if not isinstance(nodes, list):
        raise BenchmarkError("Full Native indicator node inventory is invalid")
    result: set[tuple[str, str]] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("op") != "frame-source":
            continue
        parameters = node.get("parameters")
        pair = parameters.get("pair") if isinstance(parameters, dict) else None
        timeframe = parameters.get("timeframe") if isinstance(parameters, dict) else None
        if isinstance(pair, dict) and pair.get("kind") == "literal":
            value = pair.get("value")
            if not isinstance(value, str) or not isinstance(timeframe, str):
                raise BenchmarkError("Full Native literal frame binding is invalid")
            result.add((value, timeframe))
    return result


def _vacant_temporary(directory: Path, prefix: str) -> Path:
    with tempfile.NamedTemporaryFile(
        prefix=prefix,
        suffix=".json",
        dir=directory,
        delete=False,
    ) as handle:
        path = Path(handle.name)
    path.unlink()
    return path
