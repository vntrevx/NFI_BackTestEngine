from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .canonical import read_json
from .changed_signal_capture_validation import RawSignalValidation, validate_raw_signal
from .changed_signal_json import canonical_sha256
from .changed_signal_manifest_roles import Mode
from .changed_signal_reconstruction import (
    reconstruct_native_lane,
    reconstruct_official_lane,
    reconstruct_projection_rows,
)
from .changed_signal_replay_validation import ReplayRootValidation, validate_replay_root
from .errors import SpecValidationError
from .fixture import fixture_input_sha256
from .state_trace import read_state_trace

_REPOSITORY: Final = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class LaneProvenanceValidation:
    """Raw and normalized evidence required to authenticate one producer lane."""

    mode: Mode
    provenance: Mapping[str, Any]
    lane: Mapping[str, Any]
    capture: Mapping[str, Any]
    official: bool


def validate_lane_provenance(validation: LaneProvenanceValidation) -> None:
    """Authenticate one raw producer lane and its independently normalized output."""
    mode = validation.mode
    provenance = validation.provenance
    lane = validation.lane
    capture = validation.capture
    official = validation.official
    expected_producer = "freqtrade-backtesting" if official else "nfi-native-engine"
    if provenance.get("producer") != expected_producer:
        raise SpecValidationError("changed signal lane producer differs")
    expected_command = [
        "uv",
        "run",
        "python",
        "-m",
        "nfi_backtest_engine.changed_signal_replay",
        mode,
        "--official" if official else "--native",
    ]
    if provenance["command"] != expected_command:
        raise SpecValidationError("changed signal replay command differs")
    hashes: list[str] = []
    roles: set[str] = set()
    for artifact in provenance["artifacts"]:
        path = _repository_path(artifact["path"])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if (
            digest != artifact["sha256"]
            or path.stat().st_size != artifact["bytes"]
            or artifact["role"] in roles
        ):
            raise SpecValidationError("changed signal raw artifact identity differs")
        roles.add(artifact["role"])
        hashes.append(digest)
    if provenance["raw_output_sha256"] != canonical_sha256(hashes):
        raise SpecValidationError("changed signal raw output identity differs")
    if provenance["normalized_sha256"] != canonical_sha256(lane):
        raise SpecValidationError("changed signal normalized output identity differs")
    role = "official_signal" if official else "native_signal"
    validate_raw_signal(
        RawSignalValidation(
            mode=mode,
            raw=_read_artifact(provenance, role),
            lane=lane,
            capture=capture,
            official=official,
        )
    )
    validate_replay_root(
        ReplayRootValidation(
            repository_root=_REPOSITORY,
            mode=mode,
            provenance=provenance,
            capture=capture,
            official=official,
        )
    )
    _validate_reconstructed_lane(provenance, lane, official=official)


def _validate_reconstructed_lane(
    provenance: Mapping[str, Any],
    lane: Mapping[str, Any],
    *,
    official: bool,
) -> None:
    manifest_path = _artifact_path(provenance, "replay_manifest")
    manifest = read_json(manifest_path)
    if official:
        _validate_manifest_output(
            provenance, manifest_path, ("official_execution", "freqtrade_result")
        )
        _validate_manifest_output(
            provenance, manifest_path, ("official_trace", "state_trace")
        )
        _validate_manifest_output(
            provenance, manifest_path, ("official_state", "state_projection")
        )
        _validate_trace_identity(
            provenance,
            manifest,
            "official_trace",
            "freqtrade-reference",
        )
        reconstructed = reconstruct_official_lane(
            manifest_path,
            _artifact_path(provenance, "official_execution"),
            _artifact_path(provenance, "official_trace"),
        )
        projection_role = "official_state"
    else:
        _validate_trace_identity(
            provenance,
            manifest,
            "native_state",
            "engine-projection",
        )
        reconstructed = reconstruct_native_lane(
            manifest_path,
            _artifact_path(provenance, "native_execution"),
            _artifact_path(provenance, "native_events"),
        )
        projection_role = "native_state"
    if reconstructed.trades != lane["trades"]:
        raise SpecValidationError("changed signal normalized trades are not execution-derived")
    if reconstructed.full_state != lane["full_state"]:
        raise SpecValidationError("changed signal normalized state is not raw-trace-derived")
    if reconstruct_projection_rows(_artifact_path(provenance, projection_role)) != (
        reconstructed.full_state
    ):
        raise SpecValidationError("changed signal state projection cache differs")


def _validate_manifest_output(
    provenance: Mapping[str, Any],
    manifest_path: Path,
    roles: tuple[str, str],
) -> None:
    proof_role, manifest_role = roles
    record = read_json(manifest_path)["artifacts"][manifest_role]
    path = (manifest_path.parent / record["path"]).resolve()
    artifact = next(
        (item for item in provenance["artifacts"] if item["role"] == proof_role),
        None,
    )
    if artifact is None or (
        _artifact_path(provenance, proof_role) != path
        or artifact["sha256"] != record["sha256"]
        or artifact["bytes"] != record["bytes"]
    ):
        raise SpecValidationError("changed signal replay output role differs")


def _validate_trace_identity(
    provenance: Mapping[str, Any],
    manifest: Mapping[str, Any],
    role: str,
    source: str,
) -> None:
    trace = read_state_trace(_artifact_path(provenance, role))
    expected = {
        "source": source,
        "input_sha256": fixture_input_sha256(manifest["inputs"]),
        "strategy_sha256": _artifact_sha256(provenance, "strategy_input"),
        "profile_sha256": _artifact_sha256(provenance, "config_input"),
        "trading_mode": manifest["freqtrade"]["trading_mode"],
    }
    if any(trace.header.get(key) != value for key, value in expected.items()):
        raise SpecValidationError("changed signal state producer binding differs")


def _read_artifact(provenance: Mapping[str, Any], role: str) -> dict[str, Any]:
    records = [item for item in provenance["artifacts"] if item["role"] == role]
    if len(records) != 1:
        raise SpecValidationError("changed signal required artifact role differs")
    try:
        value = json.loads(_artifact_path(provenance, role).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecValidationError("changed signal JSON artifact is malformed") from exc
    if not isinstance(value, dict):
        raise SpecValidationError("changed signal raw artifact is not an object")
    return value


def _artifact_sha256(provenance: Mapping[str, Any], role: str) -> str:
    records = [item for item in provenance["artifacts"] if item["role"] == role]
    if len(records) != 1:
        raise SpecValidationError("changed signal required artifact role differs")
    return records[0]["sha256"]


def _artifact_path(provenance: Mapping[str, Any], role: str) -> Path:
    records = [item for item in provenance["artifacts"] if item["role"] == role]
    if len(records) != 1:
        raise SpecValidationError("changed signal required artifact role differs")
    return _repository_path(records[0]["path"])


def _repository_path(value: str) -> Path:
    path = (_REPOSITORY / value).resolve()
    if not path.is_relative_to(_REPOSITORY) or not path.is_file():
        raise SpecValidationError("changed signal artifact path escapes repository")
    return path

