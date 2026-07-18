from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import fixture_input_sha256, validate_fixture
from nfi_backtest_engine.specs import validate_fixture_manifest

ROOT = Path(__file__).parents[1]
V1 = ROOT / "benchmarks" / "fixtures" / "contract" / "stops-only" / "manifest.json"
CAPTURED = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "stops-only-spot-2025-01-01_04"
    / "manifest.json"
)


def _v2_manifest() -> dict:
    manifest = deepcopy(read_json(V1))
    manifest["schema_version"] = "2.0.0"
    manifest["freqtrade"].update(
        {
            "image": "freqtradeorg/freqtrade",
            "image_index_digest": f"sha256:{'1' * 64}",
            "image_platform_digest": f"sha256:{'2' * 64}",
            "platform": "linux/amd64",
            "tracer_version": "1.0.0",
        }
    )
    manifest["inputs"] = [
        {"role": "strategy", "path": "strategy.py", "sha256": "1" * 64, "bytes": 1},
        {"role": "config", "path": "config.json", "sha256": "2" * 64, "bytes": 2},
        {"role": "candles", "path": "candles.feather", "sha256": "3" * 64, "bytes": 3},
    ]
    manifest["artifacts"]["state_trace"] = {
        "path": "state.nfitrace",
        "sha256": "4" * 64,
        "bytes": 4,
    }
    return manifest


def test_fixture_v2_manifest_accepts_pinned_reference_and_trace() -> None:
    validate_fixture_manifest(_v2_manifest())


def test_fixture_v2_rejects_unpinned_platform_digest() -> None:
    manifest = _v2_manifest()
    manifest["freqtrade"]["image_platform_digest"] = "stable"

    with pytest.raises(SpecValidationError, match="does not match"):
        validate_fixture_manifest(manifest)


def test_fixture_input_hash_is_order_independent() -> None:
    inputs = _v2_manifest()["inputs"]

    assert fixture_input_sha256(inputs) == fixture_input_sha256(list(reversed(inputs)))


def test_real_captured_v2_fixture_is_fully_sealed() -> None:
    manifest = validate_fixture(CAPTURED)

    assert manifest["evidence_status"] == "captured"
    assert manifest["freqtrade"]["version"] == "2026.5.1"
