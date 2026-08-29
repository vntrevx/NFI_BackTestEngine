from __future__ import annotations

import copy
import hashlib
import json
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.errors import TraceError
from nfi_backtest_engine.portfolio_trace import (
    PORTFOLIO_TRACE_VERSION,
    canonical_portfolio_json,
    verify_portfolio_trace,
)

ROOT = Path(__file__).parents[1]
FIXTURE = next((ROOT / "benchmarks/fixtures/captured").glob("*/manifest.json"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _event(
    sequence: int,
    timestamp_ms: int,
    pair: str,
    open_trades: list[dict[str, Any]],
    *,
    batch_index: int,
    batch_position: int,
    configured_pair_index: int,
    accepted: bool = True,
    rejection_reason: str | None = None,
    trade_id: int | None = None,
    order_id: int | None = None,
    release: str = "0",
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "timestamp_ms": timestamp_ms,
        "batch_index": batch_index,
        "batch_position": batch_position,
        "configured_pair_index": configured_pair_index,
        "phase": "open-trade"
        if pair in [trade["pair"] for trade in open_trades]
        else "remaining-pair",
        "pair": pair,
        "open_trade_insertion_order": open_trades,
        "wallet": {
            "before": {"free": "90", "tied": "10", "realized_partial": "0"},
            "after": {"free": "90", "tied": "10", "realized_partial": "0"},
        },
        "slots": {
            "before": {"occupied": len(open_trades), "limit": 3},
            "after": {"occupied": len(open_trades), "limit": 3},
        },
        "decision": {"accepted": accepted, "rejection_reason": rejection_reason},
        "trade_ids": {"next_before": sequence + 1, "allocated": trade_id},
        "order_ids": {"next_before": sequence + 1, "allocated": order_id},
        "partial_exit": {"wallet_release": release, "slot_retained": release != "0"},
        "compounding_base": "100",
    }


def _trace(manifest: Path) -> dict[str, Any]:
    fixture = json.loads(manifest.read_bytes())
    pairs = [f"pair-{index}" for index in range(3)]
    header = {
        "fixture_id": fixture["fixture_id"],
        "fixture_manifest_sha256": _sha(manifest.read_text(encoding="utf-8")),
        "source_sha256": _sha("authenticated-source"),
        "contract_sha256": _sha("portfolio-contract"),
        "configured_pairs": pairs,
        "slot_limit": 3,
    }
    open_trades = [{"trade_id": 1, "pair": pairs[0]}, {"trade_id": 2, "pair": pairs[2]}]
    events = [
        _event(
            0,
            1_000,
            pairs[0],
            [],
            batch_index=0,
            batch_position=0,
            configured_pair_index=0,
            trade_id=1,
            order_id=1,
        ),
        _event(
            1,
            1_000,
            pairs[1],
            [],
            batch_index=0,
            batch_position=1,
            configured_pair_index=1,
            accepted=False,
            rejection_reason="slot-exhausted",
        ),
        _event(
            2,
            1_000,
            pairs[2],
            [],
            batch_index=0,
            batch_position=2,
            configured_pair_index=2,
            trade_id=2,
            order_id=2,
        ),
        _event(
            3,
            2_000,
            pairs[0],
            open_trades,
            batch_index=1,
            batch_position=0,
            configured_pair_index=0,
            order_id=3,
            release="5",
        ),
        _event(
            4,
            2_000,
            pairs[2],
            open_trades,
            batch_index=1,
            batch_position=1,
            configured_pair_index=2,
            order_id=4,
        ),
        _event(
            5,
            2_000,
            pairs[1],
            open_trades,
            batch_index=1,
            batch_position=2,
            configured_pair_index=1,
            trade_id=3,
            order_id=5,
        ),
    ]
    return {
        "schema_version": PORTFOLIO_TRACE_VERSION,
        "header": header,
        "events": events,
        "final_force_exit_trade_ids": [3, 2, 1],
        "final_trades": [
            {"force_exit_sequence": 0, "trade_id": 3, "pair": pairs[1], "order_ids": [5]},
            {"force_exit_sequence": 1, "trade_id": 2, "pair": pairs[2], "order_ids": [2, 4]},
            {"force_exit_sequence": 2, "trade_id": 1, "pair": pairs[0], "order_ids": [1, 3]},
        ],
    }


def _write(path: Path, document: dict[str, Any], *, pretty: bool = False) -> None:
    path.write_text(
        json.dumps(
            document,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        ),
        encoding="utf-8",
    )


def test_portfolio_trace_accepts_equivalent_json_and_reports_raw_and_canonical_hashes(
    tmp_path: Path,
) -> None:
    official = _trace(FIXTURE)
    native = copy.deepcopy(official)
    official_path, native_path = tmp_path / "official.json", tmp_path / "native.json"
    _write(official_path, official, pretty=True)
    _write(native_path, native)

    report = verify_portfolio_trace(FIXTURE, official_path, native_path)

    assert report["exact"] is True
    assert report["mismatch"] is None
    assert report["official_raw_sha256"] != report["native_raw_sha256"]
    assert report["official_canonical_sha256"] == report["native_canonical_sha256"]
    assert canonical_portfolio_json(official) == canonical_portfolio_json(native)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda trace: trace["events"].__setitem__(0, trace["events"][1]),
        lambda trace: trace["events"][3].__setitem__("batch_position", 2),
        lambda trace: trace["events"][3]["open_trade_insertion_order"].reverse(),
        lambda trace: trace["events"][2].__setitem__("pair", trace["events"][0]["pair"]),
        lambda trace: trace["events"][3]["wallet"]["after"].__setitem__("free", "91"),
        lambda trace: trace["events"][1]["slots"]["before"].__setitem__("occupied", 1),
        lambda trace: trace["events"][1]["decision"].__setitem__("rejection_reason", "other"),
        lambda trace: trace["events"][3]["partial_exit"].__setitem__("wallet_release", "4"),
        lambda trace: trace["events"][4].__setitem__("compounding_base", "99"),
        lambda trace: trace["events"][0]["trade_ids"].__setitem__("allocated", 9),
        lambda trace: trace["events"][0]["order_ids"].__setitem__("allocated", 9),
        lambda trace: trace["final_force_exit_trade_ids"].reverse(),
        lambda trace: trace["header"].__setitem__("fixture_manifest_sha256", _sha("wrong")),
        lambda trace: trace["header"].__setitem__("source_sha256", _sha("wrong")),
        lambda trace: trace["header"].__setitem__("contract_sha256", _sha("wrong")),
    ],
)
def test_portfolio_trace_detects_each_substantive_mutation(tmp_path: Path, mutate: Any) -> None:
    official, native = _trace(FIXTURE), _trace(FIXTURE)
    mutate(native)
    official_path, native_path = tmp_path / "official.json", tmp_path / "native.json"
    _write(official_path, official)
    _write(native_path, native)

    report = verify_portfolio_trace(FIXTURE, official_path, native_path)

    assert report["exact"] is False
    assert report["mismatch"]["path"]


