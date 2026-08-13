from __future__ import annotations

from pathlib import Path
from typing import Any

from nfi_backtest_engine import full_native_calibration
from nfi_backtest_engine.canonical import read_json, write_json


def _manifest(tmp_path: Path) -> Path:
    write_json(
        tmp_path / "indicator.json",
        {
            "nodes": [
                {
                    "op": "frame-source",
                    "parameters": {
                        "pair": {"kind": "literal", "value": "BTC/USDT"},
                        "timeframe": "4h",
                    },
                }
            ]
        },
    )
    manifest = tmp_path / "manifest.json"
    write_json(
        manifest,
        {
            "schema_version": "full-native-vector-manifest-v1",
            "programs": {
                "indicator": {"artifact": {"path": "indicator.json"}},
            },
            "pairs": [
                {"identity": {"pair": "SMALL/USDT", "timeframe": "5m"}},
                {"identity": {"pair": "LARGE/USDT", "timeframe": "5m"}},
            ],
            "frames": [
                {
                    "identity": {"pair": "SMALL/USDT", "timeframe": "5m"},
                    "rows": 10,
                },
                {
                    "identity": {"pair": "LARGE/USDT", "timeframe": "5m"},
                    "rows": 20,
                },
                {
                    "identity": {"pair": "LARGE/USDT", "timeframe": "1h"},
                    "rows": 2,
                },
                {
                    "identity": {"pair": "BTC/USDT", "timeframe": "4h"},
                    "rows": 1,
                },
            ],
            "futures": None,
        },
    )
    return manifest


def test_probe_selects_largest_pair_and_literal_frames(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    probe_path, pair = full_native_calibration._probe_manifest(manifest)
    try:
        probe = read_json(probe_path)
        assert pair == "LARGE/USDT"
        assert [item["identity"]["pair"] for item in probe["pairs"]] == [pair]
        assert {
            (item["identity"]["pair"], item["identity"]["timeframe"])
            for item in probe["frames"]
        } == {
            ("LARGE/USDT", "5m"),
            ("LARGE/USDT", "1h"),
            ("BTC/USDT", "4h"),
        }
    finally:
        probe_path.unlink(missing_ok=True)


def test_measured_calibration_is_reused_by_content_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        full_native_calibration,
        "build_engine",
        lambda: {"source_fingerprint": "a" * 64},
    )

    def fake_run_engine(*args, **kwargs) -> dict[str, Any]:
        calls.append(kwargs)
        return {"peak_rss_bytes": 1024, "wall_time_seconds": 0.25}

    arguments = {
        "profile_path": tmp_path / "profile.json",
        "hardware_fingerprint": "hardware",
        "requested_workers": 3,
        "memory_cap_bytes": None,
        "calibration_directory": tmp_path / "calibrations",
        "run_engine_fn": fake_run_engine,
    }
    created = full_native_calibration.resolve_full_native_pair_workers(
        manifest,
        **arguments,
    )
    reused = full_native_calibration.resolve_full_native_pair_workers(
        manifest,
        **arguments,
    )

    assert created["reused"] is False
    assert reused["reused"] is True
    assert created["key"] == reused["key"]
    assert created["probe_pair"] == "LARGE/USDT"
    assert created["worker_limit"] == 3
    assert len(calls) == 1
    assert calls[0]["pair_worker_limit"] == 1
