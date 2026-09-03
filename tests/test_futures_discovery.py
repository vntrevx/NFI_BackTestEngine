from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nfi_backtest_engine.futures_discovery as discovery
import nfi_backtest_engine.futures_discovery_runtime as discovery_runtime
import polars as pl
import pytest
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import (
    BenchmarkError,
    DiscoveryInfrastructureError,
    SpecValidationError,
)
from nfi_backtest_engine.fixture import sha256_file
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


def test_candidate_hit_stops_at_first_reached_target_order() -> None:
    targets = [
        {**_target("gm0-target"), "value": "gm0", "tags": ["gm0"]},
        {**_target("gd1-target"), "value": "gd1", "tags": ["gd1"]},
    ]
    hit = discovery_runtime._locate_hit(
        targets,
        ["gm0-target", "gd1-target"],
        {
            "trades": [
                {
                    "pair": "BTC/USDT",
                    "open_timestamp_ms": 10,
                    "close_timestamp_ms": 100,
                    "entry_tag": "120",
                    "exit_reason": "force_exit",
                    "orders": [
                        {"filled_timestamp_ms": 20, "tag": "gm0"},
                        {"filled_timestamp_ms": 80, "tag": "gd1"},
                    ],
                }
            ]
        },
    )

    assert hit == {
        "pair": "BTC/USDT",
        "open_timestamp_ms": 10,
        "event_timestamp_ms": 20,
        "entry_tag": "120",
        "exit_reason": "force_exit",
        "target_ids": ["gm0-target"],
    }
    assert discovery_runtime._candidate_targets(targets, list(hit["target_ids"])) == [targets[0]]


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


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("exchange request failed with HTTP 451", 451),
        ("503 Service Unavailable", 503),
        ("connection timed out", None),
        ("request id 14510 failed", None),
    ],
)
def test_external_http_status_is_provider_agnostic(
    message: str,
    expected: int | None,
) -> None:
    assert discovery_runtime._external_http_status(message) == expected


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
    assert policy.market_catalog.is_file()
    assert policy.market_catalog_sha256 == sha256_file(policy.market_catalog)
    assert policy.market_snapshot.is_file()
    assert policy.market_snapshot_sha256 == sha256_file(policy.market_snapshot)
    assert policy.budget_seconds == 7200
    assert policy.archive_workers == 8
    assert policy.archive_source == "binance-public-data-monthly"
    assert policy.deferred_http_statuses == frozenset({451})
    assert policy.external_retry == "identity-change-or-manual"
    assert policy.compact_artifact_retention_days == 1
    assert policy.context_days == 1
    assert policy.max_candidate_bytes == 30 * 1024 * 1024
    assert policy.template_config.is_file()


def test_policy_rejects_changed_market_catalog_hash(tmp_path: Path) -> None:
    document = read_json(POLICY)
    document["universe"]["market_catalog"]["sha256"] = "0" * 64
    policy = tmp_path / "policy.json"
    write_json(policy, document)

    with pytest.raises(SpecValidationError, match="market_catalog SHA-256 differs"):
        load_discovery_policy(policy, repository_root=ROOT)


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


def test_benchmark_failure_is_infrastructure_not_unsupported_semantics(
    tmp_path: Path,
) -> None:
    report = _discover(
        tmp_path,
        output_name="benchmark-infrastructure",
        scout_service=lambda *_args: (_ for _ in ()).throw(
            BenchmarkError("sealed dependency download failed")
        ),
    )

    assert report["status"] == "infrastructure_failed"
    assert report["complete"] is False
    assert report["next_shard"] == 0
    assert report["attempts"][0]["outcome"] == "infrastructure_failed"


def test_policy_selected_external_status_is_deferred_without_advancing(
    tmp_path: Path,
) -> None:
    report = _discover(
        tmp_path,
        output_name="market-deferred",
        scout_service=lambda *_args: (_ for _ in ()).throw(
            DiscoveryInfrastructureError(
                "market catalog HTTP 451",
                external_http_status=451,
            )
        ),
    )

    assert report["status"] == "external_data_deferred"
    assert report["complete"] is False
    assert report["next_shard"] == 0
    assert report["searched_shard_count"] == 1
    assert report["attempts"][0]["outcome"] == "external_data_deferred"
    assert report["external_data"] == {
        "reason": "http_status",
        "http_status": 451,
        "retry": "identity-change-or-manual",
    }
    cursor = read_json(tmp_path / "market-deferred" / "cursor.json")
    assert cursor["status"] == "external_data_deferred"
    assert cursor["next_shard"] == 0


