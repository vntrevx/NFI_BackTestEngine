from __future__ import annotations

import json
from pathlib import Path

from nfi_backtest_engine import cli
from nfi_backtest_engine.freqtrade_semantic_profile import (
    load_freqtrade_semantic_profile,
)
from nfi_backtest_engine.scheduler_contract import (
    build_scheduler_contract,
    load_scheduler_contract,
    validate_native_scheduler_contract,
)
from nfi_backtest_engine.scheduler_verification import verify_scheduler_events
from nfi_backtest_engine.semantic_observer import project_official_semantic_trace
from nfi_backtest_engine.specs import (
    SCHEDULER_CONTRACT_SCHEMA,
    SCHEDULER_VERIFICATION_SCHEMA,
    validate_schema,
)
from nfi_backtest_engine.state_trace import StateTraceWriter, iter_validated_trace_events
from nfi_backtest_engine.vector_runtime import _restore_configured_pair_order

ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "planning" / "freqtrade-semantic-profile.json"
CONTRACT = ROOT / "planning" / "freqtrade-scheduler-contract.json"
RUST_CONTRACT = (
    ROOT
    / "rust"
    / "crates"
    / "nfi-sim-core"
    / "src"
    / "scheduler_contract.json"
)
FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-tag121-spot-v17.4.435-2023-01-01_02"
    / "manifest.json"
)


def test_scheduler_contract_matches_semantic_profile_and_embedded_rust() -> None:
    profile = load_freqtrade_semantic_profile(PROFILE)
    contract = load_scheduler_contract(CONTRACT, semantic_profile_path=PROFILE)
    generated = build_scheduler_contract(PROFILE)
    native_json = RUST_CONTRACT.read_text(encoding="utf-8")

    validate_schema(contract, SCHEDULER_CONTRACT_SCHEMA)
    validate_native_scheduler_contract(contract, native_json)
    assert contract == generated
    assert contract["semantic_profile_sha256"] == profile["fingerprint"]
    assert contract["chronology"]["wallet_mutation"] == "serial-global-event-loop"
    assert contract["visibility"]["signal_source_row_shift"] == 1
    assert contract["visibility"]["callback_feature_row_offset"] == -1


def test_installed_native_extension_exposes_the_same_scheduler_contract() -> None:
    from nfi_backtest_engine import _rust

    contract = load_scheduler_contract(CONTRACT, semantic_profile_path=PROFILE)

    validate_native_scheduler_contract(contract, _rust.scheduler_contract_json())


def test_parallel_vector_completion_restores_configured_pair_order() -> None:
    records = [
        {"pair": "CCC/USDT", "value": 3},
        {"pair": "AAA/USDT", "value": 1},
        {"pair": "BBB/USDT", "value": 2},
    ]

    _restore_configured_pair_order(
        records,
        ["AAA/USDT", "BBB/USDT", "CCC/USDT"],
    )

    assert [record["pair"] for record in records] == [
        "AAA/USDT",
        "BBB/USDT",
        "CCC/USDT",
    ]


def test_scheduler_event_verification_uses_official_pair_order(tmp_path: Path) -> None:
    official = tmp_path / "official.trace"
    project_official_semantic_trace(FIXTURE, PROFILE, official)
    native = tmp_path / "native.jsonl"
    with native.open("w", encoding="utf-8", newline="\n") as handle:
        for event in iter_validated_trace_events(official):
            if event["phase"] != "candle.after":
                continue
            handle.write(
                json.dumps(
                    {
                        "timestamp_ms": event["timestamp_ms"],
                        "pair": event["pair"],
                        "state": {},
                    },
                    separators=(",", ":"),
                )
            )
            handle.write("\n")

    report = verify_scheduler_events(FIXTURE, official, native, CONTRACT)

    validate_schema(report, SCHEDULER_VERIFICATION_SCHEMA)
    assert report["event_order_exact"] is True
    assert report["event_count"] == 288
    assert report["timestamp_batch_count"] == 288
    assert report["same_timestamp_batch_count"] == 0
    assert report["mismatch"] is None


def test_scheduler_event_verification_counts_same_timestamp_pair_batches(
    tmp_path: Path,
) -> None:
    profile = load_freqtrade_semantic_profile(PROFILE)
    official = tmp_path / "official.trace"
    with StateTraceWriter(
        official,
        source="freqtrade-semantic-observer",
        run_id="two-pair-scheduler",
        input_sha256="0" * 64,
        strategy_sha256="1" * 64,
        profile_sha256=profile["fingerprint"],
        trading_mode="spot",
        include_state=True,
    ) as writer:
        for timestamp_ms in (1_000, 2_000):
            for pair in ("BTC/USDT", "TRB/USDT"):
                writer.append(
                    timestamp_ms=timestamp_ms,
                    phase="candle.after",
                    pair=pair,
                    state={},
                )

    native = tmp_path / "native.jsonl"
    with native.open("w", encoding="utf-8", newline="\n") as handle:
        for timestamp_ms in (1_000, 2_000):
            for pair in ("BTC/USDT", "TRB/USDT"):
                handle.write(
                    json.dumps(
                        {"timestamp_ms": timestamp_ms, "pair": pair},
                        separators=(",", ":"),
                    )
                )
                handle.write("\n")

    report = verify_scheduler_events(FIXTURE, official, native, CONTRACT)

    assert report["event_order_exact"] is True
    assert report["event_count"] == 4
    assert report["timestamp_batch_count"] == 2
    assert report["same_timestamp_batch_count"] == 2


def test_scheduler_commands_parse_explicit_contracts_and_outputs() -> None:
    parser = cli.build_parser()
    contract = parser.parse_args(
        [
            "reference",
            "scheduler-contract",
            "--semantic-profile",
            "semantic-profile.json",
            "--output",
            "scheduler.json",
        ]
    )
    verification = parser.parse_args(
        [
            "trace",
            "verify-schedule",
            "manifest.json",
            "official.trace",
            "native.jsonl",
            "--contract",
            "scheduler.json",
            "--output",
            "verification.json",
        ]
    )

    assert contract.reference_command == "scheduler-contract"
    assert contract.semantic_profile == Path("semantic-profile.json")
    assert verification.trace_command == "verify-schedule"
    assert verification.output == Path("verification.json")
