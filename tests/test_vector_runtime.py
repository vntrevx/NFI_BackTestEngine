from __future__ import annotations

import json
from pathlib import Path

from nfi_backtest_engine import vector_runtime


def test_calibration_identity_contains_content_not_host_paths(tmp_path: Path) -> None:
    frame = tmp_path / "candles.feather"
    funding = tmp_path / "funding.feather"
    mark = tmp_path / "mark.feather"
    for path, contents in (
        (frame, b"frame"),
        (funding, b"funding"),
        (mark, b"mark"),
    ):
        path.write_bytes(contents)
    request = {
        "pair": "BTC/USDT:USDT",
        "frames": {"BTC/USDT:USDT|5m": str(frame)},
        "frame_sha256": {"BTC/USDT:USDT|5m": "1" * 64},
        "funding_data": {
            "funding_rate_path": str(funding),
            "funding_rate_sha256": "2" * 64,
            "mark_path": str(mark),
            "mark_sha256": "3" * 64,
        },
    }

    identity = vector_runtime._calibration_identity(
        [request],
        strategy_sha="4" * 64,
        config_sha="5" * 64,
        timerange="20200101-20250101",
        runtime_versions={"python": "fixture"},
    )
    encoded = json.dumps(identity, sort_keys=True)

    assert str(tmp_path) not in encoded
    assert identity["pairs"][0]["funding_data"] == {
        "funding_rate_sha256": "2" * 64,
        "mark_sha256": "3" * 64,
    }
    assert identity["pairs"][0]["input_bytes"] == 16