def test_deferred_cursor_does_not_repeat_external_request(
    tmp_path: Path,
) -> None:
    first = _discover(
        tmp_path,
        output_name="market-deferred-first",
        scout_service=lambda *_args: (_ for _ in ()).throw(
            DiscoveryInfrastructureError(
                "market catalog HTTP 451",
                external_http_status=451,
            )
        ),
    )
    assert first["status"] == "external_data_deferred"

    def unexpected(*_args):
        raise AssertionError("a deferred identity must not repeat external access")

    resumed = _discover(
        tmp_path,
        output_name="market-deferred-resumed",
        cursor_path=tmp_path / "market-deferred-first" / "cursor.json",
        scout_service=unexpected,
    )

    assert resumed["status"] == "external_data_deferred"
    assert resumed["searched_shard_count"] == 0
    assert resumed["next_shard"] == 0
    assert resumed["external_data"] == first["external_data"]


def test_unselected_external_status_remains_infrastructure_failure(
    tmp_path: Path,
) -> None:
    report = _discover(
        tmp_path,
        output_name="market-unknown-infrastructure",
        scout_service=lambda *_args: (_ for _ in ()).throw(
            DiscoveryInfrastructureError(
                "market catalog HTTP 503",
                external_http_status=503,
            )
        ),
    )

    assert report["status"] == "infrastructure_failed"
    assert report["attempts"][0]["external_http_status"] == 503


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("deferred_http_statuses", [399], "HTTP error statuses"),
        ("deferred_http_statuses", [451, 451], "HTTP error statuses"),
        ("retry", "always", "identity-change-or-manual"),
    ],
)
def test_policy_rejects_invalid_external_data_contract(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    document = read_json(POLICY)
    document["external_data"][field] = value
    policy = tmp_path / "policy.json"
    write_json(policy, document)

    with pytest.raises(SpecValidationError, match=message):
        load_discovery_policy(policy, repository_root=ROOT)


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


@pytest.mark.parametrize(
    ("trading_mode", "market_type"),
    [("spot", "spot"), ("futures", "linear")],
)
def test_discovery_config_loads_only_the_requested_market_type(
    trading_mode: str,
    market_type: str,
) -> None:
    exchange: dict[str, Any] = {}

    discovery_runtime._pin_discovery_ccxt_market_type(exchange, trading_mode)

    assert exchange == {
        "ccxt_config": {
            "options": {
                "fetchMarkets": {"types": [market_type]},
            }
        }
    }


def test_discovery_universe_reuses_policy_pinned_market_inputs(
    tmp_path: Path,
) -> None:
    policy = load_discovery_policy(POLICY)
    context = SimpleNamespace(
        policy=policy,
        all_shards=search_shards(
            date(2026, 7, 30),
            completed_years=policy.completed_years,
            shard_months=policy.shard_months,
        ),
    )
    shared = tmp_path / "shared"
    shared.mkdir()

    config, pairs, market_snapshot = discovery_runtime._ensure_universe(context, shared)

    assert config.is_file()
    assert len(pairs) == policy.pair_limit
    assert sha256_file(market_snapshot) == policy.market_snapshot_sha256
    assert read_json(shared / "pairs.json")["catalog_sha256"] == (
        policy.market_catalog_sha256
    )

    market_snapshot.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SpecValidationError, match="missing or changed"):
        discovery_runtime._ensure_universe(context, shared)


