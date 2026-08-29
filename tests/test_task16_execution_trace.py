from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.errors import TraceError
from nfi_backtest_engine.execution_trace import (
    EXECUTION_TRACE_VERSION,
    canonical_execution_json,
    verify_execution_trace,
)
from nfi_backtest_engine.fixture import fixture_input_sha256

ROOT = Path(__file__).parents[1]
FIXTURE = next((ROOT / "benchmarks/fixtures/captured").glob("*/manifest.json"))
CONTRACT = ROOT / "python/nfi_backtest_engine/contracts/freqtrade-execution-contract.json"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _trace(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_bytes())
    strategy = next(item for item in manifest["inputs"] if item["role"] == "strategy")
    artifact_path, artifact = next(iter(manifest["artifacts"].items()))
    return {
        "schema_version": EXECUTION_TRACE_VERSION,
        "header": {
            "fixture_id": manifest["fixture_id"],
            "fixture_manifest_sha256": _sha(manifest_path.read_bytes()),
            "source": {"path": strategy["path"], "sha256": strategy["sha256"]},
            "contract": {
                "path": "contracts/freqtrade-execution-contract.json",
                "sha256": _sha(CONTRACT.read_bytes()),
            },
            "input": {"path": "fixture-inputs", "sha256": fixture_input_sha256(manifest["inputs"])},
            "binary": {"path": "nfi-sim", "sha256": _sha(b"native-binary")},
            "artifact": {"path": artifact["path"], "sha256": artifact["sha256"]},
        },
        "events": [
            _event(0, "entry", "open"),
            _event(1, "adjustment", "open"),
            _event(2, "exit", "close"),
        ],
    }


def _event(sequence: int, boundary: str, fill_boundary: str) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "timestamp_ms": 1_000 + sequence,
        "pair": "any/pair",
        "direction": "long",
        "phase": f"{boundary}.fill",
        "boundary": boundary,
        "candidates": {
            "attempts": [
                {"candidate": "signal", "confirmation": "rejected", "fallthrough": True},
                {"candidate": "stoploss", "confirmation": "accepted", "fallthrough": False},
            ],
            "outcome": "selected",
            "winner": "stoploss",
        },
        "candle": {"open": "10", "high": "12", "low": "9", "close": "11", "ambiguity": "none"},
        "fill": {"boundary": fill_boundary, "order_type": "market", "rate": "10"},
        "order_lifecycle": {
            "mode": "market",
            "requested_limit_rate": None,
            "adjusted_limit_rate": None,
            "candle_crossed": True,
            "fill_predicate": "candle-open",
            "timeout": False,
            "unfilled": False,
            "retry": False,
        },
        "precision": {
            "amount_input": "3",
            "amount_step": "0.1",
            "amount_frozen_step": "0.1",
            "amount_round_direction": "floor",
            "amount_rounded": "3",
            "price_input": "10",
            "price_step": "0.01",
            "price_frozen_step": "0.01",
            "price_round_direction": "ties-even",
            "price_rounded": "10",
        },
        "min_stake": {"stage": "before-fill", "result": "1"},
        "fees": {"open_rate": "0.001", "close_rate": "0.002", "per_fill": ["0.003"]},
        "intermediates": {"stake": "30", "basis": "30.03", "profit": "0"},
        "partial_exit_amount": "0" if boundary != "adjustment" else "1",
        "trade_id": 1,
        "order_id": sequence + 1,
        "rejection_reason": None,
        "state": {
            "before": {"wallet": {"free": "100"}, "trade": {"amount": "0"}, "order": {"count": 0}},
            "after": {"wallet": {"free": "70"}, "trade": {"amount": "3"}, "order": {"count": 1}},
        },
    }


def _write(path: Path, document: dict[str, Any], *, pretty: bool = False) -> None:
    path.write_text(
        json.dumps(
            document, indent=2 if pretty else None, separators=None if pretty else (",", ":")
        ),
        encoding="utf-8",
    )


