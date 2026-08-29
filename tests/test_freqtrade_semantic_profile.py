from __future__ import annotations

import json
from pathlib import Path

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.errors import SpecValidationError, TraceError
from nfi_backtest_engine.freqtrade_semantic_profile import (
    build_current_freqtrade_semantic_profile,
    load_freqtrade_semantic_profile,
)
from nfi_backtest_engine.semantic_observer import (
    _validate_observed_event,
    project_official_semantic_trace,
)
from nfi_backtest_engine.specs import (
    FREQTRADE_SEMANTIC_PROFILE_SCHEMA,
    SEMANTIC_OBSERVER_REPORT_SCHEMA,
    validate_schema,
)
from nfi_backtest_engine.state_trace import iter_validated_trace_events, trace_summary

ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "planning" / "freqtrade-semantic-profile.json"
OFFICIAL_FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-tag121-spot-v17.4.435-2023-01-01_02"
    / "manifest.json"
)
OFFICIAL_FUTURES_FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20"
    / "manifest.json"
)


def test_committed_freqtrade_semantic_profile_matches_current_observer() -> None:
    committed = load_freqtrade_semantic_profile(PROFILE)
    generated = build_current_freqtrade_semantic_profile()

    validate_schema(committed, FREQTRADE_SEMANTIC_PROFILE_SCHEMA)
    assert committed == generated
    assert committed["reference"]["version"] == "2026.5.1"
    assert len(committed["observer"]["observed_methods"]) == 15
    assert {item["phase"] for item in committed["observer"]["events"]} == {
        "candle.after",
        "entry.lock_rejected",
        "order.manage",
        "trade.adjustment_check",
        "trade.entry",
        "trade.exit_check",
        "trade.exit_order",
    }


def test_semantic_profile_rejects_digest_tampering(tmp_path: Path) -> None:
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    profile["reference"]["image_platform_digest"] = f"sha256:{'0' * 64}"
    tampered = tmp_path / "profile.json"
    tampered.write_text(json.dumps(profile), encoding="utf-8")

    with pytest.raises(SpecValidationError, match="fingerprint differs"):
        load_freqtrade_semantic_profile(tampered)


def test_official_semantic_observer_projection_is_byte_deterministic(tmp_path: Path) -> None:
    first_trace = tmp_path / "first.trace"
    second_trace = tmp_path / "second.trace"

    first = project_official_semantic_trace(OFFICIAL_FIXTURE, PROFILE, first_trace)
    second = project_official_semantic_trace(OFFICIAL_FIXTURE, PROFILE, second_trace)

    validate_schema(first, SEMANTIC_OBSERVER_REPORT_SCHEMA)
    assert first == second
    assert first_trace.read_bytes() == second_trace.read_bytes()
    assert first["source_trace"]["event_count"] == 1153
    assert first["projected_trace"]["event_count"] == 1153
    assert first["callback_counts"] == [
        {"callback": "adjust_trade_position", "count": 288},
        {"callback": "confirm_trade_entry", "count": 1},
        {"callback": "custom_exit", "count": 288},
    ]
    summary = trace_summary(first_trace)
    assert summary["profile_sha256"] == first["semantic_profile_sha256"]
    assert summary["source"] == "freqtrade-semantic-observer"
    first_event = next(iter_validated_trace_events(first_trace))
    assert set(first_event["state"]) == {
        "balances",
        "portfolio",
        "scheduler",
        "trades",
        "orders",
        "custom_state",
        "funding",
        "liquidation",
        "protections",
    }


def test_official_semantic_observer_rejects_unprofiled_events() -> None:
    with pytest.raises(TraceError, match="unprofiled phase"):
        _validate_observed_event({"candle.after": set()}, "future.phase", None)
    with pytest.raises(TraceError, match="unprofiled callback"):
        _validate_observed_event(
            {"trade.exit_check": {"custom_exit"}},
            "trade.exit_check",
            "future_callback",
        )


def test_official_semantic_observer_projects_futures_state(tmp_path: Path) -> None:
    report = project_official_semantic_trace(
        OFFICIAL_FUTURES_FIXTURE,
        PROFILE,
        tmp_path / "futures.trace",
    )

    assert report["trading_mode"] == "futures"
    assert report["source_trace"]["event_count"] == 5665
    assert report["projected_trace"]["event_count"] == 5665
    assert {item["callback"] for item in report["callback_counts"]} == {
        "adjust_trade_position",
        "confirm_trade_entry",
        "custom_exit",
    }


def test_reference_semantic_commands_parse_explicit_outputs() -> None:
    parser = cli.build_parser()
    profile = parser.parse_args(
        ["reference", "semantic-profile", "--output", "profile.json"]
    )
    observe = parser.parse_args(
        [
            "reference",
            "semantic-observe",
            "manifest.json",
            "--profile",
            "profile.json",
            "--output-trace",
            "semantic.trace",
            "--output-report",
            "semantic.json",
        ]
    )

    assert profile.reference_command == "semantic-profile"
    assert profile.output == Path("profile.json")
    assert observe.reference_command == "semantic-observe"
    assert observe.output_trace == Path("semantic.trace")
    assert observe.output_report == Path("semantic.json")
