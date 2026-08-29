from __future__ import annotations

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.native_scorecard import NATIVE_SCORE_GATE_IDS
from nfi_backtest_engine.release_gate import seal_release_gate


def test_native_score_release_gate_contract_is_exactly_ten_binary_points() -> None:
    assert NATIVE_SCORE_GATE_IDS == (
        "immutable_identity_scope",
        "evidence_independence",
        "native_purity",
        "semantic_closure",
        "changed_path_coverage_completeness",
        "vector_callback_exactness",
        "execution_complete_state_exactness",
        "generative_metamorphic_mutation_proof",
        "same_candidate_portfolio_platform_certification",
        "deterministic_performance_resource_proof",
    )


def test_release_gate_fails_closed_without_native_scorecard() -> None:
    with pytest.raises(SpecValidationError, match="scorecard.*required"):
        seal_release_gate(
            candidate_directory="missing",
            certificate_path="missing",
            certificate_evidence_path="missing",
            platform_evidence_path="missing",
            candidate_commit="1" * 40,
            output_directory="missing",
        )
