from __future__ import annotations

from pathlib import Path

import pytest
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


def test_fixture_rejects_a_path_escape(tmp_path: Path) -> None:
    source = CONTRACT_FIXTURES / "stops-only" / "manifest.json"
    manifest = read_json(source)
    manifest["artifacts"]["freqtrade_result"]["path"] = "../outside.json"
    manifest_path = tmp_path / "manifest.json"
    write_json(manifest_path, manifest)

    with pytest.raises(SpecValidationError, match="escapes its directory"):
        validate_fixture(manifest_path, verify_hashes=False)


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
