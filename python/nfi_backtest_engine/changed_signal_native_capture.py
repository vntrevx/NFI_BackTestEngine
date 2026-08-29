"""Native current-predicate capture independent of Oracle outputs."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final, Literal

from . import _rust
from .signal_program import compile_signal_program
from .tag_program import compile_tag_program

_CONTRACT: Final = Path(__file__).resolve().parents[2] / (
    "benchmarks/reference/strategies/CurrentChangedPredicateContract.py"
)
_COLUMNS: Final[dict[str, list[float | None]]] = {
    "RSI_3_15m": [15.0, 15.000000000000002, 15.0, 15.0, 15.0],
    "RSI_3_1h": [20.0, 20.0, 20.000000000000004, 20.0, 20.0],
    "RSI_3_4h": [25.0, 25.0, 25.0, 25.000000000000004, 25.0],
    "AROONU_14_1h": [0.0, 0.0, 0.0, 0.0, 5e-324],
}


def capture_native_changed_signal(mode: Literal["spot", "futures"]) -> dict[str, Any]:
    """Compile and execute Native from sealed inputs without reading Oracle output."""
    config = {"trading_mode": mode}
    signal = compile_signal_program(
        _CONTRACT,
        class_name="CurrentChangedPredicateContract",
        trading_mode=mode,
        config=config,
    )
    tag = compile_tag_program(
        _CONTRACT,
        class_name="CurrentChangedPredicateContract",
        trading_mode=mode,
        config=config,
    )
    output = _rust.execute_numeric_mutation_program(
        json.dumps(tag, separators=(",", ":")),
        _COLUMNS,
        {},
        ["enter_long", "enter_short", "enter_tag", "exit_long", "exit_short", "exit_tag"],
    )
    return {
        "producer": "nfi-vector-core",
        "trading_mode": mode,
        "signal_program_fingerprint": signal["fingerprint"],
        "tag_program_fingerprint": tag["fingerprint"],
        "input": _COLUMNS,
        "output": output,
    }


def main() -> int:
    """Print one canonical Native capture for the requested trading mode."""
    if len(sys.argv) != 2 or sys.argv[1] not in {"spot", "futures"}:
        raise SystemExit("usage: python -m nfi_backtest_engine.changed_signal_native_capture MODE")
    mode: Literal["spot", "futures"] = "spot" if sys.argv[1] == "spot" else "futures"
    print(json.dumps(capture_native_changed_signal(mode), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
