from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TypedDict

import pytest

ROOT = Path(__file__).parents[1]
FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20"
)
SPEC = importlib.util.spec_from_file_location(
    "futures_candidate_pr",
    ROOT / "scripts" / "futures_candidate_pr.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CandidateRecord(TypedDict):
    fixture_id: str
    manifest_sha256: str
    trade_surface_exact: bool
    full_state_exact: bool
    logical_bytes: int
    target_ids: list[str]
    pair: str
    timerange: str


class CandidateReport(TypedDict):
    status: str
    trading_mode: str
    fingerprint: str
    upstream_commit: str
    engine_commit: str
    strategy_sha256: str
    candidate: CandidateRecord


class PublicationPlan(TypedDict):
    branch: str
    trading_mode: str
    fixture_id: str
    fingerprint: str
    upstream_commit: str
    engine_commit: str
    target_ids: list[str]
    logical_bytes: int


def _bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _report() -> CandidateReport:
    logical_bytes = _bytes(FIXTURE)
    manifest = MODULE.validate_fixture(FIXTURE / "manifest.json")
    return {
        "status": "candidate_found",
        "trading_mode": "futures",
        "fingerprint": "a" * 64,
        "upstream_commit": manifest["strategy_provenance"]["upstream_commit"],
        "engine_commit": "c" * 40,
        "strategy_sha256": manifest["strategy_provenance"]["effective_source_sha256"],
        "candidate": {
            "fixture_id": manifest["fixture_id"],
            "manifest_sha256": MODULE.sha256_file(FIXTURE / "manifest.json"),
            "trade_surface_exact": True,
            "full_state_exact": True,
            "logical_bytes": logical_bytes,
            "target_ids": ["generic-target"],
            "pair": "BTC/USDT:USDT",
            "timerange": "20220401-20220420",
        },
    }


def test_candidate_plan_allows_only_exact_size_bounded_fixture(
    tmp_path: Path,
) -> None:
    plan = MODULE.build_candidate_plan(
        _report(),
        FIXTURE,
        tmp_path,
        max_bytes=30 * 1024 * 1024,
    )

    assert plan["branch"] == "automation/futures-fixture-" + "a" * 16
    assert plan["fixture_destination"] == (
        "benchmarks/fixtures/captured/x7-v17.4.435-futures-lifecycle-btc-short"
    )
    assert plan["evidence_destination"] == (
        "benchmarks/evidence/future-nfi-futures-" + "a" * 16 + ".json"
    )


def test_candidate_plan_rejects_non_exact_or_size_mismatched_evidence(
    tmp_path: Path,
) -> None:
    report = _report()
    report["candidate"]["full_state_exact"] = False
    with pytest.raises(ValueError, match="exact evidence"):
        MODULE.build_candidate_plan(
            report,
            FIXTURE,
            tmp_path,
            max_bytes=30 * 1024 * 1024,
        )

    report = _report()
    report["candidate"]["logical_bytes"] += 1
    with pytest.raises(ValueError, match="sealed size"):
        MODULE.build_candidate_plan(
            report,
            FIXTURE,
            tmp_path,
            max_bytes=30 * 1024 * 1024,
        )


def test_candidate_plan_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    report = _report()
    report["candidate"]["manifest_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="sealed manifest"):
        MODULE.build_candidate_plan(
            report,
            FIXTURE,
            tmp_path,
            max_bytes=30 * 1024 * 1024,
        )


def test_candidate_plan_rejects_cross_mode_manifest(tmp_path: Path) -> None:
    report = _report()
    report["trading_mode"] = "spot"

    with pytest.raises(ValueError, match="sealed manifest"):
        MODULE.build_candidate_plan(
            report,
            FIXTURE,
            tmp_path,
            max_bytes=30 * 1024 * 1024,
        )


def test_candidate_plan_rejects_symlinked_input(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.symlink_to(FIXTURE, target_is_directory=True)

    with pytest.raises(ValueError, match="root must not be a symlink"):
        MODULE.build_candidate_plan(
            _report(),
            candidate,
            tmp_path / "repo",
            max_bytes=30 * 1024 * 1024,
        )


def _publication_plan() -> PublicationPlan:
    return {
        "branch": "automation/futures-fixture-deadbeef",
        "trading_mode": "futures",
        "fixture_id": "candidate",
        "fingerprint": "a" * 64,
        "upstream_commit": "d" * 40,
        "engine_commit": "c" * 40,
        "target_ids": ["target"],
        "logical_bytes": 1,
    }


def test_candidate_pr_rechecks_refs_immediately_before_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_kwargs) -> str:
        calls.append(arguments)
        if arguments[:3] == ["git", "ls-remote", "--heads"]:
            return "existing branch"
        if arguments[:3] == ["git", "ls-remote", "origin"]:
            return "c" * 40
        if arguments[:2] == ["git", "ls-remote"]:
            return "d" * 40
        if arguments[:3] == ["gh", "pr", "create"]:
            return "https://example.invalid/pr"
        return ""

    monkeypatch.setattr(MODULE, "_open_pr", lambda *_args: None)
    monkeypatch.setattr(MODULE, "_run", fake_run)

    MODULE.publish_candidate(
        _publication_plan(),
        repository_root=tmp_path,
        repository="owner/repository",
        base="main",
    )

    create = next(index for index, call in enumerate(calls) if call[:3] == ["gh", "pr", "create"])
    assert calls[create - 2] == ["git", "ls-remote", "origin", "refs/heads/main"]
    assert calls[create - 1] == [
        "git",
        "ls-remote",
        "https://github.com/iterativv/NostalgiaForInfinity.git",
        "refs/heads/main",
    ]


@pytest.mark.parametrize(
    ("engine", "upstream"),
    [("9" * 40, "d" * 40), ("c" * 40, "9" * 40)],
)
def test_candidate_pr_rejects_stale_ref_before_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    upstream: str,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_kwargs) -> str:
        calls.append(arguments)
        if arguments[:3] == ["git", "ls-remote", "--heads"]:
            return "existing branch"
        if arguments[:3] == ["git", "ls-remote", "origin"]:
            return engine
        if arguments[:2] == ["git", "ls-remote"]:
            return upstream
        return ""

    monkeypatch.setattr(MODULE, "_open_pr", lambda *_args: None)
    monkeypatch.setattr(MODULE, "_run", fake_run)

    with pytest.raises(ValueError, match="current refs changed"):
        MODULE.publish_candidate(
            _publication_plan(),
            repository_root=tmp_path,
            repository="owner/repository",
            base="main",
        )

    assert not any(call[:3] == ["gh", "pr", "create"] for call in calls)


def test_candidate_pr_deduplication_includes_closed_and_merged_prs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_kwargs) -> str:
        calls.append(arguments)
        return '[{"number":47,"state":"MERGED","url":"https://example.invalid/47"}]'

    monkeypatch.setattr(MODULE, "_run", fake_run)

    existing = MODULE._open_pr("owner/repository", "automation/spot-fixture-deadbeef")

    assert existing == {
        "number": 47,
        "state": "MERGED",
        "url": "https://example.invalid/47",
    }
    assert "--state" in calls[0]
    assert calls[0][calls[0].index("--state") + 1] == "all"


