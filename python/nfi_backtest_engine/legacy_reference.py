"""Digest-pinned Official runtime qualification for legacy NFI sources."""

from __future__ import annotations

import ast
import json
import re
import shutil
from collections.abc import Callable, Mapping
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import read_json, write_json
from .docker_runtime import (
    docker_root_with_bind_owner_arguments,
    run_managed_container,
)
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file, validate_fixture
from .reference.execution import ensure_docker_config

LEGACY_RUNTIME_REGISTRY_VERSION = "legacy-reference-runtime-registry-v1"
LEGACY_QUALIFICATION_SPEC_VERSION = "legacy-reference-qualification-spec-v1"
LEGACY_RUNTIME_REGISTRY_RESOURCE = "legacy-reference-runtimes-v1.json"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]{4}\.[0-9]+(?:\.[0-9]+)?$")
_FAMILIES = {
    "NostalgiaForInfinityNext": "V8",
    "NostalgiaForInfinityNextGen": "V9",
}


def load_legacy_runtime_registry(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the packaged or explicitly supplied legacy runtime registry."""
    document = read_json(Path(path)) if path is not None else json.loads(
        files("nfi_backtest_engine.contracts")
        .joinpath(LEGACY_RUNTIME_REGISTRY_RESOURCE)
        .read_text(encoding="utf-8")
    )
    _validate_registry(document)
    return document


def legacy_runtime_for_source(
    family: str,
    source_sha256: str,
    *,
    registry: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a qualified runtime only for the exact sealed legacy source."""
    document = dict(registry) if registry is not None else load_legacy_runtime_registry()
    _validate_registry(document)
    for record in document["strategies"]:
        if record["family"] == family and record["source_sha256"] == source_sha256:
            return dict(record)
    return None


def qualify_legacy_runtimes(
    spec_path: str | Path,
    output_path: str | Path,
    *,
    timeout_seconds: int = 900,
    runner: Callable[..., tuple[Any, dict[str, Any]]] = run_managed_container,
) -> dict[str, Any]:
    """Try ordered Official images and seal the first bounded success per source."""
    if timeout_seconds <= 0:
        raise BenchmarkError("legacy qualification timeout must be positive")
    spec_file = Path(spec_path).resolve()
    spec = read_json(spec_file)
    strategies, candidates, manifest_path = _validate_spec(spec, spec_file.parent)
    manifest = validate_fixture(manifest_path)
    destination = Path(output_path).resolve()
    if destination.exists():
        raise BenchmarkError(f"legacy runtime registry already exists: {destination}")
    work_root = destination.parent / f".{destination.name}.work"
    if work_root.exists():
        raise BenchmarkError(f"legacy qualification work directory already exists: {work_root}")
    work_root.mkdir(parents=True)
    docker_config = ensure_docker_config()
    selected: list[dict[str, Any]] = []
    try:
        for strategy in strategies:
            source = strategy["source"]
            attempts: list[dict[str, Any]] = []
            winner: dict[str, Any] | None = None
            for index, candidate in enumerate(candidates, start=1):
                attempt = work_root / strategy["generation"].lower() / f"attempt-{index:02d}"
                attempt.mkdir(parents=True)
                (attempt / "backtest_results").mkdir()
                source_root = attempt / "sealed-source"
                source_root.mkdir()
                staged_source = source_root / source.name
                shutil.copyfile(source, staged_source)
                if sha256_file(staged_source) != sha256_file(source):
                    raise BenchmarkError("legacy source staging changed sealed bytes")
                import_run = _run_phase(
                    runner,
                    phase="import",
                    candidate=candidate,
                    strategy=strategy,
                    manifest=manifest,
                    manifest_path=manifest_path,
                    attempt=attempt,
                    source_root=source_root,
                    docker_config=docker_config,
                    timeout_seconds=timeout_seconds,
                )
                backtest_run = (
                    _run_phase(
                        runner,
                        phase="backtest",
                        candidate=candidate,
                        strategy=strategy,
                        manifest=manifest,
                        manifest_path=manifest_path,
                        attempt=attempt,
                        source_root=source_root,
                        docker_config=docker_config,
                        timeout_seconds=timeout_seconds,
                    )
                    if import_run["exit_code"] == 0
                    else None
                )
                record = {
                    "version": candidate["version"],
                    "image_index_digest": candidate["image_index_digest"],
                    "image_platform_digest": candidate["image_platform_digest"],
                    "import": import_run,
                    "bounded_backtest": backtest_run,
                    "qualified": bool(
                        import_run["exit_code"] == 0
                        and backtest_run is not None
                        and backtest_run["exit_code"] == 0
                    ),
                }
                attempts.append(record)
                if record["qualified"]:
                    winner = candidate
                    break
            if winner is None:
                raise BenchmarkError(
                    f"LEGACY_REFERENCE_UNAVAILABLE: {strategy['family']} has no qualified runtime"
                )
            selected.append(
                {
                    "family": strategy["family"],
                    "generation": strategy["generation"],
                    "source_sha256": sha256_file(source),
                    "source_bytes": source.stat().st_size,
                    "runtime": winner,
                    "fixture_id": manifest["fixture_id"],
                    "fixture_manifest_sha256": sha256_file(manifest_path),
                    "network_requirement": "public-exchange-metadata",
                    "attempts": attempts,
                    "result_status": "official_only",
                    "native_supported": False,
                }
            )
        document = {
            "schema_version": LEGACY_RUNTIME_REGISTRY_VERSION,
            "strategies": selected,
        }
        _validate_registry(document)
        write_json(destination, document)
        return document
    finally:
        # Logs are represented by hashes in the registry; keep no mutable work tree.
        shutil.rmtree(work_root, ignore_errors=True)


def _validate_spec(
    document: Any,
    base: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], Path]:
    if not isinstance(document, dict) or document.get("schema_version") != (
        LEGACY_QUALIFICATION_SPEC_VERSION
    ):
        raise SpecValidationError("unsupported legacy qualification spec")
    manifest = _resolve_input(base, document.get("fixture_manifest"), "fixture manifest")
    raw_strategies = document.get("strategies")
    raw_candidates = document.get("candidates")
    if not isinstance(raw_strategies, list) or len(raw_strategies) != 2:
        raise SpecValidationError("legacy qualification requires exactly V8 and V9 sources")
    strategies: list[dict[str, Any]] = []
    for item in raw_strategies:
        if not isinstance(item, dict):
            raise SpecValidationError("legacy strategy record must be an object")
        family = item.get("family")
        generation = item.get("generation")
        if not isinstance(family, str) or _FAMILIES.get(family) != generation:
            raise SpecValidationError("legacy strategy family and generation differ")
        source = _resolve_input(base, item.get("source"), f"{family} source")
        classes = {
            node.name
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
            if isinstance(node, ast.ClassDef)
        }
        if family not in classes:
            raise SpecValidationError(f"legacy source does not define {family}")
        strategies.append({"family": family, "generation": generation, "source": source})
    if {item["family"] for item in strategies} != set(_FAMILIES):
        raise SpecValidationError("legacy qualification source set differs")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise SpecValidationError("legacy qualification requires image candidates")
    candidates: list[dict[str, str]] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            raise SpecValidationError("legacy runtime candidate must be an object")
        candidate = {key: item.get(key) for key in (
            "version", "image", "image_index_digest", "image_platform_digest", "platform"
        )}
        if (
            not isinstance(candidate["version"], str)
            or not _VERSION.fullmatch(candidate["version"])
            or candidate["image"] != "freqtradeorg/freqtrade"
            or not isinstance(candidate["image_index_digest"], str)
            or not _SHA256.fullmatch(candidate["image_index_digest"])
            or not isinstance(candidate["image_platform_digest"], str)
            or not _SHA256.fullmatch(candidate["image_platform_digest"])
            or candidate["platform"] != "linux/amd64"
        ):
            raise SpecValidationError("legacy runtime candidate identity is invalid")
        candidates.append({key: str(value) for key, value in candidate.items()})
    versions = [_version_key(item["version"]) for item in candidates]
    if versions != sorted(versions, reverse=True) or len(set(versions)) != len(versions):
        raise SpecValidationError("legacy runtime candidates must be unique and newest-first")
    return strategies, candidates, manifest


