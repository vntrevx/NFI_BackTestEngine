from __future__ import annotations

import copy

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.portfolio_semantic_registry import (
    portfolio_registry_fingerprint,
    portfolio_semantic_obligation_rows,
    validate_portfolio_semantic_obligation_rows,
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


def _scheduler() -> dict[str, str]:
    return {"fingerprint": "a" * 64}


def test_built_semantic_registry_contains_the_additive_portfolio_matrix(tmp_path) -> None:
    source = tmp_path / "PortfolioRegistryStrategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class PortfolioRegistryStrategy(IStrategy):\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        return dataframe\n",
        encoding="utf-8",
    )
    registry = build_semantic_obligation_registry(source, class_name="PortfolioRegistryStrategy")
    rows = [
        record
        for group in registry["obligation_groups"]
        for record in group["obligations"]
        if record["preimage"]["normalized_semantics"][0].startswith("portfolio-contract:")
    ]
    assert len(rows) == 18
    assert registry["summary"]["unknown_obligations"] == 1


def test_portfolio_obligation_registry_is_complete_deterministic_and_closed() -> None:
    first = portfolio_semantic_obligation_rows(_freqtrade(), _scheduler())
    second = portfolio_semantic_obligation_rows(_freqtrade(), _scheduler())

    assert first == second
    assert len(first) == 18
    assert len({row["dimension"] for row in first}) == len(first)
    assert len({row["boundary_row"] for row in first}) == len(first)
    assert portfolio_registry_fingerprint(first) == portfolio_registry_fingerprint(second)


@pytest.mark.parametrize(
    "dimension",
    [
        "timestamp-batch",
        "configured-order-2",
        "configured-order-3",
        "open-trade-first",
        "pair-once",
        "wallet-free",
        "wallet-tied",
        "wallet-realized",
        "slot-occupancy",
        "rejected-stake",
        "partial-exit-release",
        "compounding-base",
        "trade-id",
        "order-id",
        "final-trades",
        "artifact-identity",
        "source-hash",
        "contract-hash",
    ],
)
def test_portfolio_registry_detects_each_required_dimension_mutation(dimension: str) -> None:
    rows = [dict(row) for row in portfolio_semantic_obligation_rows(_freqtrade(), _scheduler())]
    target = next(row for row in rows if row["dimension"] == dimension)
    target["rule"] = "mutated"

    with pytest.raises(SpecValidationError, match="PORTFOLIO_SEMANTIC_REGISTRY"):
        validate_portfolio_semantic_obligation_rows(rows, _freqtrade(), _scheduler())


def test_portfolio_registry_rejects_duplicate_and_source_contract_mutations() -> None:
    rows = [dict(row) for row in portfolio_semantic_obligation_rows(_freqtrade(), _scheduler())]
    duplicate = copy.deepcopy(rows)
    duplicate[1]["boundary_row"] = duplicate[0]["boundary_row"]
    with pytest.raises(SpecValidationError, match="duplicate"):
        validate_portfolio_semantic_obligation_rows(duplicate, _freqtrade(), _scheduler())

    with pytest.raises(SpecValidationError, match="source identity"):
        portfolio_semantic_obligation_rows({"source": {}}, _scheduler())
    with pytest.raises(SpecValidationError, match="scheduler contract"):
        portfolio_semantic_obligation_rows(_freqtrade(), {"fingerprint": "bad"})
