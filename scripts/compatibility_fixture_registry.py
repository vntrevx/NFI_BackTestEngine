#!/usr/bin/env python3
"""Select and safely materialize digest-bound compatibility fixture bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.fixture import validate_fixture

_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MODES = {"spot", "futures"}
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024


def select_bundle(
    registry: Mapping[str, Any],
    *,
    trading_mode: str,
    source_sha256: str,
    freqtrade_digest: str,
) -> dict[str, Any] | None:
    """Select at most one exact mode/source/oracle bundle."""
    bundles = _validate_registry(registry)
    matches = [
        bundle
        for bundle in bundles
        if bundle["trading_mode"] == trading_mode
        and bundle["source_sha256"] == source_sha256
        and bundle["freqtrade_image_digest"] == freqtrade_digest
    ]
    if len(matches) > 1:
        raise ValueError("compatibility fixture registry selection is ambiguous")
    return matches[0] if matches else None


def materialize_bundle(
    bundle: Mapping[str, Any],
    *,
    asset_path: str | Path,
    output_directory: str | Path,
) -> dict[str, Any]:
    """Verify one immutable asset, extract regular files, and validate fixtures."""
    asset = Path(asset_path).resolve()
    output = Path(output_directory).resolve()
    if not asset.is_file() or asset.stat().st_size != bundle["asset_bytes"]:
        raise ValueError("compatibility fixture asset size differs from registry")
    if _sha256_file(asset) != bundle["asset_sha256"]:
        raise ValueError("compatibility fixture asset digest differs from registry")
    if output.exists() and any(output.iterdir()):
        raise ValueError("compatibility fixture output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    with tarfile.open(asset, mode="r:gz") as archive:
        members = archive.getmembers()
        extracted_bytes = _validate_archive_members(members)
        if extracted_bytes != bundle["extracted_bytes"]:
            raise ValueError("compatibility fixture extracted size differs from registry")
        archive.extractall(output, members=members, filter="data")
    fixture_ids = []
    for manifest_path in sorted(output.rglob("manifest.json")):
        manifest = validate_fixture(manifest_path)
        fixture_ids.append(str(manifest["fixture_id"]))
    if sorted(fixture_ids) != sorted(bundle["fixture_ids"]):
        raise ValueError("compatibility fixture ids differ from registry")
    return {
        "schema_version": "1.0.0",
        "bundle_id": bundle["id"],
        "trading_mode": bundle["trading_mode"],
        "source_sha256": bundle["source_sha256"],
        "freqtrade_image_digest": bundle["freqtrade_image_digest"],
        "asset_sha256": bundle["asset_sha256"],
        "extracted_bytes": extracted_bytes,
        "fixture_ids": sorted(fixture_ids),
        "output_directory": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    select = subcommands.add_parser("select")
    _add_identity_arguments(select)
    select.add_argument("--registry", type=Path, required=True)
    materialize = subcommands.add_parser("materialize")
    _add_identity_arguments(materialize)
    materialize.add_argument("--registry", type=Path, required=True)
    materialize.add_argument("--asset", type=Path, required=True)
    materialize.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    registry = read_json(args.registry)
    if not isinstance(registry, dict):
        raise ValueError("compatibility fixture registry must be an object")
    bundle = select_bundle(
        registry,
        trading_mode=args.trading_mode,
        source_sha256=args.source_sha256,
        freqtrade_digest=args.freqtrade_digest,
    )
    if args.command == "select":
        print(
            json.dumps(
                {"found": bundle is not None, "bundle": bundle},
                sort_keys=True,
            )
        )
        return 0
    if bundle is None:
        raise ValueError("no exact compatibility fixture bundle is registered")
    report = materialize_bundle(
        bundle,
        asset_path=args.asset,
        output_directory=args.output_dir,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--trading-mode", choices=sorted(_MODES), required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--freqtrade-digest", required=True)


def _validate_registry(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    if set(registry) != {"schema_version", "bundles"}:
        raise ValueError("compatibility fixture registry fields differ")
    if registry.get("schema_version") != "1.0.0":
        raise ValueError("compatibility fixture registry version differs")
    raw = registry.get("bundles")
    if not isinstance(raw, list):
        raise ValueError("compatibility fixture registry bundles must be an array")
    bundles: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str]] = set()
    for value in raw:
        if not isinstance(value, dict):
            raise ValueError("compatibility fixture bundle must be an object")
        _validate_bundle(value)
        identity = (
            str(value["trading_mode"]),
            str(value["source_sha256"]),
            str(value["freqtrade_image_digest"]),
        )
        if identity in identities:
            raise ValueError("compatibility fixture bundle identity is duplicated")
        identities.add(identity)
        bundles.append(dict(value))
    return bundles


def _validate_bundle(bundle: Mapping[str, Any]) -> None:
    expected = {
        "id",
        "trading_mode",
        "upstream_commit",
        "source_sha256",
        "freqtrade_image_digest",
        "release_tag",
        "asset_name",
        "asset_bytes",
        "asset_sha256",
        "extracted_bytes",
        "fixture_ids",
    }
    fixture_ids = bundle.get("fixture_ids")
    if (
        set(bundle) != expected
        or _SAFE_TOKEN.fullmatch(str(bundle.get("id"))) is None
        or bundle.get("trading_mode") not in _MODES
        or _SHA.fullmatch(str(bundle.get("upstream_commit"))) is None
        or _DIGEST.fullmatch(str(bundle.get("source_sha256"))) is None
        or _IMAGE_DIGEST.fullmatch(str(bundle.get("freqtrade_image_digest"))) is None
        or _SAFE_TOKEN.fullmatch(str(bundle.get("release_tag"))) is None
        or _SAFE_TOKEN.fullmatch(str(bundle.get("asset_name"))) is None
        or not _positive_int(bundle.get("asset_bytes"))
        or not _positive_int(bundle.get("extracted_bytes"))
        or int(bundle["extracted_bytes"]) > _MAX_BUNDLE_BYTES
        or _DIGEST.fullmatch(str(bundle.get("asset_sha256"))) is None
        or not isinstance(fixture_ids, list)
        or len(fixture_ids) < 2
        or not all(
            isinstance(fixture_id, str)
            and _SAFE_TOKEN.fullmatch(fixture_id) is not None
            for fixture_id in fixture_ids
        )
        or len(set(fixture_ids)) != len(fixture_ids)
    ):
        raise ValueError("compatibility fixture bundle is invalid")


def _validate_archive_members(members: Sequence[tarfile.TarInfo]) -> int:
    total = 0
    seen: set[PurePosixPath] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path in seen
            or not (member.isfile() or member.isdir())
        ):
            raise ValueError("compatibility fixture archive contains an unsafe member")
        seen.add(path)
        if member.isfile():
            if member.size < 0:
                raise ValueError("compatibility fixture archive size is invalid")
            total += member.size
            if total > _MAX_BUNDLE_BYTES:
                raise ValueError("compatibility fixture archive exceeds size limit")
    return total


def _positive_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