def test_scout_archive_uses_strategy_requirements_and_persists_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    write_json(
        config,
        {
            "stake_currency": "USDT",
            "trading_mode": "spot",
            "exchange": {"name": "binance", "pair_whitelist": ["ETH/USDT"]},
        },
    )
    strategy = tmp_path / "strategy.py"
    strategy.write_text("class Demo: pass\n", encoding="utf-8")
    seen: dict[str, Any] = {}

    monkeypatch.setattr(
        "nfi_backtest_engine.vector_runtime.load_strategy_analysis",
        lambda *_args, **_kwargs: {
            "strategies": [
                {
                    "required_timeframes": ["5m", "1h"],
                    "constants": {"startup_candle_count": 10},
                }
            ]
        },
    )

    def prepare(_data_directory, **kwargs):
        seen.update(kwargs)
        return {"aggregate_sha256": "a" * 64}

    monkeypatch.setattr(
        "nfi_backtest_engine.binance_archive.prepare_binance_archive_data",
        prepare,
    )
    context = SimpleNamespace(
        class_name="Demo",
        output=tmp_path / "output",
        policy=SimpleNamespace(
            archive_source="binance-public-data-monthly",
            archive_workers=8,
            trading_mode="spot",
        ),
    )
    destination = tmp_path / "archive-download.json"

    report = discovery_runtime._prepare_scout_archive(
        search_shards(date(2026, 7, 30), completed_years=1, shard_months=3)[0],
        context,
        strategy_path=strategy,
        config_path=config,
        data_directory=tmp_path / "data",
        pairs=["ETH/USDT"],
        destination=destination,
    )

    assert report["aggregate_sha256"] == "a" * 64
    assert read_json(destination) == report
    assert seen["pairs"] == ["ETH/USDT", "BTC/USDT"]
    assert seen["timeframes"] == ["5m", "1h"]
    assert seen["workers"] == 8
    assert seen["coverage_start_timestamp_ms_by_timeframe"]["1h"] == (
        int(datetime(2025, 10, 1, tzinfo=UTC).timestamp() * 1000) - 10 * 3_600_000
    )


