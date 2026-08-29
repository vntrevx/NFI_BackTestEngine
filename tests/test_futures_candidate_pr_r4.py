from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, TypedDict

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "futures_candidate_pr_r4",
    ROOT / "scripts" / "futures_candidate_pr.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PublicationPlan(TypedDict):
    branch: str
    trading_mode: str
    fixture_id: str
    fingerprint: str
    upstream_commit: str
    engine_commit: str
    strategy_sha256: str
    target_ids: list[str]
    logical_bytes: int
    fixture_source: str
    fixture_destination: str
    evidence_destination: str
    manifest_sha256: str
    pair: str
    timerange: str


def test_stale_ref_immediately_before_push_has_zero_remote_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "fixture.json").write_text("{}", encoding="utf-8")
    plan = PublicationPlan(
        branch="automation/futures-fixture-deadbeef",
        trading_mode="futures",
        fixture_id="candidate",
        fingerprint="a" * 64,
        upstream_commit="d" * 40,
        engine_commit="c" * 40,
        strategy_sha256="e" * 64,
        target_ids=["target"],
        logical_bytes=2,
        fixture_source=str(source),
        fixture_destination="benchmarks/fixtures/captured/candidate",
        evidence_destination="benchmarks/evidence/candidate.json",
        manifest_sha256="f" * 64,
        pair="BTC/USDT:USDT",
        timerange="20220401-20220420",
    )
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_kwargs: Any) -> str:
        calls.append(arguments)
        if arguments[:3] == ["git", "ls-remote", "--heads"]:
            return ""
        if arguments[:3] == ["git", "diff", "--cached"]:
            return f"{plan['fixture_destination']}/fixture.json\n"
        if arguments[:3] == ["git", "ls-remote", "origin"]:
            return "9" * 40
        if arguments[:2] == ["git", "ls-remote"]:
            return "d" * 40
        return ""

    monkeypatch.setattr(MODULE, "_open_pr", lambda *_args: None)
    monkeypatch.setattr(MODULE, "_run", fake_run)

    with pytest.raises(MODULE.CandidatePublicationError, match="current refs changed"):
        MODULE.publish_candidate(
            plan,
            repository_root=tmp_path,
            repository="owner/repository",
            base="main",
        )

    assert not any(call[:2] == ["git", "push"] for call in calls)
    assert not any(call[:3] == ["gh", "pr", "create"] for call in calls)
