from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
TRACER = ROOT / "benchmarks" / "reference" / "tracer" / "nfi_reference_trace.py"


def _load_tracer():
    spec = importlib.util.spec_from_file_location("nfi_reference_trace_test", TRACER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reference_state_canonicalizer_rejects_no_supported_runtime_values() -> None:
    tracer = _load_tracer()

    @dataclass
    class Wallet:
        currency: str
        free: float
        used: Decimal

    value = tracer._canonicalize(
        {
            "wallet": Wallet("USDT", 1000.25, Decimal("2.500")),
            "time": datetime(2025, 1, 1, tzinfo=UTC),
        }
    )

    assert value == {
        "wallet": {"currency": "USDT", "free": "1000.25", "used": "2.5"},
        "time": 1_735_689_600_000,
    }


def test_callback_audit_aggregates_outcomes_without_per_call_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tracer = _load_tracer()

    class Backtesting:
        _nfi_callback_audit = {"schema_version": "1.0.0", "callbacks": {}}

    class Trade:
        id = 1
        pair = "APE/USDT"
        enter_tag = "141"
        is_short = False
        amount = 2.0
        stake_amount = 10.0
        nr_of_successful_entries = 1
        nr_of_successful_exits = 0
        orders = []
        custom_data = {"system_version": "system_v3_2"}

    backtesting = Backtesting()
    trade = Trade()
    arguments = {
        "trade": trade,
        "current_time": datetime(2025, 1, 1, tzinfo=UTC),
        "current_rate": 5.0,
        "current_profit": -0.1,
    }
    state = tracer._audit_trade_state(trade)
    for _ in range(2):
        tracer._record_callback_audit(
            backtesting,
            "adjust_trade_position",
            arguments,
            state,
            state,
            (5.0, "grind_1_entry"),
            None,
        )

    callback = backtesting._nfi_callback_audit["callbacks"]["adjust_trade_position"]
    assert callback["calls"] == 2
    bucket = next(iter(callback["outcomes"].values()))
    assert bucket["count"] == 2
    assert bucket["signature"]["result"] == {
        "kind": "tuple",
        "length": 2,
        "tag": "grind_1_entry",
    }
    assert len(bucket["samples"]) == 2

    destination = tmp_path / "callback-audit.json"
    monkeypatch.setenv("NFI_CALLBACK_AUDIT_PATH", str(destination))
    tracer._flush_callback_audit(backtesting)

    assert destination.is_file()


def test_callback_audit_preserves_requested_timestamp_after_sample_cap(monkeypatch) -> None:
    tracer = _load_tracer()

    class Backtesting:
        _nfi_callback_audit = {"schema_version": "1.0.0", "callbacks": {}}

    monkeypatch.setenv("NFI_CALLBACK_AUDIT_TIMESTAMPS_MS", "1735690500000")
    start = datetime(2025, 1, 1, tzinfo=UTC)
    for offset in range(4):
        arguments = {
            "current_time": start.replace(minute=offset * 5),
            "current_rate": 1.0,
            "current_profit": 0.0,
        }
        tracer._record_callback_audit(
            Backtesting,
            "adjust_trade_position",
            arguments,
            None,
            None,
            None,
            None,
        )

    callback = Backtesting._nfi_callback_audit["callbacks"]["adjust_trade_position"]
    bucket = next(iter(callback["outcomes"].values()))
    assert [sample["timestamp"] for sample in bucket["samples"]] == [
        1_735_689_600_000,
        1_735_689_900_000,
        1_735_690_200_000,
        1_735_690_500_000,
    ]


def test_callback_audit_environment_activates_sitecustomize(tmp_path: Path) -> None:
    marker = tmp_path / "installed.txt"
    (tmp_path / "nfi_reference_trace.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def install_reference_tracer():\n"
        "    Path(os.environ['NFI_TEST_INSTALL_MARKER']).write_text('installed')\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(TRACER.parent)))
    environment["NFI_CALLBACK_AUDIT_PATH"] = str(tmp_path / "audit.json")
    environment["NFI_TEST_INSTALL_MARKER"] = str(marker)

    result = subprocess.run(
        [sys.executable, "-c", "pass"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "installed"


def test_signal_audit_records_only_shifted_rows_consumed_by_hot_loop() -> None:
    tracer = _load_tracer()

    class Strategy:
        pass

    class Backtesting:
        strategy = Strategy()
        config = {"trading_mode": "futures", "timeframe": "5m"}

    headers = (
        "date",
        "open",
        "high",
        "low",
        "close",
        "enter_long",
        "exit_long",
        "enter_short",
        "exit_short",
        "enter_tag",
        "exit_tag",
    )
    rows = (
        (
            datetime(2025, 1, 1, tzinfo=UTC),
            1.0,
            1.0,
            1.0,
            1.0,
            0,
            0,
            0,
            0,
            None,
            None,
        ),
        (
            datetime(2025, 1, 1, 0, 5, tzinfo=UTC),
            1.0,
            1.0,
            1.0,
            1.0,
            0,
            0,
            1,
            0,
            "562 ",
            None,
        ),
    )

    audit = tracer._build_signal_audit(
        Backtesting(),
        {"APE/USDT:USDT": rows},
        headers,
    )

    pair = audit["pairs"]["APE/USDT:USDT"]
    assert pair["rows"] == 2
    assert pair["signal_rows"] == 1
    assert pair["signals"] == [
        {
            "timestamp_ms": 1_735_689_900_000,
            "enter_long": False,
            "exit_long": False,
            "enter_short": True,
            "exit_short": False,
            "enter_tag": "562 ",
            "exit_tag": None,
        }
    ]


def test_signal_feature_audit_samples_requested_and_source_signal_rows(
    monkeypatch,
) -> None:
    tracer = _load_tracer()
    start = datetime(2025, 1, 1, tzinfo=UTC)
    frame = pd.DataFrame(
        {
            "date": [start, start.replace(minute=5), start.replace(minute=10)],
            "close": [1.0, 2.0, 3.0],
            "enter_short": [0, 1, 0],
            "enter_tag": ["", "562 ", ""],
        }
    )
    audit = {"pairs": {"APE/USDT:USDT": {}}}
    monkeypatch.setenv("NFI_SIGNAL_AUDIT_FEATURES", "close")
    monkeypatch.setenv("NFI_SIGNAL_AUDIT_TIMESTAMPS_MS", "1735689600000")

    tracer._add_signal_feature_samples(
        audit,
        {"APE/USDT:USDT": frame},
    )

    pair = audit["pairs"]["APE/USDT:USDT"]
    assert pair["feature_columns"] == ["close"]
    assert [sample["timestamp_ms"] for sample in pair["feature_samples"]] == [
        1_735_689_600_000,
        1_735_689_900_000,
    ]
    assert pair["feature_samples"][1]["values"] == {
        "enter_short": 1,
        "enter_tag": "562 ",
        "close": "2",
    }