def test_candidate_pr_reconciliation_keeps_one_review_slot_per_mode() -> None:
    plan = MODULE.build_candidate_pr_reconciliation(
        [
            {
                "number": 40,
                "headRefName": "automation/spot-fixture-current",
                "isDraft": True,
            },
            {
                "number": 41,
                "headRefName": "automation/spot-fixture-stale",
                "isDraft": True,
            },
            {
                "number": 42,
                "headRefName": "automation/spot-fixture-human-ready",
                "isDraft": False,
            },
            {
                "number": 43,
                "headRefName": "automation/futures-fixture-unrelated",
                "isDraft": True,
            },
            {
                "number": 44,
                "headRefName": "fix/unrelated",
                "isDraft": True,
            },
        ],
        trading_mode="spot",
        desired_branch="automation/spot-fixture-current",
    )

    assert plan == {
        "trading_mode": "spot",
        "desired_branch": "automation/spot-fixture-current",
        "keep": 40,
        "close": [41],
        "blocked": [42],
    }


def test_candidate_pr_reconciliation_closes_only_stale_drafts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_kwargs) -> str:
        calls.append(arguments)
        if arguments[:3] == ["gh", "pr", "list"]:
            return (
                '[{"number":41,"headRefName":"automation/futures-fixture-stale",'
                '"isDraft":true},{"number":42,'
                '"headRefName":"automation/futures-fixture-human-ready","isDraft":false},'
                '{"number":43,"headRefName":"automation/spot-fixture-unrelated",'
                '"isDraft":true}]'
            )
        return ""

    monkeypatch.setattr(MODULE, "_run", fake_run)
    result = MODULE.reconcile_candidate_prs(
        "owner/repository",
        trading_mode="futures",
        desired_branch=None,
        upstream_commit="d" * 40,
        engine_commit="c" * 40,
    )

    assert result["closed"] == [41]
    assert result["blocked"] == [42]
    close_calls = [call for call in calls if call[:3] == ["gh", "pr", "close"]]
    assert len(close_calls) == 1
    assert close_calls[0][3] == "41"
    assert "42" not in close_calls[0]


def test_non_draft_candidate_blocks_another_automated_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE, "_validate_current_refs", lambda *_args: None)
    monkeypatch.setattr(
        MODULE,
        "reconcile_candidate_prs",
        lambda *_args, **_kwargs: {"blocked": [42], "closed": [41]},
    )
    monkeypatch.setattr(MODULE, "_open_pr", lambda *_args: None)

    with pytest.raises(
        MODULE.CandidatePublicationError,
        match="non-Draft automation candidate",
    ):
        MODULE.publish_candidate(
            _publication_plan(),
            repository_root=tmp_path,
            repository="owner/repository",
            base="main",
        )
