from __future__ import annotations

import hashlib
import importlib.util
import tarfile
from io import BytesIO
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json

ROOT = Path(__file__).parents[1]
REGISTRY = ROOT / "planning" / "compatibility-fixtures.json"
SPEC = importlib.util.spec_from_file_location(
    "compatibility_fixture_registry",
    ROOT / "scripts" / "compatibility_fixture_registry.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_registry_selects_only_exact_mode_source_and_oracle() -> None:
    registry = read_json(REGISTRY)
    selected = MODULE.select_bundle(
        registry,
        trading_mode="futures",
        source_sha256="17bb8002110f5ea0edae051348dd2f5125d943bff578f4c942368e9bff3528cc",
        freqtrade_digest=(
            "sha256:d47d7053dc07eca2ace20385575143090"
            "ba88621007e5e8b76052dca6038799a"
        ),
    )
    changed_oracle = MODULE.select_bundle(
        registry,
        trading_mode="futures",
        source_sha256="17bb8002110f5ea0edae051348dd2f5125d943bff578f4c942368e9bff3528cc",
        freqtrade_digest="sha256:" + "a" * 64,
    )

    assert selected is not None
    assert selected["asset_sha256"] == (
        "2729d48eabe339cf1db5abd3813c6fe507be61a356319b8c0e3362a2c3711990"
    )
    assert changed_oracle is None


def test_registry_rejects_duplicate_identity() -> None:
    registry = read_json(REGISTRY)
    registry["bundles"].append(dict(registry["bundles"][0]))

    with pytest.raises(ValueError, match="duplicated"):
        MODULE.select_bundle(
            registry,
            trading_mode="spot",
            source_sha256=registry["bundles"][0]["source_sha256"],
            freqtrade_digest=registry["bundles"][0]["freqtrade_image_digest"],
        )


def test_archive_validation_rejects_traversal_and_links() -> None:
    traversal = tarfile.TarInfo("../outside")
    traversal.size = 1
    link = tarfile.TarInfo("fixture/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../outside"

    with pytest.raises(ValueError, match="unsafe"):
        MODULE._validate_archive_members([traversal])
    with pytest.raises(ValueError, match="unsafe"):
        MODULE._validate_archive_members([link])


def test_archive_validation_rejects_portable_device_member() -> None:
    reserved = tarfile.TarInfo("fixture/NUL.txt")
    reserved.size = 1

    with pytest.raises(ValueError, match="unsafe|portable"):
        MODULE._validate_archive_members([reserved])


def test_complete_valid_bundle_publishes_exact_fixture(tmp_path: Path) -> None:
    fixture = ROOT / "benchmarks" / "fixtures" / "contract" / "stops-only"
    asset = tmp_path / "bundle.tar.gz"
    with tarfile.open(asset, "w:gz") as archive:
        archive.add(fixture, arcname="stops-only")
    expected = {
        path.relative_to(fixture).as_posix(): path.read_bytes()
        for path in fixture.rglob("*")
        if path.is_file()
    }
    bundle = {
        "id": "contract-stops-only",
        "trading_mode": "spot",
        "source_sha256": "1" * 64,
        "freqtrade_image_digest": "sha256:" + "2" * 64,
        "asset_bytes": asset.stat().st_size,
        "asset_sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
        "extracted_bytes": sum(len(payload) for payload in expected.values()),
        "fixture_ids": ["contract-stops-only-v1"],
    }
    output = tmp_path / "public"

    report = MODULE.materialize_bundle(
        bundle,
        asset_path=asset,
        output_directory=output,
    )

    assert report["fixture_ids"] == ["contract-stops-only-v1"]
    assert report["extracted_bytes"] == bundle["extracted_bytes"]
    assert {
        path.relative_to(output / "stops-only").as_posix(): path.read_bytes()
        for path in (output / "stops-only").rglob("*")
        if path.is_file()
    } == expected
    assert not list(tmp_path.glob(".public.stage-*"))


def test_hostile_archive_failure_leaves_no_public_destination(tmp_path: Path) -> None:
    asset = tmp_path / "bundle.tar.gz"
    with tarfile.open(asset, "w:gz") as archive:
        member = tarfile.TarInfo("NUL.txt")
        payload = b"hostile"
        member.size = len(payload)
        archive.addfile(member, BytesIO(payload))
    bundle = {
        "asset_bytes": asset.stat().st_size,
        "asset_sha256": MODULE._sha256_file(asset),
    }
    output = tmp_path / "public"

    with pytest.raises(ValueError, match="unsafe|portable"):
        MODULE.materialize_bundle(bundle, asset_path=asset, output_directory=output)

    assert not output.exists()
    assert not list(tmp_path.glob(".public.stage-*"))


def test_archive_validation_counts_only_regular_file_bytes() -> None:
    directory = tarfile.TarInfo("fixture")
    directory.type = tarfile.DIRTYPE
    payload = tarfile.TarInfo("fixture/data.bin")
    payload.size = len(BytesIO(b"exact").getvalue())

    assert MODULE._validate_archive_members([directory, payload]) == 5
