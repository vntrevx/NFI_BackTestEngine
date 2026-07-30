from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nfi_backtest_engine.futures_discovery as discovery
import nfi_backtest_engine.futures_discovery_runtime as discovery_runtime
import pytest
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import (
    DiscoveryInfrastructureError,
    SpecValidationError,
)
from nfi_backtest_engine.futures_discovery import (
    build_discovery_request,
    discover_futures_targets,
    discover_targets,
    load_discovery_policy,
    search_shards,
)
from nfi_backtest_engine.reference.contracts import REFERENCE_INDEX_DIGEST

ROOT = Path(__file__).parents[1]
POLICY = ROOT / "planning" / "futures-discovery-policy.json"
SPOT_POLICY = ROOT / "planning" / "spot-discovery-policy.json"
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


def test_freqtrade_decorated_exit_route_is_searchable_and_sealed_exactly() -> None:
    surface = {
        "trades": [
            {
                "entry_tag": "65 ",
                "exit_reason": "exit_long_rebuy_e_r ( 65 )",
                "orders": [],
            }
        ]
    }
    features = discovery_runtime._surface_features(surface)
    target = {
        **_target(),
        "value": "exit_long_rebuy_e_r",
        "tags": ["exit_long_rebuy_e_r"],
    }

    assert discovery_runtime.target_observed(target, features) is True
    required = discovery_runtime._required_coverage(
        [target],
        {
            "entry_tag": "65",
            "exit_reason": "exit_long_rebuy_e_r ( 65 )",
        },
    )
    assert required["exit_reasons"] == ["exit_long_rebuy_e_r ( 65 )"]


def test_previous_lane_replays_boolean_mapping_transition_from_diff() -> None:
    assert discovery_runtime._baseline_boolean_toggles(
        {
            "changes": {
                "boolean_mappings": [
                    {
                        "mapping": "long_entry_signal_params",
                        "key": "route_enable",
                        "old": False,
                        "new": True,
                    }
                ]
            }
        }
    ) == [
        {
            "mapping": "long_entry_signal_params",
            "key": "route_enable",
            "expected": False,
            "replacement": True,
        }
    ]


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
    assert first["identity"]["freqtrade_image_digest"] == REFERENCE_INDEX_DIGEST

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

    paired = build_discovery_request(
        _difference(_target()),
        {"native_compatible": True},
        FIXTURES,
        policy=policy,
        upstream_commit="a" * 40,
        baseline_upstream_commit="c" * 40,
        baseline_source_sha256="d" * 64,
        engine_commit="b" * 40,
        as_of=date(2026, 7, 30),
    )
    assert paired["fingerprint"] != first["fingerprint"]
    assert paired["identity"]["baseline_upstream_commit"] == "c" * 40
    assert paired["identity"]["baseline_strategy_sha256"] == "d" * 64


def test_request_fingerprint_changes_with_pinned_freqtrade_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = load_discovery_policy(POLICY)
    kwargs = {
        "policy": policy,
        "upstream_commit": "a" * 40,
        "engine_commit": "b" * 40,
        "as_of": date(2026, 7, 30),
    }
    first = build_discovery_request(
        _difference(_target()),
        {"native_compatible": True},
        FIXTURES,
        **kwargs,
    )
    monkeypatch.setattr(
        discovery,
        "REFERENCE_INDEX_DIGEST",
        "sha256:" + "e" * 64,
    )
    changed = build_discovery_request(
        _difference(_target()),
        {"native_compatible": True},
        FIXTURES,
        **kwargs,
    )

    assert changed["fingerprint"] != first["fingerprint"]
    assert changed["identity"]["freqtrade_image_digest"] == "sha256:" + "e" * 64


def test_request_rejects_unpaired_baseline_identity() -> None:
    policy = load_discovery_policy(POLICY)

    with pytest.raises(SpecValidationError, match="must be supplied together"):
        build_discovery_request(
            _difference(_target()),
            {"native_compatible": True},
            FIXTURES,
            policy=policy,
            upstream_commit="a" * 40,
            baseline_upstream_commit="c" * 40,
            engine_commit="b" * 40,
            as_of=date(2026, 7, 30),
        )


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
    assert "Existing exact fixtures" in report["message"]
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


