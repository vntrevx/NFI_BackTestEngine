from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compatibility_identity",
    ROOT / "scripts" / "compatibility_identity.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_identity_skips_only_when_all_four_identities_match() -> None:
    report = MODULE.decide_run(
        upstream_sha="a" * 40,
        engine_sha="b" * 40,
        freqtrade_digest="sha256:" + "c" * 64,
        semantic_profile_sha256="d" * 64,
        previous_upstream_sha="a" * 40,
        previous_engine_sha="b" * 40,
        previous_freqtrade_digest="sha256:" + "c" * 64,
        previous_semantic_profile_sha256="d" * 64,
    )

    assert report["changed"] is False
    assert report["reason"] == "unchanged"


def test_engine_change_rechecks_same_upstream() -> None:
    report = MODULE.decide_run(
        upstream_sha="a" * 40,
        engine_sha="c" * 40,
        freqtrade_digest="sha256:" + "d" * 64,
        semantic_profile_sha256="e" * 64,
        previous_upstream_sha="a" * 40,
        previous_engine_sha="b" * 40,
        previous_freqtrade_digest="sha256:" + "d" * 64,
        previous_semantic_profile_sha256="e" * 64,
    )

    assert report["changed"] is True
    assert report["reason"] == "engine-changed"


def test_manual_force_rechecks_same_identity() -> None:
    report = MODULE.decide_run(
        upstream_sha="a" * 40,
        engine_sha="b" * 40,
        freqtrade_digest="sha256:" + "c" * 64,
        semantic_profile_sha256="d" * 64,
        previous_upstream_sha="a" * 40,
        previous_engine_sha="b" * 40,
        previous_freqtrade_digest="sha256:" + "c" * 64,
        previous_semantic_profile_sha256="d" * 64,
        force=True,
    )

    assert report["changed"] is True
    assert report["reason"] == "manual-force"


def test_freqtrade_digest_change_rechecks_same_source_and_engine() -> None:
    report = MODULE.decide_run(
        upstream_sha="a" * 40,
        engine_sha="b" * 40,
        freqtrade_digest="sha256:" + "d" * 64,
        semantic_profile_sha256="e" * 64,
        previous_upstream_sha="a" * 40,
        previous_engine_sha="b" * 40,
        previous_freqtrade_digest="sha256:" + "c" * 64,
        previous_semantic_profile_sha256="e" * 64,
    )

    assert report["changed"] is True
    assert report["reason"] == "freqtrade-changed"


def test_semantic_profile_change_rechecks_same_other_identities() -> None:
    report = MODULE.decide_run(
        upstream_sha="a" * 40,
        engine_sha="b" * 40,
        freqtrade_digest="sha256:" + "c" * 64,
        semantic_profile_sha256="e" * 64,
        previous_upstream_sha="a" * 40,
        previous_engine_sha="b" * 40,
        previous_freqtrade_digest="sha256:" + "c" * 64,
        previous_semantic_profile_sha256="d" * 64,
    )

    assert report["changed"] is True
    assert report["reason"] == "semantic-profile-changed"


def test_semantic_profile_identity_must_be_canonical_sha256() -> None:
    with pytest.raises(ValueError, match="semantic profile SHA-256"):
        MODULE.decide_run(
            upstream_sha="a" * 40,
            engine_sha="b" * 40,
            freqtrade_digest="sha256:" + "c" * 64,
            semantic_profile_sha256="not-a-fingerprint",
        )
