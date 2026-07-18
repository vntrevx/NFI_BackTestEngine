from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

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
            "time": datetime(2025, 1, 1, tzinfo=timezone.utc),
        }
    )

    assert value == {
        "wallet": {"currency": "USDT", "free": "1000.25", "used": "2.5"},
        "time": 1_735_689_600_000,
    }