def _resolve_input(base: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise SpecValidationError(f"{label} path is missing")
    path = (base / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    if not path.is_file() or path.is_symlink():
        raise SpecValidationError(f"{label} must be a regular non-symlink file")
    return path


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _run_phase(
    runner: Callable[..., tuple[Any, dict[str, Any]]],
    *,
    phase: str,
    candidate: Mapping[str, str],
    strategy: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_path: Path,
    attempt: Path,
    source_root: Path,
    docker_config: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    fixture_root = manifest_path.parent
    arguments = [
        "--platform", candidate["platform"],
        "--network", "bridge",
        *docker_root_with_bind_owner_arguments(attempt),
        "--workdir", "/fixture",
        "--volume", f"{fixture_root}:/fixture:ro",
        "--volume", f"{source_root}:/strategy:ro",
        "--volume", f"{attempt}:/freqtrade/user_data",
        "--entrypoint", "freqtrade",
        f"{candidate['image']}@{candidate['image_platform_digest']}",
    ]
    if phase == "import":
        arguments.extend([
            "list-strategies", "--strategy-path", "/strategy",
            "--recursive-strategy-search", "--user-data-dir", "/freqtrade/user_data",
        ])
    else:
        arguments.extend(_bounded_backtest_args(manifest, strategy["family"]))
    completed, _resources = runner(
        arguments,
        docker_config=docker_config,
        role="legacy-qualification",
        memory_cap_bytes=4 * 1024**3,
        swap_mode="disabled",
        capture_output=True,
        timeout=timeout_seconds,
    )
    stdout = str(completed.stdout or "")
    stderr = str(completed.stderr or "")
    stdout_path = attempt / f"{phase}.stdout.log"
    stderr_path = attempt / f"{phase}.stderr.log"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return {
        "exit_code": int(completed.returncode),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_sha256": sha256_file(stderr_path),
    }


def _bounded_backtest_args(manifest: Mapping[str, Any], family: str) -> list[str]:
    raw = manifest.get("freqtrade", {}).get("command")
    if not isinstance(raw, list) or raw[:2] != ["freqtrade", "backtesting"]:
        raise SpecValidationError("qualification fixture must declare Freqtrade backtesting")
    values = [str(value) for value in raw[2:]]
    rewritten: list[str] = ["backtesting"]
    index = 0
    while index < len(values):
        value = values[index]
        if value in {"--strategy", "--strategy-path", "--user-data-dir"}:
            index += 2
            continue
        if value in {"--config", "--datadir"}:
            if index + 1 >= len(values):
                raise SpecValidationError(f"qualification fixture has incomplete {value}")
            relative = PurePosixPath(values[index + 1])
            if relative.is_absolute() or ".." in relative.parts:
                raise SpecValidationError("qualification fixture path escapes its root")
            rewritten.extend([value, f"/fixture/{relative}"])
            index += 2
            continue
        if value == "--export":
            rewritten.extend([value, "none"])
            index += 2
            continue
        rewritten.append(value)
        index += 1
    rewritten.extend([
        "--user-data-dir", "/freqtrade/user_data",
        "--strategy-path", "/strategy",
        "--strategy", family,
    ])
    return rewritten


def _validate_registry(document: Any) -> None:
    if not isinstance(document, dict) or document.get("schema_version") != (
        LEGACY_RUNTIME_REGISTRY_VERSION
    ):
        raise SpecValidationError("unsupported legacy runtime registry")
    records = document.get("strategies")
    if not isinstance(records, list) or len(records) != 2:
        raise SpecValidationError("legacy runtime registry requires V8 and V9")
    families: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise SpecValidationError("legacy runtime record must be an object")
        family = record.get("family")
        generation = record.get("generation")
        runtime = record.get("runtime")
        if (
            not isinstance(family, str)
            or _FAMILIES.get(family) != generation
            or not isinstance(record.get("source_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["source_sha256"])
            or record.get("result_status") != "official_only"
            or record.get("native_supported") is not False
            or record.get("network_requirement") != "public-exchange-metadata"
            or not isinstance(runtime, dict)
            or runtime.get("image") != "freqtradeorg/freqtrade"
            or not _SHA256.fullmatch(str(runtime.get("image_index_digest", "")))
            or not _SHA256.fullmatch(str(runtime.get("image_platform_digest", "")))
            or runtime.get("platform") != "linux/amd64"
        ):
            raise SpecValidationError("legacy runtime registry record is invalid")
        families.add(str(family))
    if families != set(_FAMILIES):
        raise SpecValidationError("legacy runtime registry family set differs")


__all__ = [
    "LEGACY_QUALIFICATION_SPEC_VERSION",
    "LEGACY_RUNTIME_REGISTRY_RESOURCE",
    "LEGACY_RUNTIME_REGISTRY_VERSION",
    "legacy_runtime_for_source",
    "load_legacy_runtime_registry",
    "qualify_legacy_runtimes",
]
