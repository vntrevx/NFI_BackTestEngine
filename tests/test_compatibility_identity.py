from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compatibility_identity",
    ROOT / "scripts" / "compatibility_identity.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_identity_skips_only_when_both_revisions_match() -> None:
    report = MODULE.decide_run(
        upstream_sha="a" * 40,
        engine_sha="b" * 40,
        freqtrade_digest="sha256:" + "c" * 64,
        previous_upstream_sha="a" * 40,
        previous_engine_sha="b" * 40,
        previous_freqtrade_digest="sha256:" + "c" * 64,
    )

    assert report["changed"] is False
    assert report["reason"] == "unchanged"


def test_engine_change_rechecks_same_upstream() -> None:
    report = MODULE.decide_run(
        upstream_sha="a" * 40,
        engine_sha="c" * 40,
        freqtrade_digest="sha256:" + "d" * 64,
        previous_upstream_sha="a" * 40,
        previous_engine_sha="b" * 40,
        previous_freqtrade_digest="sha256:" + "d" * 64,
    )

    assert report["changed"] is True
    assert report["reason"] == "engine-changed"


def test_manual_force_rechecks_same_identity() -> None:
    report = MODULE.decide_run(
        upstream_sha="a" * 40,
        engine_sha="b" * 40,
        freqtrade_digest="sha256:" + "c" * 64,
        previous_upstream_sha="a" * 40,
        previous_engine_sha="b" * 40,
        previous_freqtrade_digest="sha256:" + "c" * 64,
        force=True,
    )

    assert report["changed"] is True
    assert report["reason"] == "manual-force"


def test_freqtrade_digest_change_rechecks_same_source_and_engine() -> None:
    report = MODULE.decide_run(
        upstream_sha="a" * 40,
        engine_sha="b" * 40,
        freqtrade_digest="sha256:" + "d" * 64,
        previous_upstream_sha="a" * 40,
        previous_engine_sha="b" * 40,
        previous_freqtrade_digest="sha256:" + "c" * 64,
    )

    assert report["changed"] is True
    assert report["reason"] == "freqtrade-changed"
