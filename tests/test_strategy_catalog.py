from __future__ import annotations

from pathlib import Path

from nfi_backtest_engine import cli
from nfi_backtest_engine.strategy_catalog import discover_strategy_catalog


def _write_strategy(path: Path, class_name: str, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from freqtrade.strategy import IStrategy\n"
        f"class {class_name}(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    stoploss = -0.1\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        f"        {body or 'return dataframe'}\n",
        encoding="utf-8",
    )


def test_catalog_hides_legacy_and_incompatible_sources_by_default(
    tmp_path: Path,
    capsys,
) -> None:
    _write_strategy(
        tmp_path / "NostalgiaForInfinityX7.py",
        "NostalgiaForInfinityX7",
    )
    legacy = tmp_path / "legacy" / "NostalgiaForInfinityNext.py"
    _write_strategy(legacy, "NostalgiaForInfinityNext")
    linked = tmp_path / "user_data" / "strategies" / legacy.name
    linked.parent.mkdir(parents=True)
    linked.symlink_to(legacy)
    _write_strategy(
        tmp_path / "user_data" / "strategies" / "NostalgiaForInfinityNextGen.py",
        "NostalgiaForInfinityNextGen",
        "dataframe['future'] = dataframe['close'].shift(-1); return dataframe",
    )

    catalog = discover_strategy_catalog(tmp_path)

    assert [item["status"] for item in catalog["candidates"]] == [
        "supported",
        "unsupported",
        "unsupported",
    ]
    assert catalog["summary"] == {
        "total": 3,
        "supported": 1,
        "unsupported": 2,
        "invalid": 0,
    }

    assert cli.main(["strategy", "list", "--workspace", str(tmp_path)]) == 0
    default_output = capsys.readouterr().out
    assert "NostalgiaForInfinityX7" in default_output
    assert "NostalgiaForInfinityNext" not in default_output

    assert cli.main(
        ["strategy", "list", "--workspace", str(tmp_path), "--show-unsupported"]
    ) == 0
    advanced_output = capsys.readouterr().out
    assert "NostalgiaForInfinityNext" in advanced_output
    assert "LEGACY_SOURCE" in advanced_output
    assert "LOOKAHEAD_NEGATIVE_SHIFT" not in advanced_output
    legacy_candidates = [item for item in catalog["candidates"] if item["legacy"]]
    assert [item["generation"] for item in legacy_candidates] == ["V8", "V9"]
    assert all(item["fallback_status"] == "unavailable" for item in legacy_candidates)


def test_strategy_list_json_has_a_versioned_machine_contract(tmp_path: Path, capsys) -> None:
    _write_strategy(tmp_path / "NostalgiaForInfinityX7.py", "NostalgiaForInfinityX7")

    assert cli.main(
        ["strategy", "list", "--workspace", str(tmp_path), "--json"]
    ) == 0

    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "1.0.0"
    assert payload["candidates"][0]["status"] == "supported"
