from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from nfi_backtest_engine import branch_coverage, fixture
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import validate_fixture
from nfi_backtest_engine.specs import validate_fixture_manifest

ROOT = Path(__file__).parents[1]
CONTRACT_FIXTURES = ROOT / "benchmarks" / "fixtures" / "contract"


@pytest.mark.parametrize("fixture_name", ["stops-only", "normal-routing"])
def test_contract_fixture_is_fully_sealed(fixture_name: str) -> None:
    manifest = validate_fixture(CONTRACT_FIXTURES / fixture_name / "manifest.json")
    assert manifest["evidence_status"] == "contract-only"


def test_v3_fixture_semantics_use_only_retained_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = (
        ROOT
        / "benchmarks/fixtures/release-candidate/x7-tag121-spot-v17.4.473-2023-01-01_02"
        / "manifest.json"
    )

    def pathname_reopen(*_args, **_kwargs):
        raise AssertionError("v3 semantic validation reopened a fixture pathname")

    monkeypatch.setattr(branch_coverage, "read_json", pathname_reopen)
    monkeypatch.setattr(branch_coverage, "trace_summary", pathname_reopen)
    monkeypatch.setattr(branch_coverage, "configured_protection_methods", pathname_reopen)

    assert validate_fixture(manifest)["schema_version"] == "3.0.0"


def test_fixture_rejects_a_path_escape(tmp_path: Path) -> None:
    source = CONTRACT_FIXTURES / "stops-only" / "manifest.json"
    manifest = read_json(source)
    outside_name = f"outside-{tmp_path.name}.json"
    manifest["artifacts"]["freqtrade_result"]["path"] = f"../{outside_name}"
    manifest_path = tmp_path / "manifest.json"
    write_json(manifest_path, manifest)

    with pytest.raises(SpecValidationError):
        validate_fixture(manifest_path, verify_hashes=False)
    assert not (tmp_path.parent / outside_name).exists()


def test_fixture_rejects_reserved_initial_manifest_name(tmp_path: Path) -> None:
    source_root = CONTRACT_FIXTURES / "stops-only"
    root = tmp_path / "fixture"
    shutil.copytree(source_root, root)
    (root / "manifest.json").rename(root / "NUL.json")

    with pytest.raises(SpecValidationError, match="portable|manifest path"):
        validate_fixture(root / "NUL.json")


def test_fixture_final_file_swap_never_reads_outside(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_root = CONTRACT_FIXTURES / "stops-only"
    root = tmp_path / "fixture"
    shutil.copytree(source_root, root)
    manifest = read_json(root / "manifest.json")
    relative = next(iter(manifest["artifacts"].values()))["path"]
    target = root / relative
    outside = tmp_path / "outside.bin"
    outside.write_bytes(target.read_bytes())
    swapped = False

    def swap(checkpoint: str, name: str) -> None:
        nonlocal swapped
        if checkpoint == "after-open" and name == relative and not swapped:
            target.rename(target.with_suffix(".trusted"))
            target.symlink_to(outside)
            swapped = True

    monkeypatch.setattr(fixture, "_fixture_file_checkpoint", swap)
    with pytest.raises(SpecValidationError, match="changed|symlink|reparse|identity"):
        validate_fixture(root / "manifest.json")

    assert swapped
    assert outside.read_bytes() == target.with_suffix(".trusted").read_bytes()


@pytest.mark.parametrize("mutation", ["in-place", "hardlink", "parent-swap"])
def test_fixture_mutation_checkpoints_reject_without_outside_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / "fixture"
    shutil.copytree(CONTRACT_FIXTURES / "stops-only", root)
    manifest = read_json(root / "manifest.json")
    relative = next(iter(manifest["artifacts"].values()))["path"]
    target = root / relative
    original = target.read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / target.name
    outside_target.write_bytes(original)
    triggered = False

    def mutate(checkpoint: str, name: str) -> None:
        nonlocal triggered
        if checkpoint != "after-read" or name != relative or triggered:
            return
        if mutation == "in-place":
            with target.open("r+b") as handle:
                handle.seek(0)
                handle.write(b"X")
        elif mutation == "hardlink":
            alias = tmp_path / "alias"
            os.link(target, alias)
            with alias.open("r+b") as handle:
                handle.seek(0)
                handle.write(b"X")
        else:
            parent = target.parent
            parent.rename(parent.with_name(parent.name + "-trusted"))
            parent.symlink_to(outside, target_is_directory=True)
        triggered = True

    monkeypatch.setattr(fixture, "_fixture_file_checkpoint", mutate)
    with pytest.raises(SpecValidationError, match="changed|identity|symlink|reparse|containment"):
        validate_fixture(root / "manifest.json")

    assert triggered
    assert outside_target.read_bytes() == original


def test_fixture_initial_parent_symlink_is_rejected_without_following(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    shutil.copytree(CONTRACT_FIXTURES / "stops-only", outside)
    holder = tmp_path / "holder"
    holder.mkdir()
    (holder / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SpecValidationError, match="symlink|containment|root"):
        validate_fixture(holder / "linked" / "manifest.json")


def test_captured_manifest_accepts_required_typed_inputs() -> None:
    manifest = read_json(CONTRACT_FIXTURES / "stops-only" / "manifest.json")
    manifest["evidence_status"] = "captured"
    zero_hash = "0" * 64
    manifest["inputs"] = [
        {"role": "strategy", "path": "strategy.py", "sha256": zero_hash, "bytes": 1},
        {"role": "config", "path": "config.json", "sha256": zero_hash, "bytes": 1},
        {"role": "candles", "path": "candles.feather", "sha256": zero_hash, "bytes": 1},
        {
            "role": "funding_candles",
            "path": "funding.feather",
            "sha256": zero_hash,
            "bytes": 1,
        },
        {
            "role": "mark_candles",
            "path": "mark.feather",
            "sha256": zero_hash,
            "bytes": 1,
        },
    ]

    validate_fixture_manifest(manifest)
