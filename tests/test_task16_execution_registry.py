from __future__ import annotations

import copy

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.execution_semantic_registry import (
    execution_registry_fingerprint,
    execution_semantic_obligation_rows,
    validate_execution_semantic_obligation_rows,
)
from nfi_backtest_engine.semantic_registry import build_semantic_obligation_registry


def _freqtrade() -> dict[str, object]:
    return {
        "source": {
            "commit": "6fa470939cc74bf0672e0e348a4d9b293072e43c",
            "observed_method_count": 15,
            "observed_method_merkle_root": (
                "54e428105e8b2108b76a5ae1fbdf4d948e1a27a853b1c0bcdee6f1ac5d1b0192"
            ),
        }
    }


def _execution() -> dict[str, str]:
    return {"fingerprint": "a" * 64}


def test_execution_registry_is_additive_deterministic_complete_and_closed() -> None:
    rows = execution_semantic_obligation_rows(_freqtrade(), _execution())
    assert rows == execution_semantic_obligation_rows(_freqtrade(), _execution())
    assert len(rows) == len({row["dimension"] for row in rows})
    assert len(rows) == len({row["boundary_row"] for row in rows})
    assert {
        "fill-entry",
        "fill-order-type",
        "limit-retry",
        "precision-amount-frozen-step",
        "fee-per-fill-count",
        "candidate-winner",
        "candidate-rejection-continue",
        "candidate-rejection-stop",
        "partial-exit-amount",
        "rejection-reason",
        "ambiguity",
        "visibility-before-after",
        "identity-binary-path",
    } <= {row["dimension"] for row in rows}
    assert execution_registry_fingerprint(rows) == execution_registry_fingerprint(rows)


@pytest.mark.parametrize(
    "index", range(len(execution_semantic_obligation_rows(_freqtrade(), _execution())))
)
def test_execution_registry_rejects_every_dimension_mutation(index: int) -> None:
    rows = [dict(row) for row in execution_semantic_obligation_rows(_freqtrade(), _execution())]
    rows[index]["rule"] = "mutated"
    with pytest.raises(SpecValidationError, match="EXECUTION_SEMANTIC_REGISTRY"):
        validate_execution_semantic_obligation_rows(rows, _freqtrade(), _execution())


def test_execution_registry_rejects_duplicate_and_identity_mutations() -> None:
    rows = [dict(row) for row in execution_semantic_obligation_rows(_freqtrade(), _execution())]
    duplicate = copy.deepcopy(rows)
    duplicate[1]["boundary_row"] = duplicate[0]["boundary_row"]
    with pytest.raises(SpecValidationError, match="duplicate"):
        validate_execution_semantic_obligation_rows(duplicate, _freqtrade(), _execution())
    with pytest.raises(SpecValidationError, match="source identity"):
        execution_semantic_obligation_rows({"source": {}}, _execution())
    with pytest.raises(SpecValidationError, match="execution contract"):
        execution_semantic_obligation_rows(_freqtrade(), {"fingerprint": "bad"})


def test_generated_registry_contains_closed_additive_execution_matrix(tmp_path) -> None:
    source = tmp_path / "ExecutionRegistryStrategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class ExecutionRegistryStrategy(IStrategy):\n"
        "    def populate_indicators(self, dataframe, metadata): return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata): return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata): return dataframe\n",
        encoding="utf-8",
    )
    registry = build_semantic_obligation_registry(source, class_name="ExecutionRegistryStrategy")
    rows = [
        record
        for group in registry["obligation_groups"]
        for record in group["obligations"]
        if record["preimage"]["normalized_semantics"][0].startswith("execution-contract:")
    ]
    assert len(rows) == len(execution_semantic_obligation_rows(_freqtrade(), _execution()))
