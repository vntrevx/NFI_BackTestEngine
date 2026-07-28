#!/usr/bin/env python3
"""Validate and materialize the declarative release-candidate workload plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"
REQUIRED_MODE_CONTRACTS = frozenset({"binance-spot", "binance-usdtm-isolated"})
REQUIRED_MODE_SLUGS = {
    "spot": "binance-spot",
    "futures": "binance-usdtm-isolated",
}
_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def load_release_candidate_contract(
    path: str | Path,
    *,
    repository_root: str | Path,
) -> dict[str, Any]:
    """Return a normalized plan after validating every referenced fixture."""
    root = Path(repository_root).resolve()
    contract_path = Path(path).resolve()
    document = _read_json(contract_path, label="release candidate contract")
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema_version",
            "platform_evidence",
            "certification_probes",
        }
        or document.get("schema_version") != SCHEMA_VERSION
    ):
        raise ValueError("release candidate contract fields or version differ")
    platform = document["platform_evidence"]
    if (
        not isinstance(platform, dict)
        or set(platform) != {"runs", "timeout_seconds", "modes"}
        or not isinstance(platform["runs"], int)
        or isinstance(platform["runs"], bool)
        or platform["runs"] < 3
        or platform["runs"] > 5
        or not isinstance(platform["timeout_seconds"], int)
        or isinstance(platform["timeout_seconds"], bool)
        or platform["timeout_seconds"] < 1
        or not isinstance(platform["modes"], list)
    ):
        raise ValueError("platform evidence contract is invalid")

    normalized_modes = [_validate_mode(item, repository_root=root) for item in platform["modes"]]
    slugs = {item["slug"] for item in normalized_modes}
    mode_contracts = {item["mode_contract"] for item in normalized_modes}
    strategy_hashes = {item["strategy_sha256"] for item in normalized_modes}
    if len(slugs) != len(normalized_modes):
        raise ValueError("release candidate mode slugs must be unique")
    if mode_contracts != REQUIRED_MODE_CONTRACTS:
        raise ValueError("release candidate must contain exact Spot and Futures modes")
    if len(strategy_hashes) != 1:
        raise ValueError("Spot and Futures platform fixtures must share one strategy SHA")
    normalized_modes.sort(key=lambda item: item["slug"])
    certification_probes = _validate_certification_probes(
        document["certification_probes"],
        repository_root=root,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": {
            "path": contract_path.relative_to(root).as_posix(),
            "sha256": _sha256_file(contract_path),
        },
        "platform_evidence": {
            "runs": platform["runs"],
            "timeout_seconds": platform["timeout_seconds"],
            "strategy_sha256": next(iter(strategy_hashes)),
            "modes": normalized_modes,
        },
        "certification_probes": certification_probes,
    }


def _validate_mode(
    value: Any,
    *,
    repository_root: Path,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"slug", "mode_contract", "manifest"}
        or not isinstance(value["slug"], str)
        or _SLUG_PATTERN.fullmatch(value["slug"]) is None
        or value["mode_contract"] not in REQUIRED_MODE_CONTRACTS
    ):
        raise ValueError("release candidate mode entry is invalid")
    manifest_relative = _safe_relative_path(value["manifest"], label="fixture manifest")
    manifest_path = (repository_root / manifest_relative).resolve()
    if not manifest_path.is_relative_to(repository_root) or not manifest_path.is_file():
        raise ValueError(f"fixture manifest does not exist: {manifest_relative}")
    manifest = _read_json(manifest_path, label="fixture manifest")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "3.0.0":
        raise ValueError(f"release platform fixture must use schema v3: {manifest_relative}")
    _validate_fixture_records(
        manifest,
        manifest_path=manifest_path,
        label=f"release platform fixture {manifest_relative}",
    )
    freqtrade = manifest.get("freqtrade")
    actual_mode = _mode_contract_for_freqtrade(freqtrade)
    if actual_mode != value["mode_contract"]:
        raise ValueError(f"fixture mode differs from release contract: {manifest_relative}")
    strategy_records = [
        item
        for item in manifest.get("inputs", [])
        if isinstance(item, dict) and item.get("role") == "strategy"
    ]
    if len(strategy_records) != 1:
        raise ValueError(f"fixture must contain exactly one strategy input: {manifest_relative}")
    strategy = strategy_records[0]
    strategy_relative = _safe_relative_path(
        strategy.get("path"),
        label="fixture strategy",
    )
    strategy_path = (manifest_path.parent / strategy_relative).resolve()
    expected_sha = strategy.get("sha256")
    if (
        not strategy_path.is_relative_to(manifest_path.parent)
        or not strategy_path.is_file()
        or not isinstance(expected_sha, str)
        or _SHA256_PATTERN.fullmatch(expected_sha) is None
        or _sha256_file(strategy_path) != expected_sha
    ):
        raise ValueError(f"fixture strategy input failed hash validation: {manifest_relative}")
    return {
        "slug": value["slug"],
        "mode_contract": value["mode_contract"],
        "manifest": manifest_relative.as_posix(),
        "candidate_evidence": (f"platform/{value['slug']}/platform-evidence.json"),
        "fixture_id": manifest.get("fixture_id"),
        "manifest_sha256": _sha256_file(manifest_path),
        "strategy_sha256": expected_sha,
    }


def _validate_certification_probes(
    value: Any,
    *,
    repository_root: Path,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "upstream_repository",
            "upstream_commit",
            "base_source_sha256",
            "modes",
        }
        or not isinstance(value["upstream_repository"], str)
        or not value["upstream_repository"]
        or not isinstance(value["upstream_commit"], str)
        or re.fullmatch(r"[0-9a-f]{40}", value["upstream_commit"]) is None
        or not isinstance(value["base_source_sha256"], str)
        or _SHA256_PATTERN.fullmatch(value["base_source_sha256"]) is None
        or not isinstance(value["modes"], list)
    ):
        raise ValueError("certification probe contract is invalid")

    normalized_modes: list[dict[str, Any]] = []
    fixture_ids: set[str] = set()
    manifest_paths: set[str] = set()
    for mode in value["modes"]:
        if (
            not isinstance(mode, dict)
            or set(mode)
            != {
                "slug",
                "mode_contract",
                "required_manifests",
                "manifests",
            }
            or not isinstance(mode["slug"], str)
            or _SLUG_PATTERN.fullmatch(mode["slug"]) is None
            or mode["mode_contract"] not in REQUIRED_MODE_CONTRACTS
            or not isinstance(mode["required_manifests"], int)
            or isinstance(mode["required_manifests"], bool)
            or mode["required_manifests"] < 1
            or not isinstance(mode["manifests"], list)
            or len(mode["manifests"]) != mode["required_manifests"]
        ):
            raise ValueError("certification probe mode entry is invalid")
        records = [
            _validate_certification_manifest(
                manifest,
                repository_root=repository_root,
                expected_mode=mode["mode_contract"],
                upstream_repository=value["upstream_repository"],
                upstream_commit=value["upstream_commit"],
                base_source_sha256=value["base_source_sha256"],
            )
            for manifest in mode["manifests"]
        ]
        for record in records:
            if record["manifest"] in manifest_paths:
                raise ValueError("certification probe manifests must be unique")
            if record["fixture_id"] in fixture_ids:
                raise ValueError("certification probe fixture IDs must be unique")
            manifest_paths.add(record["manifest"])
            fixture_ids.add(record["fixture_id"])
        normalized_modes.append(
            {
                "slug": mode["slug"],
                "mode_contract": mode["mode_contract"],
                "required_manifests": mode["required_manifests"],
                "manifests": records,
            }
        )
    if {
        item["slug"]: item["mode_contract"] for item in normalized_modes
    } != REQUIRED_MODE_SLUGS:
        raise ValueError(
            "certification probes must contain exact unique Spot and Futures modes"
        )
    normalized_modes.sort(key=lambda item: item["slug"])
    return {
        "upstream_repository": value["upstream_repository"],
        "upstream_commit": value["upstream_commit"],
        "base_source_sha256": value["base_source_sha256"],
        "modes": normalized_modes,
    }


def _validate_certification_manifest(
    value: Any,
    *,
    repository_root: Path,
    expected_mode: str,
    upstream_repository: str,
    upstream_commit: str,
    base_source_sha256: str,
) -> dict[str, Any]:
    relative = _safe_relative_path(value, label="certification probe manifest")
    path = (repository_root / relative).resolve()
    if not path.is_relative_to(repository_root) or not path.is_file():
        raise ValueError(f"certification probe manifest does not exist: {relative}")
    manifest = _read_json(path, label="certification probe manifest")
    provenance = manifest.get("strategy_provenance") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "3.0.0"
        or manifest.get("evidence_status") != "captured"
        or manifest.get("fixture_kind") != "x7-branch-probe"
        or _mode_contract_for_freqtrade(manifest.get("freqtrade")) != expected_mode
        or not isinstance(provenance, dict)
        or provenance.get("upstream_repository") != upstream_repository
        or provenance.get("upstream_commit") != upstream_commit
        or provenance.get("base_source_sha256") != base_source_sha256
    ):
        raise ValueError(
            f"certification probe identity differs: {relative.as_posix()}"
        )
    _validate_fixture_records(
        manifest,
        manifest_path=path,
        label=f"certification probe {relative.as_posix()}",
    )
    fixture_id = manifest.get("fixture_id")
    probe_kind = manifest.get("probe_kind")
    effective_sha = provenance.get("effective_source_sha256")
    if (
        not isinstance(fixture_id, str)
        or not fixture_id
        or not isinstance(probe_kind, str)
        or not probe_kind
        or not isinstance(effective_sha, str)
        or _SHA256_PATTERN.fullmatch(effective_sha) is None
    ):
        raise ValueError(
            f"certification probe metadata is incomplete: {relative.as_posix()}"
        )
    return {
        "manifest": relative.as_posix(),
        "manifest_sha256": _sha256_file(path),
        "fixture_id": fixture_id,
        "probe_kind": probe_kind,
        "effective_strategy_sha256": effective_sha,
    }


def _validate_fixture_records(
    manifest: dict[str, Any],
    *,
    manifest_path: Path,
    label: str,
) -> None:
    inputs = manifest.get("inputs")
    artifacts = manifest.get("artifacts")
    if (
        not isinstance(inputs, list)
        or not inputs
        or not isinstance(artifacts, dict)
        or not artifacts
    ):
        raise ValueError(f"{label} has no sealed inputs or artifacts")
    strategy_hashes: list[str] = []
    for index, record in enumerate(inputs):
        digest = _validate_fixture_record(
            record,
            root=manifest_path.parent,
            label=f"{label} input {index}",
        )
        if isinstance(record, dict) and record.get("role") == "strategy":
            strategy_hashes.append(digest)
    for name, record in artifacts.items():
        _validate_fixture_record(
            record,
            root=manifest_path.parent,
            label=f"{label} artifact {name}",
        )
    provenance = manifest.get("strategy_provenance")
    if (
        len(strategy_hashes) != 1
        or not isinstance(provenance, dict)
        or provenance.get("effective_source_sha256") != strategy_hashes[0]
    ):
        raise ValueError(f"{label} strategy identity differs")


def _validate_fixture_record(
    value: Any,
    *,
    root: Path,
    label: str,
) -> str:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("path"), str)
        or not isinstance(value.get("sha256"), str)
        or _SHA256_PATTERN.fullmatch(value["sha256"]) is None
        or not isinstance(value.get("bytes"), int)
        or isinstance(value.get("bytes"), bool)
        or value["bytes"] < 0
    ):
        raise ValueError(f"{label} record is invalid")
    relative = _safe_relative_path(value["path"], label=label)
    path = (root / relative).resolve()
    if (
        not path.is_relative_to(root)
        or not path.is_file()
        or path.stat().st_size != value["bytes"]
        or _sha256_file(path) != value["sha256"]
    ):
        raise ValueError(f"{label} failed hash validation")
    return value["sha256"]


def _mode_contract_for_freqtrade(value: Any) -> str:
    if not isinstance(value, dict):
        raise ValueError("fixture freqtrade identity is missing")
    if (
        value.get("trading_mode") == "spot"
        and value.get("margin_mode") in {"", None}
        and value.get("exchange") == "binance"
    ):
        return "binance-spot"
    if (
        value.get("trading_mode") == "futures"
        and value.get("margin_mode") == "isolated"
        and value.get("exchange") == "binance"
    ):
        return "binance-usdtm-isolated"
    raise ValueError("fixture is outside the supported release mode contracts")


def _safe_relative_path(value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} path is invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError(f"{label} path must be normalized and repository-relative")
    return path


def _read_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not readable JSON: {path}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path(".github/release-candidate-contract.json"),
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = load_release_candidate_contract(
        args.contract,
        repository_root=args.repository_root,
    )
    args.output.write_text(
        json.dumps(
            plan,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        "release candidate contract valid: "
        f"modes={len(plan['platform_evidence']['modes'])}, "
        f"strategy={plan['platform_evidence']['strategy_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