def test_execution_trace_accepts_canonical_equivalent_json_and_retains_hashes(
    tmp_path: Path,
) -> None:
    official, native = _trace(FIXTURE), _trace(FIXTURE)
    official_path, native_path = tmp_path / "official.json", tmp_path / "native.json"
    _write(official_path, official, pretty=True)
    _write(native_path, native)
    report = verify_execution_trace(FIXTURE, official_path, native_path)
    assert report["exact"] is True
    assert report["official_raw_sha256"] != report["native_raw_sha256"]
    assert report["official_canonical_sha256"] == report["native_canonical_sha256"]
    assert canonical_execution_json(official) == canonical_execution_json(native)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t["events"].__setitem__(0, t["events"][1]),
        lambda t: t["events"][0].__setitem__("phase", "exit.fill"),
        lambda t: t["events"][0]["candidates"]["attempts"].reverse(),
        lambda t: t["events"][0]["candidates"]["attempts"][0].__setitem__(
            "confirmation", "accepted"
        ),
        lambda t: t["events"][0]["candidates"].__setitem__("winner", "signal"),
        lambda t: t["events"][0]["candidates"]["attempts"][0].__setitem__("fallthrough", False),
        lambda t: t["events"][0]["candidates"].__setitem__("outcome", "all-rejected"),
        lambda t: t["events"][0]["candle"].__setitem__("ambiguity", "high-first"),
        lambda t: t["events"][0]["fill"].__setitem__("boundary", "close"),
        lambda t: t["events"][0]["fill"].__setitem__("order_type", "limit"),
        lambda t: t["events"][0]["precision"].__setitem__("amount_round_direction", "ceil"),
        lambda t: t["events"][0]["precision"].__setitem__("price_frozen_step", "0.02"),
        lambda t: t["events"][0]["min_stake"].__setitem__("stage", "after-fill"),
        lambda t: t["events"][0]["fees"].__setitem__("per_fill", ["0.004", "0.003"]),
        lambda t: t["events"][1].__setitem__("partial_exit_amount", "2"),
        lambda t: t["events"][0]["order_lifecycle"].__setitem__("requested_limit_rate", "10"),
        lambda t: t["events"][0]["order_lifecycle"].__setitem__("adjusted_limit_rate", "10"),
        lambda t: t["events"][0]["order_lifecycle"].__setitem__("candle_crossed", False),
        lambda t: t["events"][0]["order_lifecycle"].__setitem__("fill_predicate", "limit-cross"),
        lambda t: t["events"][0]["order_lifecycle"].__setitem__("timeout", True),
        lambda t: t["events"][0]["order_lifecycle"].__setitem__("unfilled", True),
        lambda t: t["events"][0]["order_lifecycle"].__setitem__("retry", True),
        lambda t: t["events"][0].__setitem__("trade_id", 2),
        lambda t: t["events"][0].__setitem__("order_id", 99),
        lambda t: t["events"][0].__setitem__("rejection_reason", "rejected"),
        lambda t: t["events"][0]["state"]["after"]["wallet"].__setitem__("free", "71"),
        lambda t: t["header"]["source"].__setitem__("sha256", "0" * 64),
        lambda t: t["header"]["contract"].__setitem__("sha256", "0" * 64),
        lambda t: t["header"]["input"].__setitem__("sha256", "0" * 64),
        lambda t: t["header"]["binary"].__setitem__("path", "other-binary"),
        lambda t: t["header"]["artifact"].__setitem__("path", "other-artifact"),
    ],
)
def test_execution_trace_detects_every_boundary_mutation(tmp_path: Path, mutate: Any) -> None:
    official, native = _trace(FIXTURE), _trace(FIXTURE)
    mutate(native)
    official_path, native_path = tmp_path / "official.json", tmp_path / "native.json"
    _write(official_path, official)
    _write(native_path, native)
    report = verify_execution_trace(FIXTURE, official_path, native_path)
    assert report["exact"] is False
    assert report["mismatch"]["path"]


def test_execution_trace_rejects_unrepresentable_numeric_and_bad_identity(tmp_path: Path) -> None:
    official, native = _trace(FIXTURE), _trace(FIXTURE)
    official["events"][0]["precision"]["amount_input"] = "1e-3"
    official_path, native_path = tmp_path / "official.json", tmp_path / "native.json"
    _write(official_path, official)
    _write(native_path, native)
    with pytest.raises(TraceError, match="schema|decimal"):
        verify_execution_trace(FIXTURE, official_path, native_path)
    official = _trace(FIXTURE)
    official["header"]["source"]["sha256"] = "0" * 64
    _write(official_path, official)
    with pytest.raises(TraceError, match="source identity"):
        verify_execution_trace(FIXTURE, official_path, native_path)


