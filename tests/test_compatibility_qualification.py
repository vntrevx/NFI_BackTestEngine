from __future__ import annotations

from nfi_backtest_engine.compatibility_qualification import qualify_compatibility


def _check() -> dict:
    return {
        "native_compatible": True,
        "trading_mode": "spot",
        "source": {"sha256": "a" * 64},
    }


def test_latest_checked_is_not_promoted_without_changed_branch_proof() -> None:
    report = qualify_compatibility(
        _check(),
        {"classification": "vector-only"},
    )

    assert report["verification_state"] == "latest_checked"
    assert report["blockers"][0]["code"] == "CHANGED_BRANCH_PROOF_REQUIRED"


def test_quick_verified_requires_trade_surface_and_full_state_exactness() -> None:
    report = qualify_compatibility(
        _check(),
        {"classification": "ir-compatible"},
        branch_proof={
            "complete": True,
            "changed_branch_reached": True,
            "trade_surface_exact": True,
            "full_state_exact": True,
        },
    )

    assert report["verification_state"] == "quick_verified"
    assert report["blockers"] == []


def test_full_state_mismatch_prevents_promotion() -> None:
    report = qualify_compatibility(
        _check(),
        {"classification": "stateful-review"},
        branch_proof={
            "complete": True,
            "changed_branch_reached": True,
            "trade_surface_exact": True,
            "full_state_exact": False,
        },
    )

    assert report["verification_state"] == "latest_checked"
    assert report["full_state_exact"] is False