def test_all_two_and_three_pair_configured_order_permutations_are_proven(tmp_path: Path) -> None:
    base = _trace(FIXTURE)
    seed_pairs = base["header"]["configured_pairs"]
    for size in (2, 3):
        for permutation in permutations(seed_pairs[:size]):
            trace = copy.deepcopy(base)
            trace["header"]["configured_pairs"] = list(permutation)
            trace["events"] = [
                _event(
                    sequence,
                    1_000,
                    pair,
                    [],
                    batch_index=0,
                    batch_position=sequence,
                    configured_pair_index=sequence,
                )
                for sequence, pair in enumerate(permutation)
            ]
            trace["final_force_exit_trade_ids"] = []
            trace["final_trades"] = []
            official_path = tmp_path / f"official-{size}-{''.join(permutation)}.json"
            native_path = tmp_path / f"native-{size}-{''.join(permutation)}.json"
            _write(official_path, trace)
            _write(native_path, trace)
            assert verify_portfolio_trace(FIXTURE, official_path, native_path)["exact"] is True
            native = copy.deepcopy(trace)
            native["events"][0], native["events"][1] = native["events"][1], native["events"][0]
            _write(native_path, native)
            assert verify_portfolio_trace(FIXTURE, official_path, native_path)["exact"] is False


def test_native_portfolio_event_adapter_is_lossless_and_closed(tmp_path: Path) -> None:
    official, native = _trace(FIXTURE), _trace(FIXTURE)
    native_envelope = {
        "schema_version": "native-portfolio-events-v1",
        "portfolio_header": native["header"],
        "portfolio_events": native["events"],
        "final_force_exit_trade_ids": native["final_force_exit_trade_ids"],
        "final_trades": native["final_trades"],
    }
    official_path, native_path = tmp_path / "official.json", tmp_path / "native.json"
    _write(official_path, official)
    _write(native_path, native_envelope)
    assert verify_portfolio_trace(FIXTURE, official_path, native_path)["exact"] is True
    native_envelope["inferred"] = True
    _write(native_path, native_envelope)
    with pytest.raises(TraceError, match="adapter"):
        verify_portfolio_trace(FIXTURE, official_path, native_path)


def test_trace_verify_portfolio_cli_happy_and_bad(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    official, native = _trace(FIXTURE), _trace(FIXTURE)
    official_path, native_path, output = (
        tmp_path / "official.json",
        tmp_path / "native.json",
        tmp_path / "report.json",
    )
    _write(official_path, official)
    _write(native_path, native)
    arguments = [
        "trace",
        "verify-portfolio",
        str(FIXTURE),
        str(official_path),
        str(native_path),
        "--output",
        str(output),
    ]
    assert cli.main(arguments) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["exact"] is True
    native["events"][0]["trade_ids"]["allocated"] = 99
    _write(native_path, native)
    assert cli.main(arguments) == 1
    assert "portfolio semantic trace: exact=False" in capsys.readouterr().out


def test_portfolio_trace_rejects_duplicate_pair_and_symlink(tmp_path: Path) -> None:
    official = _trace(FIXTURE)
    official["events"][1]["pair"] = official["events"][0]["pair"]
    path = tmp_path / "official.json"
    _write(path, official)
    native = tmp_path / "native.json"
    _write(native, _trace(FIXTURE))
    with pytest.raises(TraceError, match="pair order"):
        verify_portfolio_trace(FIXTURE, path, native)

    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(TraceError, match="regular non-symlink"):
        verify_portfolio_trace(FIXTURE, link, native)