def test_native_execution_adapter_is_lossless_closed_and_cli_has_happy_bad_help(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    official, native = _trace(FIXTURE), _trace(FIXTURE)
    envelope = {
        "schema_version": "native-execution-events-v1",
        "execution_header": native["header"],
        "execution_events": native["events"],
    }
    official_path, native_path, output = (
        tmp_path / "official.json",
        tmp_path / "native.json",
        tmp_path / "report.json",
    )
    _write(official_path, official)
    _write(native_path, envelope)
    arguments = [
        "trace",
        "verify-execution",
        str(FIXTURE),
        str(official_path),
        str(native_path),
        "--output",
        str(output),
    ]
    assert cli.main(arguments) == 0
    assert json.loads(output.read_text())["exact"] is True
    envelope["execution_events"][0]["order_id"] = 99
    _write(native_path, envelope)
    assert cli.main(arguments) == 1
    assert "execution semantic trace: exact=False" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="0"):
        cli.main(["trace", "verify-execution", "--help"])
    envelope["inferred"] = True
    _write(native_path, envelope)
    with pytest.raises(TraceError, match="adapter"):
        verify_execution_trace(FIXTURE, official_path, native_path)


def test_candidate_attempts_allow_rejection_fallthrough_without_no_fallthrough_rule(
    tmp_path: Path,
) -> None:
    official, native = _trace(FIXTURE), _trace(FIXTURE)
    official["events"][0]["candidates"]["attempts"][1]["fallthrough"] = True
    native = json.loads(json.dumps(official))
    official_path, native_path = tmp_path / "official.json", tmp_path / "native.json"
    _write(official_path, official)
    _write(native_path, native)
    assert verify_execution_trace(FIXTURE, official_path, native_path)["exact"] is True


def test_limit_lifecycle_and_all_rejected_candidates_map_directly_from_native(
    tmp_path: Path,
) -> None:
    official, native = _trace(FIXTURE), _trace(FIXTURE)
    event = official["events"][2]
    event["candidates"] = {
        "attempts": [
            {"candidate": "signal", "confirmation": "rejected", "fallthrough": True},
            {"candidate": "stoploss", "confirmation": "rejected", "fallthrough": False},
        ],
        "outcome": "all-rejected",
        "winner": None,
    }
    event["fill"] = {"boundary": "close", "order_type": "limit", "rate": None}
    event["order_lifecycle"] = {
        "mode": "limit",
        "requested_limit_rate": "10",
        "adjusted_limit_rate": "10.01",
        "candle_crossed": False,
        "fill_predicate": "low-lte-limit",
        "timeout": True,
        "unfilled": True,
        "retry": True,
    }
    native = json.loads(json.dumps(official))
    native_path, official_path = tmp_path / "native.json", tmp_path / "official.json"
    _write(official_path, official)
    _write(
        native_path,
        {
            "schema_version": "native-execution-events-v1",
            "execution_header": native["header"],
            "execution_events": native["events"],
        },
    )
    assert verify_execution_trace(FIXTURE, official_path, native_path)["exact"] is True
    native_event = json.loads(native_path.read_text())
    native_event["execution_events"][2]["order_lifecycle"]["retry"] = False
    _write(native_path, native_event)
    assert verify_execution_trace(FIXTURE, official_path, native_path)["exact"] is False


def test_execution_trace_rejects_symlink(tmp_path: Path) -> None:
    official, native = _trace(FIXTURE), _trace(FIXTURE)
    real, link, native_path = (
        tmp_path / "official.json",
        tmp_path / "link.json",
        tmp_path / "native.json",
    )
    _write(real, official)
    _write(native_path, native)
    link.symlink_to(real)
    with pytest.raises(TraceError, match="regular non-symlink"):
        verify_execution_trace(FIXTURE, link, native_path)
