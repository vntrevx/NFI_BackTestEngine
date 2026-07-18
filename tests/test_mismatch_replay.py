from __future__ import annotations

from pathlib import Path

from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.mismatch_replay import create_mismatch_replay
from nfi_backtest_engine.parity import ParityDifference


def test_trade_replay_is_bounded_at_first_mismatched_trade(tmp_path: Path) -> None:
    source_input = tmp_path / "source-input.json"
    expected_path = tmp_path / "expected.json"
    actual_path = tmp_path / "actual.json"
    manifest_path = tmp_path / "manifest.json"
    write_json(
        source_input,
        {
            "schema_version": "1.0.0",
            "config": {},
            "pairs": [
                {
                    "pair": "BTC/USDT",
                    "candles": [
                        {"timestamp_ms": 100},
                        {"timestamp_ms": 200},
                        {"timestamp_ms": 300},
                    ],
                }
            ],
        },
    )
    expected = {
        "summary": {"total_trades": 1},
        "trades": [{"close_timestamp_ms": 200, "close_rate": "10"}],
    }
    actual = {
        "summary": {"total_trades": 1},
        "trades": [{"close_timestamp_ms": 200, "close_rate": "11"}],
    }
    write_json(expected_path, expected)
    write_json(actual_path, actual)
    write_json(manifest_path, {"fixture_id": "fixture"})

    report = create_mismatch_replay(
        tmp_path / "replay",
        fixture_id="fixture",
        manifest_path=manifest_path,
        simulation_input_path=source_input,
        expected_surface_path=expected_path,
        actual_surface_path=actual_path,
        trade_difference=ParityDifference(
            "$.trades[0].close_rate",
            "10",
            "11",
            "value differs",
        ),
        state_difference=None,
    )

    replay_input = read_json(tmp_path / "replay" / "simulation-input.json")
    replay_manifest = read_json(tmp_path / "replay" / "replay.json")
    assert report["cutoff_timestamp_ms"] == 200
    assert [item["timestamp_ms"] for item in replay_input["pairs"][0]["candles"]] == [
        100,
        200,
    ]
    assert replay_manifest["prefix"]["candle_count"] == 2
    assert replay_manifest["reproduce"]["command"][-2:] == [
        "--events",
        "engine-events.jsonl",
    ]
