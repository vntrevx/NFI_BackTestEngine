from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compatibility_canary",
    ROOT / "scripts" / "compatibility_canary.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _inputs() -> tuple[dict, dict, dict, dict, dict, dict]:
    source_sha256 = "e" * 64
    identity = {
        "schema_version": "1.1.0",
        "upstream_sha": "a" * 40,
        "engine_sha": "b" * 40,
        "freqtrade_digest": "sha256:" + "c" * 64,
        "semantic_profile_sha256": "d" * 64,
        "source_sha256": source_sha256,
    }
    strategy_diff = {"new": {"sha256": source_sha256}}
    reports = {
        mode: {
            "schema_version": "1.0.0",
            "trading_mode": mode,
            "source": {"sha256": source_sha256},
            "native_compatible": True,
            "blockers": [],
        }
        for mode in ("spot", "futures")
    }
    qualifications = {
        mode: {
            "schema_version": "1.0.0",
            "strategy_sha256": source_sha256,
            "verification_state": "quick_verified",
            "blockers": [],
        }
        for mode in ("spot", "futures")
    }
    targeted = {
        mode: {
            "schema_version": "1.0.0",
            "trading_mode": mode,
            "source_sha256": source_sha256,
            "runs": [{}],
            "blockers": [],
            "complete": True,
            "verification_state": "quick_verified",
            "qualification": qualifications[mode],
        }
        for mode in ("spot", "futures")
    }
    profile = {"fingerprint": "d" * 64}
    return identity, strategy_diff, reports, targeted, qualifications, profile


def test_canary_seals_four_identities_and_both_modes() -> None:
    inputs = _inputs()

    first = MODULE.build_hosted_canary(*inputs[:-1], semantic_profile=inputs[-1])
    second = MODULE.build_hosted_canary(*inputs[:-1], semantic_profile=inputs[-1])

    assert first == second
    assert first["complete"] is True
    assert first["identity_advancement_allowed"] is True
    assert set(first["identity"]) == {
        "nfi_upstream_sha",
        "engine_sha",
        "freqtrade_digest",
        "semantic_profile_sha256",
    }
    assert [result["trading_mode"] for result in first["modes"]] == [
        "spot",
        "futures",
    ]


def test_canary_rejects_an_incomplete_mode_set() -> None:
    identity, difference, reports, targeted, qualifications, profile = _inputs()
    reports.pop("futures")

    with pytest.raises(ValueError, match="futures compatibility report is missing"):
        MODULE.build_hosted_canary(
            identity,
            difference,
            reports,
            targeted,
            qualifications,
            semantic_profile=profile,
        )


def test_canary_rejects_cross_mode_or_source_substitution() -> None:
    identity, difference, reports, targeted, qualifications, profile = _inputs()
    reports["futures"]["source"]["sha256"] = "f" * 64

    with pytest.raises(ValueError, match="futures compatibility report has a different source"):
        MODULE.build_hosted_canary(
            identity,
            difference,
            reports,
            targeted,
            qualifications,
            semantic_profile=profile,
        )


def test_canary_rejects_a_detached_qualification() -> None:
    identity, difference, reports, targeted, qualifications, profile = _inputs()
    qualifications = copy.deepcopy(qualifications)
    qualifications["spot"]["verification_state"] = "latest_checked"

    with pytest.raises(ValueError, match="spot targeted report and qualification differ"):
        MODULE.build_hosted_canary(
            identity,
            difference,
            reports,
            targeted,
            qualifications,
            semantic_profile=profile,
        )


def test_canary_records_completed_blocked_checks_without_claiming_exact() -> None:
    identity, difference, reports, targeted, qualifications, profile = _inputs()
    qualifications["futures"]["verification_state"] = "latest_checked"
    qualifications["futures"]["blockers"] = [{"code": "NEW_OPCODE"}]
    targeted["futures"]["qualification"] = qualifications["futures"]
    targeted["futures"]["complete"] = False
    targeted["futures"]["verification_state"] = "latest_checked"
    targeted["futures"]["blockers"] = [{"code": "NEW_OPCODE"}]
    reports["futures"]["native_compatible"] = False
    reports["futures"]["blockers"] = [{"code": "NEW_OPCODE"}]

    canary = MODULE.build_hosted_canary(
        identity,
        difference,
        reports,
        targeted,
        qualifications,
        semantic_profile=profile,
    )

    futures = canary["modes"][1]
    assert canary["complete"] is True
    assert futures["native_compatible"] is False
    assert futures["quick_verified"] is False
