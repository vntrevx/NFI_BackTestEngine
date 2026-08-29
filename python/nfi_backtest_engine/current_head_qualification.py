"""Independent validation for a sealed current-HEAD qualification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Final

from .canonical import read_json
from .errors import SpecValidationError
from .parity import first_difference

_VERSION: Final = "current-head-qualification-v1"
_MODES: Final = ("spot", "futures")
_SURFACES: Final = ("signal_tag", "trade_surface", "full_state")


def validate_current_head_qualification(
    root: Path,
    document: Mapping[str, Any],
    expected_identity: Mapping[str, Any],
) -> None:
    """Recompute identity, artifact hashes, parity, and mode qualification."""
    if document.get("schema_version") != _VERSION:
        raise SpecValidationError("current-head qualification schema is unsupported")
    if document.get("identity") != expected_identity:
        raise SpecValidationError("current-head qualification identity differs")
    unsigned = {key: value for key, value in document.items() if key != "fingerprint"}
    if document.get("fingerprint") != _canonical_sha256(unsigned):
        raise SpecValidationError("current-head qualification fingerprint differs")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list):
        raise SpecValidationError("current-head qualification artifact manifest is invalid")
    sealed = _sealed_artifacts(root, artifacts)
    modes = document.get("modes")
    if not isinstance(modes, Mapping) or tuple(modes) != _MODES:
        raise SpecValidationError("current-head qualification mode inventory differs")
    targets = document.get("changed_target_ids")
    if not isinstance(targets, list) or not targets:
        raise SpecValidationError("current-head qualification target inventory is empty")
    mode_role_paths: dict[str, dict[str, set[str]]] = {name: {} for name in _MODES}
    for mode_name in _MODES:
        mode = modes[mode_name]
        if not isinstance(mode, Mapping):
            raise SpecValidationError("current-head qualification mode is invalid")
        if mode.get("trading_mode") != mode_name:
            raise SpecValidationError(
                f"current-head {mode_name} qualification trading mode differs"
            )
        if (
            mode.get("verification_state") != "quick_verified"
            or mode.get("target_gaps") != []
            or mode.get("unknown_compiler_constructs") != []
        ):
            raise SpecValidationError(f"current-head {mode_name} qualification is incomplete")
        official = mode.get("official")
        native = mode.get("native")
        if not isinstance(official, Mapping) or not isinstance(native, Mapping):
            raise SpecValidationError("current-head qualification lane inventory is invalid")
        surface_values: dict[str, Any] = {}
        for surface in _SURFACES:
            for lane_name, lane in (("official", official), ("native", native)):
                relative = lane.get(surface)
                surface_values[f"{lane_name}_{surface}"] = _artifact_document(
                    root, sealed, relative
                )
                if isinstance(relative, str):
                    mode_role_paths[mode_name].setdefault(relative, set()).add(
                        f"{lane_name}_{surface}"
                    )
            if first_difference(
                surface_values[f"official_{surface}"],
                surface_values[f"native_{surface}"],
            ) is not None:
                raise SpecValidationError(f"current-head {mode_name} {surface} parity differs")
            if mode.get(f"{surface}_first_difference") is not None:
                raise SpecValidationError(f"current-head {mode_name} {surface} claim differs")
        qualification: Mapping[str, Any] | None = None
        role_paths: dict[str, str] = {}
        for role in (
            "qualification",
            "fixture_manifest",
            "native_input",
            "native_result",
            "native_vector",
        ):
            relative = mode.get(role)
            if (
                not isinstance(relative, str)
                or relative not in sealed
                or mode.get(f"{role}_sha256") != sealed[relative]
            ):
                raise SpecValidationError(f"current-head {mode_name} {role} identity differs")
            role_paths[role] = relative
            mode_role_paths[mode_name].setdefault(relative, set()).add(role)
            if role == "qualification":
                candidate = _artifact_document(root, sealed, relative)
                if (
                    not isinstance(candidate, Mapping)
                    or candidate.get("trading_mode") != mode_name
                ):
                    raise SpecValidationError(
                        f"current-head {mode_name} qualification trading mode differs"
                    )
                qualification = candidate
        assert qualification is not None
        _validate_mode_provenance(
            root=root,
            mode_name=mode_name,
            sealed=sealed,
            role_paths=role_paths,
            official=official,
            native=native,
            surface_values=surface_values,
            qualification=qualification,
        )
    _validate_cross_mode_path_reuse(mode_role_paths)


def _validate_mode_provenance(
    *,
    root: Path,
    mode_name: str,
    sealed: Mapping[str, str],
    role_paths: Mapping[str, str],
    official: Mapping[str, Any],
    native: Mapping[str, Any],
    surface_values: Mapping[str, Any],
    qualification: Mapping[str, Any],
) -> None:
    role_relatives = {
        "fixture_manifest": role_paths["fixture_manifest"],
        **{
            f"{lane_name}_{surface}": str(lane[surface])
            for lane_name, lane in (("official", official), ("native", native))
            for surface in _SURFACES
        },
        "native_input": role_paths["native_input"],
        "native_result": role_paths["native_result"],
        "native_vector": role_paths["native_vector"],
    }
    expected_role_names = (
        "fixture_manifest",
        "official_signal_tag",
        "official_trade_surface",
        "official_full_state",
        "native_signal_tag",
        "native_trade_surface",
        "native_full_state",
        "native_input",
        "native_result",
        "native_vector",
    )
    if tuple(role_relatives) != expected_role_names:
        raise SpecValidationError(f"current-head {mode_name} mode provenance inventory differs")
    expected_roles = {role: sealed[relative] for role, relative in role_relatives.items()}
    provenance = qualification.get("mode_provenance")
    if not isinstance(provenance, Mapping) or provenance.get("roles") != expected_roles:
        raise SpecValidationError(f"current-head {mode_name} mode provenance differs")

    fixture = _artifact_document(root, sealed, role_paths["fixture_manifest"])
    freqtrade = fixture.get("freqtrade") if isinstance(fixture, Mapping) else None
    fixture_artifacts = fixture.get("artifacts") if isinstance(fixture, Mapping) else None
    fixture_trade = (
        fixture_artifacts.get("trade_surface")
        if isinstance(fixture_artifacts, Mapping)
        else None
    )
    fixture_projection = (
        fixture_artifacts.get("state_projection")
        if isinstance(fixture_artifacts, Mapping)
        else None
    )
    if (
        not isinstance(freqtrade, Mapping)
        or freqtrade.get("trading_mode") != mode_name
        or not isinstance(fixture_trade, Mapping)
        or fixture_trade.get("sha256") != expected_roles["official_trade_surface"]
        or not isinstance(fixture_projection, Mapping)
        or not isinstance(fixture_projection.get("sha256"), str)
    ):
        raise SpecValidationError(f"current-head {mode_name} fixture mode provenance differs")

    for lane_name in ("official", "native"):
        trade_surface = surface_values[f"{lane_name}_trade_surface"]
        context = trade_surface.get("context") if isinstance(trade_surface, Mapping) else None
        if not isinstance(context, Mapping) or context.get("trading_mode") != mode_name:
            raise SpecValidationError(
                f"current-head {mode_name} trade surface mode provenance differs"
            )

    native_input = _artifact_document(root, sealed, role_paths["native_input"])
    config = native_input.get("config") if isinstance(native_input, Mapping) else None
    pairs = native_input.get("pairs") if isinstance(native_input, Mapping) else None
    is_futures = mode_name == "futures"
    if (
        not isinstance(config, Mapping)
        or config.get("is_futures") is not is_futures
        or not isinstance(pairs, list)
        or not pairs
    ):
        raise SpecValidationError(f"current-head {mode_name} Native input mode provenance differs")
    pair_names: set[str] = set()
    vector_digests: set[str] = set()
    for pair in pairs:
        vector = pair.get("vector") if isinstance(pair, Mapping) else None
        pair_name = pair.get("pair") if isinstance(pair, Mapping) else None
        if (
            not isinstance(pair_name, str)
            or not isinstance(vector, Mapping)
            or pair.get("can_short") is not is_futures
            or not isinstance(vector.get("sha256"), str)
        ):
            raise SpecValidationError(
                f"current-head {mode_name} Native input mode provenance differs"
            )
        pair_names.add(pair_name)
        vector_digests.add(str(vector["sha256"]))
    if vector_digests != {expected_roles["native_vector"]}:
        raise SpecValidationError(f"current-head {mode_name} Native vector provenance differs")

    for lane_name in ("official", "native"):
        full_state = surface_values[f"{lane_name}_full_state"]
        if not isinstance(full_state, list) or not full_state or any(
            not isinstance(row, Mapping) or row.get("pair") not in pair_names
            for row in full_state
        ):
            raise SpecValidationError(
                f"current-head {mode_name} full-state mode provenance differs"
            )

    expected_run = {
        "trading_mode": mode_name,
        "input_sha256": expected_roles["native_input"],
        "result_sha256": expected_roles["native_result"],
        "vector_sha256": expected_roles["native_vector"],
        "trade_surface_sha256": expected_roles["native_trade_surface"],
        "full_state_sha256": expected_roles["native_full_state"],
        "official_full_state_sha256": expected_roles["official_full_state"],
        "fixture_projection_sha256": fixture_projection["sha256"],
    }
    if provenance.get("native_run") != expected_run:
        raise SpecValidationError(f"current-head {mode_name} Native run mode provenance differs")


def _validate_cross_mode_path_reuse(
    mode_role_paths: Mapping[str, Mapping[str, set[str]]],
) -> None:
    signal_roles = {"official_signal_tag", "native_signal_tag"}
    for path in mode_role_paths["spot"].keys() & mode_role_paths["futures"].keys():
        roles = mode_role_paths["spot"][path] | mode_role_paths["futures"][path]
        if not roles <= signal_roles:
            raise SpecValidationError("current-head cross-mode role path provenance differs")


def _sealed_artifacts(root: Path, records: list[Any]) -> dict[str, str]:
    sealed: dict[str, str] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise SpecValidationError("current-head qualification artifact record is invalid")
        relative = record.get("path")
        digest = record.get("sha256")
        if (
            not isinstance(relative, str)
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or relative in sealed
            or not isinstance(digest, str)
        ):
            raise SpecValidationError("current-head qualification artifact path is invalid")
        path = root / relative
        if path.is_symlink() or not path.is_file() or _sha256_file(path) != digest:
            raise SpecValidationError("current-head qualification artifact hash differs")
        sealed[relative] = digest
    return sealed


def _artifact_document(root: Path, sealed: Mapping[str, str], value: Any) -> Any:
    if not isinstance(value, str) or value not in sealed:
        raise SpecValidationError("current-head qualification artifact role is unsealed")
    return read_json(root / value)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
