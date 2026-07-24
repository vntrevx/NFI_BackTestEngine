from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import BenchmarkError
from nfi_backtest_engine.run_registry import RunRegistry


def test_registry_upserts_and_loads_run_report(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    report = {
        "run_id": "abc",
        "status": "complete",
        "created_at": "2026-07-18T00:00:00Z",
        "inputs": {
            "strategy": {
                "class_name": "Simple",
                "file_sha256": "strategy",
            },
            "config": {"run_effective_sha256": "config"},
        },
        "vectors": {"pair_count": 2},
        "result": {"trade_count": 3},
    }
    write_json(output / "run.json", report)

    with RunRegistry(tmp_path / "runs.sqlite") as registry:
        registry.record(report, output)
        registry.record({**report, "status": "prepared"}, output)
        listed = registry.list()
        shown = registry.show("abc")

    assert len(listed) == 1
    assert listed[0]["status"] == "prepared"
    assert shown["report"]["run_id"] == "abc"


def test_registry_resolves_only_unambiguous_run_id_prefixes(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    base_report = {
        "status": "complete",
        "created_at": "2026-07-18T00:00:00Z",
        "inputs": {
            "strategy": {
                "class_name": "Simple",
                "file_sha256": "strategy",
            },
            "config": {"run_effective_sha256": "config"},
        },
        "vectors": {"pair_count": 2},
        "result": {"trade_count": 3},
    }
    first = {**base_report, "run_id": "abcdef1234567890"}
    second = {**base_report, "run_id": "abcd999999999999"}
    write_json(output / "run.json", first)

    with RunRegistry(tmp_path / "runs.sqlite") as registry:
        registry.record(first, output)
        registry.record(second, output)
        shown = registry.show("abcdef123456")

        assert shown["run_id"] == first["run_id"]
        with pytest.raises(BenchmarkError, match="prefix is ambiguous"):
            registry.show("abcd")
