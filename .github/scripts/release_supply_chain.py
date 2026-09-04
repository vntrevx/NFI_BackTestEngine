#!/usr/bin/env python3
"""Seal and verify build-once Python distribution supply-chain metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"
SPDX_VERSION = "SPDX-2.3"
CHANNELS = ("github-rc", "testpypi", "github-stable", "pypi")
_SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def seal_supply_chain(
    distribution_paths: Sequence[str | Path],
    output_directory: str | Path,
    *,
    project: str,
    version: str,
    candidate_commit: str,
    created_at: str,
) -> dict[str, Any]:
    """Write one SPDX document and one cross-channel byte identity graph."""
    if not project or not version or not created_at:
        raise ValueError("project, version, and created-at are required")
    if _SHA1_PATTERN.fullmatch(candidate_commit) is None:
        raise ValueError("candidate commit must be a full lowercase Git SHA")
    records = _distribution_records(distribution_paths)
    output = Path(output_directory)
    sbom_path = output / "nfi-backtest-engine.spdx.json"
    identity_path = output / "distribution-identity.json"
    if sbom_path.exists() or identity_path.exists():
        raise ValueError("supply-chain output already exists")
    output.mkdir(parents=True, exist_ok=True)

    distribution_root = _canonical_sha(records)
    namespace = (
        "https://github.com/vntrevx/NFI_BackTestEngine/releases/sbom/"
        f"{candidate_commit}/{distribution_root}"
    )
    packages = []
    relationships = []
    for index, record in enumerate(records, start=1):
        spdx_id = f"SPDXRef-Package-{index}"
        record["spdx_id"] = spdx_id
        packages.append(
            {
                "SPDXID": spdx_id,
                "name": project,
                "versionInfo": version,
                "packageFileName": record["filename"],
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "checksums": [
                    {"algorithm": "SHA256", "checksumValue": record["sha256"]}
                ],
                "primaryPackagePurpose": "LIBRARY",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:pypi/{project}@{version}",
                    }
                ],
            }
        )
        relationships.append(
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": spdx_id,
            }
        )
    sbom = {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{project}-{version}-python-distributions",
        "documentNamespace": namespace,
        "creationInfo": {
            "created": created_at,
            "creators": ["Tool: nfi-release-supply-chain/1.0.0"],
        },
        "packages": packages,
        "relationships": relationships,
    }
    _write_json(sbom_path, sbom)

    expected_hashes = {record["filename"]: record["sha256"] for record in records}
    graph_without_identity = {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "version": version,
        "candidate_commit": candidate_commit,
        "build_once": True,
        "distributions": records,
        "sbom": {
            "filename": sbom_path.name,
            "sha256": _sha256_file(sbom_path),
        },
        "channels": [
            {
                "slug": channel,
                "required": True,
                "expected_sha256": expected_hashes,
            }
            for channel in CHANNELS
        ],
    }
    graph = {
        **graph_without_identity,
        "identity_sha256": _canonical_sha(graph_without_identity),
    }
    _write_json(identity_path, graph)
    return graph


def verify_supply_chain(
    distribution_directory: str | Path,
    identity_path: str | Path,
    sbom_path: str | Path,
) -> dict[str, Any]:
    """Fail closed unless local distributions match the sealed channel graph."""
    directory = Path(distribution_directory)
    identity = _read_json(Path(identity_path))
    sbom_file = Path(sbom_path)
    if not isinstance(identity, dict) or identity.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported distribution identity graph")
    claimed_identity = identity.get("identity_sha256")
    body = {key: value for key, value in identity.items() if key != "identity_sha256"}
    if claimed_identity != _canonical_sha(body):
        raise ValueError("distribution identity graph hash differs")
    if identity.get("build_once") is not True:
        raise ValueError("distribution identity graph is not build-once")
    records = identity.get("distributions")
    if not isinstance(records, list):
        raise ValueError("distribution identity records are malformed")
    expected_names = {record.get("filename") for record in records if isinstance(record, dict)}
    actual_paths = sorted(
        [*directory.glob("*.whl"), *directory.glob("*.tar.gz")],
        key=lambda path: path.name,
    )
    if {path.name for path in actual_paths} != expected_names:
        raise ValueError("distribution file set differs from sealed identity")
    actual_records = _distribution_records(actual_paths)
    comparable = [
        {key: value for key, value in record.items() if key != "spdx_id"}
        for record in records
    ]
    if actual_records != comparable:
        raise ValueError("distribution bytes differ from sealed identity")
    if identity.get("sbom") != {
        "filename": sbom_file.name,
        "sha256": _sha256_file(sbom_file),
    }:
        raise ValueError("SPDX document differs from sealed identity")
    sbom = _read_json(sbom_file)
    if not isinstance(sbom, dict) or sbom.get("spdxVersion") != SPDX_VERSION:
        raise ValueError("unsupported SPDX document")
    sbom_hashes = {
        package.get("packageFileName"): package.get("checksums", [{}])[0].get(
            "checksumValue"
        )
        for package in sbom.get("packages", [])
        if isinstance(package, dict)
    }
    expected_hashes = {record["filename"]: record["sha256"] for record in records}
    if sbom_hashes != expected_hashes:
        raise ValueError("SPDX distribution hashes differ")
    expected_channels = [
        {"slug": channel, "required": True, "expected_sha256": expected_hashes}
        for channel in CHANNELS
    ]
    if identity.get("channels") != expected_channels:
        raise ValueError("cross-channel distribution policy differs")
    return identity


def _distribution_records(paths: Sequence[str | Path]) -> list[dict[str, Any]]:
    candidates = [Path(path) for path in paths]
    if len(candidates) < 2:
        raise ValueError("at least one wheel and one sdist are required")
    if len({path.name for path in candidates}) != len(candidates):
        raise ValueError("distribution filenames must be unique")
    wheel_count = sum(path.name.endswith(".whl") for path in candidates)
    sdist_count = sum(path.name.endswith(".tar.gz") for path in candidates)
    if wheel_count < 1 or sdist_count != 1 or wheel_count + sdist_count != len(candidates):
        raise ValueError("distribution set must contain wheels and exactly one tar.gz sdist")
    records = []
    for path in sorted(candidates, key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"distribution must be a regular non-symlink file: {path}")
        records.append(
            {
                "filename": path.name,
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return records


def _canonical_sha(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON document is unreadable: {path}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    seal = commands.add_parser("seal")
    seal.add_argument("--distribution", type=Path, action="append", required=True)
    seal.add_argument("--output-dir", type=Path, required=True)
    seal.add_argument("--project", required=True)
    seal.add_argument("--version", required=True)
    seal.add_argument("--candidate-commit", required=True)
    seal.add_argument("--created-at", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--directory", type=Path, required=True)
    verify.add_argument("--identity", type=Path, required=True)
    verify.add_argument("--sbom", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "seal":
        graph = seal_supply_chain(
            args.distribution,
            args.output_dir,
            project=args.project,
            version=args.version,
            candidate_commit=args.candidate_commit,
            created_at=args.created_at,
        )
        print(f"distribution supply chain sealed: {graph['identity_sha256']}")
    else:
        graph = verify_supply_chain(args.directory, args.identity, args.sbom)
        print(f"distribution supply chain valid: {graph['identity_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
