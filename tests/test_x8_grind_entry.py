from __future__ import annotations

import ast
from pathlib import Path

from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.trade_ir import build_trade_dependency_ir

_SOURCE = Path(
    "benchmarks/fixtures/captured/x8-grind-entry-futures-r1/inputs/strategy.py"
)
_METHODS = (
    "long_grind_entry_v3", "short_grind_entry_v3",
    "long_grind_entry_v4", "short_grind_entry_v4",
)


def test_captured_grind_predicates_compile_with_all_diagnostic_writers(tmp_path: Path) -> None:
    # Keep the captured donor methods unchanged, including writers outside the
    # selected v3 closure. No dependency on the user's untracked X8 file.
    text = _SOURCE.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    strategy = next(
        node for node in ast.parse(text).body
        if isinstance(node, ast.ClassDef) and node.name == "GrindEntryProbe"
    )
    methods = {
        node.name: node for node in strategy.body
        if isinstance(node, ast.FunctionDef) and node.name in _METHODS
    }
    source = tmp_path / "GrindDecisions.py"
    source.write_text(
        "from __future__ import annotations\n"
        "from freqtrade.strategy import IStrategy\n"
        "class GrindDecisions(IStrategy):\n"
        "  timeframe = '5m'\n"
        + "\n".join(
            "".join(lines[methods[name].lineno - 1:methods[name].end_lineno])
            for name in _METHODS
        ),
        encoding="utf-8",
    )
    analysis = analyze_strategy(source)
    report = build_trade_dependency_ir(analysis, roots=_METHODS)

    assert report["stateful_methods"] == {}
    assert set(report["compiled_scalar_methods"]) == set(_METHODS)
    for name, record in report["compiled_scalar_methods"].items():
        assert record["elided_observability_writes"] == ["_grind_entry_tag"]
        fields = record["input_contract"]["indexed_fields"]["last_candle"]
        side = name.split("_", 1)[0]
        assert f"protections_{side}_global" in fields
        assert f"enter_{side}" in fields
        # Late recovery predicates must survive alongside the early g0 gate.
        expressions = record["program"]["expressions"]
        assert ["literal", "g0"] in expressions
        assert ["literal", "g27"] in expressions
