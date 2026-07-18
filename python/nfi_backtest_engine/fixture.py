"""Benchmark fixture manifest validation and sealing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import SpecValidationError
from .specs import validate_fixture_manifest, validate_trade_surface
from .state_trace import trace_summary


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_fixture(
    manifest_path: str | Path,
    *,
    verify_hashes: bool = True,
    validate_trace_semantics: bool = True,
) -> dict[str, Any]:
    """Validate schema, semantic rules, file boundaries, hashes, and surface artifact."""
    manifest_file = Path(manifest_path).resolve()
    manifest = read_json(manifest_file)
    validate_fixture_manifest(manifest)
    root = manifest_file.parent

    references = [*manifest["inputs"], *manifest["artifacts"].values()]
    seen: set[str] = set()
    for reference in references:
        relative = reference["path"]
        if relative in seen:
            raise SpecValidationError(f"duplicate fixture file reference: {relative}")
        seen.add(relative)
        target = _safe_fixture_path(root, relative)
        if not target.is_file():
            raise SpecValidationError(f"fixture file does not exist: {relative}")
        if target.stat().st_size != reference["bytes"]:
            raise SpecValidationError(
                f"{relative}: byte size differs; expected {reference['bytes']}, "
                f"actual {target.stat().st_size}"
            )
        if verify_hashes:
            actual_hash = sha256_file(target)
            if actual_hash != reference["sha256"]:
                raise SpecValidationError(
                    f"{relative}: SHA-256 differs; expected {reference['sha256']}, "
                    f"actual {actual_hash}"
                )

    surface_path = _safe_fixture_path(root, manifest["artifacts"]["trade_surface"]["path"])
    validate_trade_surface(read_json(surface_path))
    if manifest["schema_version"] == "2.0.0" and validate_trace_semantics:
        strategy = _one_input(manifest["inputs"], "strategy")
        config = _one_input(manifest["inputs"], "config")
        expected_input_hash = fixture_input_sha256(manifest["inputs"])
        trace_names = ["state_trace"]
        if "state_projection" in manifest["artifacts"]:
            trace_names.append("state_projection")
        for trace_name in trace_names:
            trace_path = _safe_fixture_path(
                root,
                manifest["artifacts"][trace_name]["path"],
            )
            trace = trace_summary(trace_path)
            _validate_trace_binding(
                trace,
                trace_name=trace_name,
                strategy_sha256=strategy["sha256"],
                config_sha256=config["sha256"],
                input_sha256=expected_input_hash,
                trading_mode=manifest["freqtrade"]["trading_mode"],
            )
    return manifest


def _validate_trace_binding(
    trace: dict[str, Any],
    *,
    trace_name: str,
    strategy_sha256: str,
    config_sha256: str,
    input_sha256: str,
    trading_mode: str,
) -> None:
    if trace["strategy_sha256"] != strategy_sha256:
        raise SpecValidationError(
            f"{trace_name} strategy_sha256 does not match the sealed strategy input"
        )
    if trace["profile_sha256"] != config_sha256:
        raise SpecValidationError(
            f"{trace_name} profile_sha256 does not match the sealed config input"
        )
    if trace["input_sha256"] != input_sha256:
        raise SpecValidationError(
            f"{trace_name} input_sha256 does not match the sealed fixture inputs"
        )
    if trace["trading_mode"] != trading_mode:
        raise SpecValidationError(
            f"{trace_name} trading_mode does not match the fixture manifest"
        )


def seal_fixture(manifest_path: str | Path) -> dict[str, Any]:
    """Refresh declared file byte counts and hashes, then validate the sealed fixture."""
    manifest_file = Path(manifest_path).resolve()
    manifest = read_json(manifest_file)
    validate_fixture_manifest(manifest)
    root = manifest_file.parent
    for reference in [*manifest["inputs"], *manifest["artifacts"].values()]:
        target = _safe_fixture_path(root, reference["path"])
        if not target.is_file():
            raise SpecValidationError(f"fixture file does not exist: {reference['path']}")
        reference["bytes"] = target.stat().st_size
        reference["sha256"] = sha256_file(target)
    write_json(manifest_file, manifest)
    return validate_fixture(manifest_file)


def _safe_fixture_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise SpecValidationError(f"fixture path must be relative: {relative}")
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise SpecValidationError(f"fixture path escapes its directory: {relative}")
    return target


def fixture_input_sha256(inputs: list[dict[str, Any]]) -> str:
    """Hash the ordered, behavior-affecting input identity without file contents in memory."""
    identity = [
        {
            "role": item["role"],
            "path": item["path"],
            "sha256": item["sha256"],
            "bytes": item["bytes"],
        }
        for item in sorted(inputs, key=lambda item: (item["role"], item["path"]))
    ]
    payload = json.dumps(
        identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _one_input(inputs: list[dict[str, Any]], role: str) -> dict[str, Any]:
    matches = [item for item in inputs if item["role"] == role]
    if len(matches) != 1:
        raise SpecValidationError(f"fixture requires exactly one {role!r} input")
    return matches[0]