def test_external_market_failure_is_infrastructure_not_unsupported_semantics(
    tmp_path: Path,
) -> None:
    report = _discover(
        tmp_path,
        output_name="market-infrastructure",
        scout_service=lambda *_args: (_ for _ in ()).throw(
            DiscoveryInfrastructureError("market catalog HTTP 451")
        ),
    )

    assert report["status"] == "infrastructure_failed"
    assert report["message"] == "market catalog HTTP 451"
    assert report["attempts"][0]["outcome"] == "infrastructure_failed"
    assert report["official_fallback_available"] is True


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
    assert "paired previous/latest proof" in report["message"]


def test_removed_only_gap_searches_previous_surface_for_absence_proof(
    tmp_path: Path,
) -> None:
    source, _profile = _files(tmp_path)
    baseline = tmp_path / "baseline.py"
    baseline.write_text("class Demo: pass\n# previous\n", encoding="utf-8")
    policy = load_discovery_policy(POLICY)
    removed = {
        **_target("removed-route"),
        "change": "removed",
        "value": "old-route",
        "tags": ["old-route"],
    }
    request = build_discovery_request(
        _difference(removed),
        {"native_compatible": True},
        FIXTURES,
        policy=policy,
        upstream_commit="a" * 40,
        baseline_upstream_commit="c" * 40,
        baseline_source_sha256=discovery.sha256_file(baseline),
        engine_commit="b" * 40,
        as_of=date(2026, 7, 30),
    )

    assert request["searchable_targets"] == [removed]
    assert request["unsearchable_targets"] == []
    context = SimpleNamespace(
        source=source,
        baseline_source=baseline,
        baseline_upstream_commit="c" * 40,
        search_targets=[removed],
    )
    assert discovery_runtime._scout_strategy_source(context) == baseline


def test_removed_target_does_not_block_searchable_transition_partner(
    tmp_path: Path,
) -> None:
    added = _target("added-route")
    removed = {
        **_target("removed-route"),
        "change": "removed",
        "value": "old-route",
        "tags": ["old-route"],
    }
    seen: list[list[str]] = []

    def candidate(_shard, context):
        seen.append([target["id"] for target in context.search_targets])
        assert {target["id"] for target in context.targets} == {
            "added-route",
            "removed-route",
        }
        return {
            "outcome": "candidate",
            "message": "paired transition exact",
            "target_ids": ["added-route"],
            "candidate": {
                "proved_target_ids": ["added-route", "removed-route"],
                "trade_surface_exact": True,
                "full_state_exact": True,
            },
        }

    report = _discover(
        tmp_path,
        output_name="mixed-transition",
        difference=_difference(added, removed),
        scout_service=candidate,
    )

    assert seen == [["added-route"]]
    assert report["status"] == "candidate_found"
    assert report["unsearchable_target_ids"] == ["removed-route"]
    assert report["candidate"]["proved_target_ids"] == [
        "added-route",
        "removed-route",
    ]


def test_spot_policy_uses_the_shared_discovery_service(tmp_path: Path) -> None:
    source, profile = _files(tmp_path)
    seen_modes: list[str] = []

    report = discover_targets(
        source,
        _difference(_target()),
        {"native_compatible": True},
        FIXTURES,
        SPOT_POLICY,
        tmp_path / "spot",
        class_name="Demo",
        upstream_repository="iterativv/NostalgiaForInfinity",
        upstream_commit="a" * 40,
        engine_commit="b" * 40,
        profile_path=profile,
        as_of=date(2026, 7, 30),
        scout_service=lambda _shard, context: (
            seen_modes.append(context.policy.trading_mode)
            or {
                "outcome": "candidate",
                "message": "spot exact",
                "target_ids": ["target-new-route"],
                "candidate": {
                    "trade_surface_exact": True,
                    "full_state_exact": True,
                },
            }
        ),
    )

    assert seen_modes == ["spot"]
    assert report["trading_mode"] == "spot"
    assert report["status"] == "candidate_found"


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
