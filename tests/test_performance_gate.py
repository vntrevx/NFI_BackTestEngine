from __future__ import annotations

from pathlib import Path

from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.performance_gate import _representative_scope


def _manifest(tmp_path: Path, timerange: str, pair_count: int) -> tuple[Path, dict]:
    manifest_path = tmp_path / "manifest.json"
    config_path = tmp_path / "config.json"
    write_json(
        config_path,
        {
            "exchange": {
                "pair_whitelist": [f"PAIR-{index}/USDT" for index in range(pair_count)]
            }
        },
    )
    return manifest_path, {
        "inputs": [{"role": "config", "path": config_path.name}],
        "freqtrade": {"timerange": timerange},
    }


def test_performance_claim_requires_at_least_four_years(tmp_path: Path) -> None:
    manifest_path, manifest = _manifest(tmp_path, "20210101-20250101", 80)

    scope = _representative_scope(manifest_path, manifest)

    assert scope["eligible"] is True
    assert scope["required_days"] == 1460
    assert scope["actual_days"] == 1461


def test_three_year_fixture_remains_diagnostic_only(tmp_path: Path) -> None:
    manifest_path, manifest = _manifest(tmp_path, "20220101-20250101", 80)

    scope = _representative_scope(manifest_path, manifest)

    assert scope["eligible"] is False
    assert scope["label"] == "fixture-diagnostic-only"
