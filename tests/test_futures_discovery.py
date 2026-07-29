from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.futures_discovery import (
    build_discovery_request,
    discover_futures_targets,
    load_discovery_policy,
    search_shards,
)

ROOT = Path(__file__).parents[1]
POLICY = ROOT / "planning" / "futures-discovery-policy.json"
FIXTURES = ROOT / "benchmarks" / "fixtures" / "captured"


def _target(identifier: str = "target-new-route") -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "tag",
        "change": "added",
        "value": "new-route",
        "methods": ["populate_entry_trend"],
        "tags": ["new-route"],
        "runtime_observable": True,
    }


def _difference(*targets: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.2.0",
        "classification": "vector-only",
        "new": {"sha256": "1" * 64},
        "behavior_targets": list(targets),
    }


def _files(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "strategy.py"
    source.write_text("class Demo: pass\n", encoding="utf-8")
    profile = tmp_path / "profile.json"
    write_json(profile, {"profile": "test"})
    return source, profile


def _discover(
    tmp_path: Path,
    *,
    output_name: str,
    difference: dict[str, Any] | None = None,
    compatibility: dict[str, Any] | None = None,
    cursor_path: Path | None = None,
    scout_service=None,
    clock=lambda: 0.0,
) -> dict[str, Any]:
    source, profile = _files(tmp_path)
    return discover_futures_targets(
        source,
        difference or _difference(_target()),
        compatibility or {"native_compatible": True},
        FIXTURES,
        POLICY,
        tmp_path / output_name,
        class_name="Demo",
        upstream_repository="iterativv/NostalgiaForInfinity",
        upstream_commit="a" * 40,
        engine_commit="b" * 40,
        profile_path=profile,
        cursor_path=cursor_path,
        as_of=date(2026, 7, 30),
        scout_service=scout_service,
        clock=clock,
    )


def test_policy_is_declarative_and_repository_bound() -> None:
    policy = load_discovery_policy(POLICY)

    assert policy.completed_years == 5
    assert policy.shard_months == 3
    assert policy.pair_limit == 80
    assert policy.budget_seconds == 7200
    assert policy.max_candidate_bytes == 30 * 1024 * 1024
    assert policy.template_config.is_file()


def test_policy_rejects_non_divisible_calendar_shards(tmp_path: Path) -> None:
    document = read_json(POLICY)
    document["history"]["shard_months"] = 5
    policy = tmp_path / "policy.json"
    write_json(policy, document)

    with pytest.raises(SpecValidationError, match="divide one calendar year"):
        load_discovery_policy(policy, repository_root=ROOT)


def test_search_shards_are_newest_first_completed_calendar_quarters() -> None:
    shards = search_shards(
        date(2026, 7, 30),
        completed_years=5,
        shard_months=3,
    )

    assert len(shards) == 20
    assert shards[0].timerange == "20251001-20260101"
    assert shards[-1].timerange == "20210101-20210401"
    assert [shard.index for shard in shards] == list(range(20))


def test_request_binds_targets_policy_upstream_engine_and_windows() -> None:
    policy = load_discovery_policy(POLICY)
    first = build_discovery_request(
        _difference(_target()),
        {"native_compatible": True},
        FIXTURES,
        policy=policy,
        upstream_commit="a" * 40,
        engine_commit="b" * 40,
        as_of=date(2026, 7, 30),
    )
    second = build_discovery_request(
        _difference(_target()),
        {"native_compatible": True},
        FIXTURES,
        policy=policy,
        upstream_commit="a" * 40,
        engine_commit="b" * 40,
        as_of=date(2026, 7, 30),
    )

    assert first == second
    assert first["plan_status"] == "coverage-gap"
    assert first["missing_targets"] == [_target()]
    assert first["searchable_targets"] == [_target()]
    assert first["unsearchable_targets"] == []
    assert len(first["identity"]["timeranges"]) == 20

    same_completed_years = build_discovery_request(
        _difference(_target()),
        {"native_compatible": True},
        FIXTURES,
        policy=policy,
        upstream_commit="a" * 40,
        engine_commit="b" * 40,
        as_of=date(2026, 12, 31),
    )
    assert same_completed_years["fingerprint"] == first["fingerprint"]


def test_no_gap_does_not_start_a_deep_search(tmp_path: Path) -> None:
    def unexpected(*_args):
        raise AssertionError("deep scout must not run")

    report = _discover(
        tmp_path,
        output_name="no-gap",
        difference=_difference(),
        scout_service=unexpected,
    )

    assert report["status"] == "no_gap"
    assert "Existing exact Futures fixtures" in report["message"]
    assert report["searched_shard_count"] == 0
    assert report["complete"] is True


def test_static_blocker_keeps_official_fallback_without_search(tmp_path: Path) -> None:
    def unexpected(*_args):
        raise AssertionError("Native scout must not run for static blockers")

    report = _discover(
        tmp_path,
        output_name="unsupported",
        compatibility={"native_compatible": False},
        scout_service=unexpected,
    )

    assert report["status"] == "unsupported_semantics"
    assert report["official_fallback_available"] is True
    assert "Static Native compatibility is blocked" in report["message"]
    assert report["searched_shard_count"] == 0


@pytest.mark.parametrize(
    "target",
    [
        {**_target(), "change": "removed"},
        {
            **_target(),
            "kind": "callback",
            "value": "new_callback",
            "tags": [],
        },
        {**_target(), "runtime_observable": False},
    ],
)
def test_unsearchable_gap_fails_closed_without_guessing(
    tmp_path: Path,
    target: dict[str, Any],
) -> None:
    def unexpected(*_args):
        raise AssertionError("unprovable target must not start the Native scout")

    report = _discover(
        tmp_path,
        output_name=f"unsearchable-{target['change']}-{target['kind']}",
        difference=_difference(target),
        scout_service=unexpected,
    )

    assert report["status"] == "unsupported_semantics"
    assert report["target_count"] == 1
    assert report["searchable_target_count"] == 0
    assert report["unsearchable_target_ids"] == [target["id"]]
    assert report["official_fallback_available"] is True
    assert "cannot independently prove" in report["message"]


def test_budget_cursor_resumes_at_the_next_unsearched_shard(tmp_path: Path) -> None:
    times = iter([0.0, 0.0, 7201.0, 7201.0, 7201.0])

    first = _discover(
        tmp_path,
        output_name="first",
        scout_service=lambda shard, _context: {
            "outcome": "miss",
            "message": f"missed {shard.timerange}",
            "target_ids": [],
        },
        clock=lambda: next(times),
    )

    assert first["status"] == "budget_exhausted"
    assert first["searched_shard_count"] == 1
    assert first["next_shard"] == 1
    cursor = tmp_path / "first" / "cursor.json"

    seen: list[int] = []

    def candidate(shard, _context):
        seen.append(shard.index)
        return {
            "outcome": "candidate",
            "message": "exact",
            "target_ids": ["target-new-route"],
            "candidate": {
                "manifest_path": "candidate/manifest.json",
                "trade_surface_exact": True,
                "full_state_exact": True,
            },
        }

    second = _discover(
        tmp_path,
        output_name="second",
        cursor_path=cursor,
        scout_service=candidate,
    )

    assert seen == [1]
    assert second["status"] == "candidate_found"
    assert second["next_shard"] == 2
    assert second["candidate"]["full_state_exact"] is True


def test_cursor_cannot_cross_identity(tmp_path: Path) -> None:
    first = _discover(
        tmp_path,
        output_name="cursor-source",
        scout_service=lambda *_args: {
            "outcome": "unsupported",
            "message": "stop",
            "target_ids": [],
        },
    )
    assert first["status"] == "unsupported_semantics"

    with pytest.raises(SpecValidationError, match="cursor identity"):
        _discover(
            tmp_path,
            output_name="cursor-mismatch",
            difference=_difference(_target("different-target")),
            cursor_path=tmp_path / "cursor-source" / "cursor.json",
            scout_service=lambda *_args: {
                "outcome": "miss",
                "message": "miss",
                "target_ids": [],
            },
        )


def test_infrastructure_failure_is_not_misreported_as_semantic_gap(
    tmp_path: Path,
) -> None:
    report = _discover(
        tmp_path,
        output_name="infra",
        scout_service=lambda *_args: {
            "outcome": "infrastructure_failed",
            "message": "network unavailable",
            "target_ids": [],
        },
    )

    assert report["status"] == "infrastructure_failed"
    assert report["complete"] is False
    assert read_json(tmp_path / "infra" / "cursor.json")["next_shard"] == 1
