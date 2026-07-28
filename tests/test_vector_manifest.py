from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.vector_manifest import declared_vector_sha256


def test_declared_vector_sha_preserves_the_sealed_stage_digest(tmp_path: Path) -> None:
    vector = tmp_path / "vector.feather"
    vector.write_bytes(b"sealed vector payload")
    expected = "a" * 64

    assert (
        declared_vector_sha256(
            vector,
            {"sha256": expected},
            "BTC/USDT",
        )
        == expected
    )


@pytest.mark.parametrize(
    "value",
    [
        "a" * 63,
        "A" * 64,
        "g" * 64,
        123,
        None,
    ],
)
def test_declared_vector_sha_requires_a_canonical_token(
    tmp_path: Path,
    value: object,
) -> None:
    vector = tmp_path / "vector.feather"
    vector.write_bytes(b"sealed vector payload")

    with pytest.raises(StrategyAnalysisError, match="lacks a canonical SHA-256"):
        declared_vector_sha256(vector, {"sha256": value}, "BTC/USDT")


def test_declared_vector_sha_requires_the_vector_file(tmp_path: Path) -> None:
    with pytest.raises(StrategyAnalysisError, match="does not exist"):
        declared_vector_sha256(
            tmp_path / "missing.feather",
            {"sha256": "a" * 64},
            "BTC/USDT",
        )
