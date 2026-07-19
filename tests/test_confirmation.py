from __future__ import annotations

import shutil
from pathlib import Path

from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.confirmation import confirm_research_run
from nfi_backtest_engine.fixture import sha256_file

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "benchmarks" / "fixtures" / "captured" / "stops-only-spot-2025-01-01_04"


def test_complete_run_confirms_against_official_freqtrade_export(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    surface = run / "trade-surface.json"
    shutil.copyfile(FIXTURE / "artifacts" / "trade-surface.json", surface)
    write_json(
        run / "run.json",
        {
            "run_id": "fixture",
            "status": "complete",
            "result": {
                "trade_surface": {
                    "path": str(surface),
                    "sha256": sha256_file(surface),
                }
            },
        },
    )

    report = confirm_research_run(
        run,
        FIXTURE / "artifacts" / "freqtrade-result.zip",
        tmp_path / "confirmation",
        strategy="ContractStopsOnly",
    )

    assert report["equal"]
    assert report["difference"] is None
