from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from nfi_backtest_engine import doctor
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.strategy_catalog import discover_strategy_catalog
from nfi_backtest_engine.user_flow import RunProgress

ROOT = Path(__file__).parents[1]
SCHEMAS = ROOT / "python" / "nfi_backtest_engine" / "schemas"


def _validate(name: str, document: object) -> None:
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=None).validate(document)


def test_product_ux_documents_match_their_published_schemas(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "NostalgiaForInfinityX7.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class NostalgiaForInfinityX7(IStrategy):\n"
        "    timeframe = '5m'\n",
        encoding="utf-8",
    )
    catalog = discover_strategy_catalog(tmp_path)
    _validate("strategy-catalog-v1.schema.json", catalog)

    progress_path = tmp_path / "progress.json"
    with RunProgress(interactive=False, state_path=progress_path) as progress:
        progress.update(10, "Checking inputs")
    _validate("run-progress-v1.schema.json", read_json(progress_path))

    monkeypatch.setattr(
        doctor,
        "inspect_hardware",
        lambda _workspace: {
            "memory": {"available_bytes": 4 * 1024**3},
        },
    )
    monkeypatch.setattr(doctor, "_docker_checks", lambda: ([], None))
    report = doctor.run_doctor()
    _validate("doctor-report-v2.schema.json", report)