def test_spot_scout_archive_excludes_a_base_pair_with_an_internal_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nfi_backtest_engine.binance_archive import BinanceArchiveContinuityError

    config = tmp_path / "config.json"
    write_json(
        config,
        {
            "stake_currency": "USDT",
            "trading_mode": "spot",
            "exchange": {
                "name": "binance",
                "pair_whitelist": ["ETH/USDT", "SOL/USDT"],
            },
        },
    )
    strategy = tmp_path / "strategy.py"
    strategy.write_text("class Demo: pass\n", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(
        "nfi_backtest_engine.vector_runtime.load_strategy_analysis",
        lambda *_args, **_kwargs: {
            "strategies": [
                {
                    "required_timeframes": ["5m"],
                    "constants": {"startup_candle_count": 0},
                }
            ]
        },
    )

    def prepare(data_directory: Path, **kwargs):
        pairs = list(kwargs["pairs"])
        calls.append(pairs)
        if "ETH/USDT" in pairs:
            raise BinanceArchiveContinuityError(
                pair="ETH/USDT",
                source=data_directory / "ETH_USDT-5m.feather",
            )
        return {"aggregate_sha256": "a" * 64}

    monkeypatch.setattr(
        "nfi_backtest_engine.binance_archive.prepare_binance_archive_data",
        prepare,
    )
    context = SimpleNamespace(
        class_name="Demo",
        output=tmp_path / "output",
        policy=SimpleNamespace(
            archive_source="binance-public-data-monthly",
            archive_workers=2,
            trading_mode="spot",
        ),
    )
    destination = tmp_path / "archive-download.json"

    report = discovery_runtime._prepare_scout_archive(
        search_shards(date(2026, 7, 30), completed_years=1, shard_months=3)[0],
        context,
        strategy_path=strategy,
        config_path=config,
        data_directory=tmp_path / "data",
        pairs=["ETH/USDT", "SOL/USDT"],
        destination=destination,
    )

    assert calls == [
        ["ETH/USDT", "SOL/USDT", "BTC/USDT"],
        ["SOL/USDT", "BTC/USDT"],
    ]
    assert report["discovery_pairs"] == ["SOL/USDT"]
    assert report["excluded_discovery_pairs"] == [
        {"pair": "ETH/USDT", "reason": "INTERNAL_ARCHIVE_GAP"}
    ]


def test_scout_uses_compact_surface_without_unbounded_event_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def run_research_backtest(**kwargs):
        seen.update(kwargs)
        return {"complete": True}

    monkeypatch.setattr(
        "nfi_backtest_engine.research_runner.run_research_backtest",
        run_research_backtest,
    )
    context = SimpleNamespace(
        class_name="Demo",
        workers=1,
        output=tmp_path,
        profile_path=tmp_path / "profile.json",
    )

    result = discovery_runtime._run_scout_backtest(
        search_shards(date(2026, 7, 30), completed_years=1, shard_months=3)[0],
        context,
        strategy_path=tmp_path / "strategy.py",
        config_path=tmp_path / "config.json",
        data_directory=tmp_path / "data",
        output_directory=tmp_path / "engine",
        pairs=["BTC/USDT"],
        market_snapshot=tmp_path / "markets.json",
    )

    assert result == {"complete": True}
    assert seen["trace_engine_events"] is False
    assert seen["download_missing"] is False


def test_spot_discovery_retry_drops_empty_base_candles(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    write_json(config, {"timeframe": "5m", "trading_mode": "spot"})
    data = tmp_path / "data"
    data.mkdir()
    pl.DataFrame(schema={"date": pl.Datetime(time_unit="ms", time_zone="UTC")}).write_ipc(
        data / "AAOIB_USDT-5m.feather"
    )
    pl.DataFrame({"date": [datetime(2025, 10, 1, tzinfo=UTC)]}).write_ipc(
        data / "BTC_USDT-5m.feather"
    )

    available = discovery_runtime._pairs_with_nonempty_base_candles(
        data,
        pairs=["AAOIB/USDT", "BTC/USDT"],
        config_path=config,
        start_timestamp_ms=int(datetime(2025, 10, 1, tzinfo=UTC).timestamp() * 1000),
        end_timestamp_ms=int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000),
        startup_candles=0,
    )

    assert available == ["BTC/USDT"]


def test_spot_discovery_drops_pair_without_candles_inside_shard(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    write_json(config, {"timeframe": "5m", "trading_mode": "spot"})
    data = tmp_path / "data"
    data.mkdir()
    pl.DataFrame({"date": [datetime(2025, 10, 1, tzinfo=UTC)]}).write_ipc(
        data / "ASTER_USDT-5m.feather"
    )
    pl.DataFrame({"date": [datetime(2025, 9, 30, tzinfo=UTC)]}).write_ipc(
        data / "BTC_USDT-5m.feather"
    )

    available = discovery_runtime._pairs_with_nonempty_base_candles(
        data,
        pairs=["ASTER/USDT", "BTC/USDT"],
        config_path=config,
        start_timestamp_ms=int(datetime(2025, 7, 1, tzinfo=UTC).timestamp() * 1000),
        end_timestamp_ms=int(datetime(2025, 10, 1, tzinfo=UTC).timestamp() * 1000),
        startup_candles=0,
    )

    assert available == ["BTC/USDT"]


def test_spot_discovery_drops_pair_consumed_by_startup_window(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    write_json(config, {"timeframe": "5m", "trading_mode": "spot"})
    data = tmp_path / "data"
    data.mkdir()
    for pair, row_count in (("NEW", 2), ("BTC", 3)):
        pl.DataFrame(
            {
                "date": [
                    datetime(2025, 9, 30, 23, 45 + index * 5, tzinfo=UTC)
                    for index in range(row_count)
                ]
            }
        ).write_ipc(data / f"{pair}_USDT-5m.feather")

    available = discovery_runtime._pairs_with_nonempty_base_candles(
        data,
        pairs=["NEW/USDT", "BTC/USDT"],
        config_path=config,
        start_timestamp_ms=int(datetime(2025, 9, 30, tzinfo=UTC).timestamp() * 1000),
        end_timestamp_ms=int(datetime(2025, 10, 1, tzinfo=UTC).timestamp() * 1000),
        startup_candles=2,
    )

    assert available == ["BTC/USDT"]


def test_spot_scout_filters_fully_unlisted_pairs_before_backtest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    write_json(
        config,
        {
            "timeframe": "5m",
            "trading_mode": "spot",
            "exchange": {"pair_whitelist": ["RLUSD/USDT", "BTC/USDT"]},
        },
    )
    market_snapshot = tmp_path / "markets.json"
    write_json(market_snapshot, {})
    seen: dict[str, Any] = {}

    monkeypatch.setattr(
        discovery_runtime,
        "_ensure_universe",
        lambda _context, _shared: (
            config,
            ["RLUSD/USDT", "BTC/USDT"],
            market_snapshot,
        ),
    )

    def prepare_archive(*_args, destination: Path, **_kwargs):
        report = {
            "aggregate_sha256": "a" * 64,
            "strategy_startup_candles": 0,
        }
        write_json(destination, report)
        return report

    monkeypatch.setattr(discovery_runtime, "_prepare_scout_archive", prepare_archive)
    monkeypatch.setattr(
        discovery_runtime,
        "_pairs_with_nonempty_base_candles",
        lambda *_args, **_kwargs: ["BTC/USDT"],
    )

    def run_backtest(*_args, **kwargs):
        seen.update(kwargs)
        return {"complete": False}

    monkeypatch.setattr(discovery_runtime, "_run_scout_backtest", run_backtest)
    context = SimpleNamespace(
        class_name="Demo",
        workers=1,
        output=tmp_path / "result",
        profile_path=tmp_path / "profile.json",
        source=tmp_path / "strategy.py",
        baseline_source=None,
        baseline_upstream_commit=None,
        search_targets=[_target()],
        policy=SimpleNamespace(trading_mode="spot"),
    )

    result = discovery_runtime.run_shard_scout(
        search_shards(date(2026, 7, 30), completed_years=1, shard_months=3)[0],
        context,
    )

    assert result["outcome"] == "unsupported"
    assert seen["pairs"] == ["BTC/USDT"]
    assert seen["output_directory"].name == "engine-available"
    assert read_json(seen["config_path"])["exchange"]["pair_whitelist"] == [
        "BTC/USDT"
    ]


def test_spot_search_universe_accepts_missing_onboarding_dates(
    tmp_path: Path,
) -> None:
    markets = tmp_path / "markets.json"
    write_json(
        markets,
        {
            "exchange": "binance",
            "trading_mode": "spot",
            "pairs": ["BTC/USDT", "ETH/BTC"],
            "markets": {
                "BTC/USDT": {
                    "active": True,
                    "created": None,
                    "quote_volume": 100.0,
                    "spot": True,
                },
                "ETH/BTC": {
                    "active": True,
                    "created": None,
                    "quote_volume": 200.0,
                    "spot": True,
                },
            },
        },
    )

    report = discovery_runtime._discover_spot_search_universe(
        read_json(ROOT / "planning" / "spot-discovery-config.json"),
        market_snapshot_path=markets,
        destination=tmp_path / "universe.json",
    )

    assert report["pairs"] == ["BTC/USDT"]
    assert report["rejected"] == [{"pair": "ETH/BTC", "reason": "PAIR_CONTRACT"}]


def test_spot_search_universe_is_ranked_by_frozen_quote_volume(
    tmp_path: Path,
) -> None:
    markets = tmp_path / "markets.json"
    write_json(
        markets,
        {
            "exchange": "binance",
            "trading_mode": "spot",
            "pairs": ["BTC/USDT", "ETH/USDT", "XRP/USDT"],
            "markets": {
                "BTC/USDT": {"active": True, "spot": True, "quote_volume": 200.0},
                "ETH/USDT": {"active": True, "spot": True, "quote_volume": 300.0},
                "XRP/USDT": {"active": True, "spot": True, "quote_volume": 100.0},
            },
        },
    )

    report = discovery_runtime._discover_spot_search_universe(
        read_json(ROOT / "planning" / "spot-discovery-config.json"),
        market_snapshot_path=markets,
        destination=tmp_path / "universe.json",
    )

    assert report["pairs"] == ["ETH/USDT", "BTC/USDT", "XRP/USDT"]


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
    assert read_json(tmp_path / "infra" / "cursor.json")["next_shard"] == 0
