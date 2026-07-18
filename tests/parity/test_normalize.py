from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.errors import NormalizationError
from nfi_backtest_engine.normalize import normalize_file, normalize_freqtrade_result

ROOT = Path(__file__).parents[2]
CONTRACT_FIXTURES = ROOT / "benchmarks" / "fixtures" / "contract"


@pytest.mark.parametrize("fixture_name", ["stops-only", "normal-routing"])
def test_contract_result_normalizes_to_exact_expected_surface(fixture_name: str) -> None:
    fixture = CONTRACT_FIXTURES / fixture_name
    raw = read_json(fixture / "freqtrade-result.json", decimals=True)
    expected = read_json(fixture / "trade-surface.json")

    actual = normalize_freqtrade_result(raw)

    assert actual == expected


def test_multiple_strategies_require_an_explicit_selection() -> None:
    fixture = CONTRACT_FIXTURES / "stops-only"
    raw = read_json(fixture / "freqtrade-result.json", decimals=True)
    raw["strategy"]["Second"] = deepcopy(raw["strategy"]["ContractStopsOnly"])

    with pytest.raises(NormalizationError, match="select one strategy explicitly"):
        normalize_freqtrade_result(raw)


def test_timestamp_and_date_must_agree() -> None:
    fixture = CONTRACT_FIXTURES / "stops-only"
    raw = read_json(fixture / "freqtrade-result.json", decimals=True)
    raw["strategy"]["ContractStopsOnly"]["trades"][0]["open_timestamp"] += 1

    with pytest.raises(NormalizationError, match="timestamp/date disagree"):
        normalize_freqtrade_result(raw)


def test_official_zip_export_is_read_without_extracting(tmp_path: Path) -> None:
    fixture = CONTRACT_FIXTURES / "stops-only"
    archive_path = tmp_path / "backtest-result.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(fixture / "freqtrade-result.json", arcname="backtest-result-2022.json")
        archive.writestr("backtest-result-2022.meta.json", '{"metadata": true}')
    output = tmp_path / "surface.json"

    normalize_file(archive_path, output, strategy="ContractStopsOnly")

    assert read_json(output) == read_json(fixture / "trade-surface.json")
