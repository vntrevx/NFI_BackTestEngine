from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.cli import build_parser
from nfi_backtest_engine.errors import BenchmarkError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.reference_runtime import (
    REFERENCE_IMAGE,
    REFERENCE_INDEX_DIGEST,
    REFERENCE_PLATFORM,
    REFERENCE_PLATFORM_DIGEST,
    REFERENCE_VERSION,
)
from nfi_backtest_engine.result_report import write_result_presentation
from nfi_backtest_engine.run_registry import RunRegistry
from nfi_backtest_engine.selected_result import (
    load_selected_run_view,
    write_official_selection,
)
from nfi_backtest_engine.user_flow import finish_official_fallback
from nfi_backtest_engine.verification_ledger import (
    VerificationLedger,
    create_verification_record,
)

ROOT = Path(__file__).parents[1]
SURFACE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "normal-routing-spot-2025-01-01_04"
    / "artifacts"
    / "trade-surface.json"
)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _blocked_run(root: Path) -> tuple[dict, Path, dict]:
    root.mkdir()
    inputs = {
        "strategy": {
            "class_name": "FallbackStrategy",
            "file_sha256": "a" * 64,
        },
        "config": {"run_effective_sha256": "b" * 64},
        "timerange": "20250101-20250104",
    }
    run_id = _canonical_sha256(inputs)
    blocker = {
        "code": "STRATEGY_CALLBACK_NOT_COMPILED",
        "callback": "adjust_trade_position",
        "message": "callback has no exact Rust lowering",
    }
    run = {
        "schema_version": "1.5.0",
        "run_id": run_id,
        "status": "blocked_unsupported_semantics",
        "complete": False,
        "prepared_only": False,
        "created_at": "2026-07-29T00:00:00Z",
        "inputs": inputs,
        "vectors": {"pair_count": 1, "cache_hits": 0},
        "execution": {},
        "timings": {"pipeline_wall_time_seconds": 1.0},
        "resumed_stages": [],
        "capability": {"blockers": [blocker]},
        "result": None,
        "official_confirmation": {"status": "not_run"},
    }
    write_json(root / "run.json", run)
    write_json(root / "identity.json", {"run_id": run_id, "identity": inputs})
    write_json(root / "data-seal.json", {"test": True})

    attempt = root / "official-fallback" / "attempt-0001"
    attempt.mkdir(parents=True)
    surface_path = attempt / "official-trade-surface.json"
    shutil.copyfile(SURFACE, surface_path)
    report = {
        "schema_version": "1.4.0",
        "run_id": run_id,
        "purpose": "fallback",
        "reference": {
            "version": REFERENCE_VERSION,
            "image": REFERENCE_IMAGE,
            "image_index_digest": REFERENCE_INDEX_DIGEST,
            "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
            "platform": REFERENCE_PLATFORM,
            "network": "none",
        },
        "started_at": "2026-07-29T00:00:01Z",
        "ended_at": "2026-07-29T00:00:02Z",
        "wall_time_seconds": 1.0,
        "exit_code": 0,
        "timed_out": False,
        "complete": True,
        "exact_parity": None,
        "difference": None,
        "inputs": {
            "data_seal": {
                "path": str(root / "data-seal.json"),
                "bytes": (root / "data-seal.json").stat().st_size,
                "sha256": sha256_file(root / "data-seal.json"),
            },
        },
        "reference_storage": {"mode": "spooled", "complete": True},
        "official_trade_surface": {
            "path": str(surface_path),
            "bytes": surface_path.stat().st_size,
            "sha256": sha256_file(surface_path),
        },
    }
    report_path = attempt / "run.json"
    write_json(report_path, report)
    return run, report_path, report


def _fingerprint() -> dict[str, object]:
    return {
        "upstream_repository": None,
        "upstream_commit": None,
        "strategy_version": None,
        "strategy_source_sha256": "a" * 64,
        "strategy_ir_sha256": None,
        "hot_callback_ir_sha256": None,
        "config_sha256": "b" * 64,
        "pairlist_sha256": None,
        "data_seal_sha256": "c" * 64,
        "market_snapshot_sha256": None,
        "timerange": "20250101-20250104",
        "mode_contract": "spot",
        "reference_version": "2026.5.1",
        "reference_image_index_digest": None,
        "reference_image_platform_digest": None,
        "reference_platform": None,
        "package_sha256": None,
        "wheel_sha256": None,
        "native_binary_sha256": None,
    }


def test_selected_official_result_preserves_native_evidence_and_labels_lane(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    run, report_path, report = _blocked_run(root)
    source_hash = sha256_file(root / "run.json")

    selection = write_official_selection(root, report_path)
    summary = write_result_presentation(
        root,
        verification=report,
        verification_path=report_path,
    )

    assert sha256_file(root / "run.json") == source_hash
    assert read_json(root / "run.json") == run
    assert selection["native_status"] == "blocked_unsupported_semantics"
    assert selection["selected_status"] == "official_complete"
    assert selection["selected_lane"] == "official"
    assert selection["exact_parity"] is None
    assert summary["run"]["status"] == "official_complete"
    assert summary["run"]["native_status"] == "blocked_unsupported_semantics"
    assert summary["run"]["execution_lane"] == "official"
    assert summary["verification"]["status"] == "official_only"
    assert summary["verification"]["exact"] is None
    roles = {entry["role"] for entry in read_json(root / "evidence/index.json")["entries"]}
    assert {
        "run",
        "selected_result",
        "official_fallback_report",
        "official_trade_surface",
    } <= roles


def test_selected_result_rejects_tampered_official_surface(tmp_path: Path) -> None:
    root = tmp_path / "run"
    run, report_path, _ = _blocked_run(root)
    write_official_selection(root, report_path)
    (report_path.parent / "official-trade-surface.json").write_text(
        "{}\n",
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkError, match="hash binding"):
        load_selected_run_view(root, run)


def test_noninteractive_ask_does_not_imply_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "run"
    _blocked_run(root)
    calls = []
    messages = []
    monkeypatch.setattr(
        "nfi_backtest_engine.user_flow.record_native_blocker",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        "nfi_backtest_engine.user_flow.run_official_fallback",
        lambda *_args, **_kwargs: calls.append(True),
    )

    status = finish_official_fallback(
        root,
        ledger_path=tmp_path / "ledger.sqlite",
        native_status=1,
        fallback_policy="ask",
        timeout_seconds=None,
        interactive=False,
        emit=messages.append,
    )

    assert status == 1
    assert calls == []
    assert any(
        "Native execution stopped safely: STRATEGY_CALLBACK_NOT_COMPILED"
        in message
        for message in messages
    )
    assert any(
        "use --fallback official to run non-interactively" in message
        for message in messages
    )


def test_explicit_official_fallback_announces_transition_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "run"
    _blocked_run(root)
    messages = []
    monkeypatch.setattr(
        "nfi_backtest_engine.user_flow.record_native_blocker",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        "nfi_backtest_engine.user_flow.run_official_fallback",
        lambda *_args, **_kwargs: (
            {
                "complete": False,
                "timed_out": False,
                "exit_code": 2,
            },
            tmp_path / "attempt" / "run.json",
            False,
        ),
    )

    status = finish_official_fallback(
        root,
        ledger_path=tmp_path / "ledger.sqlite",
        native_status=1,
        fallback_policy="official",
        timeout_seconds=None,
        interactive=False,
        emit=messages.append,
    )

    assert status == 1
    transition_index = next(
        index
        for index, message in enumerate(messages)
        if message.startswith("official fallback: approved")
    )
    failure_index = next(
        index
        for index, message in enumerate(messages)
        if message.startswith("official fallback: failed")
    )
    assert transition_index < failure_index
    assert "may take much longer than Native" in messages[transition_index]
    assert "Native run remains unchanged" in messages[transition_index]
    assert "does not claim parity" in messages[transition_index]


def test_cli_fallback_policy_is_explicit_and_yes_does_not_change_it() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "--yes"])
    assert args.fallback == "ask"
    explicit = parser.parse_args(["run", "--yes", "--fallback", "official"])
    assert explicit.fallback == "official"


def test_registry_separates_native_and_selected_status(tmp_path: Path) -> None:
    root = tmp_path / "run"
    run, report_path, _ = _blocked_run(root)
    registry_path = tmp_path / "runs.sqlite"
    with RunRegistry(registry_path) as registry:
        registry.record(run, root)
        write_official_selection(root, report_path)
        registry.record_selection(root)
        row = registry.list()[0]

    assert row["status"] == "official_complete"
    assert row["native_status"] == "blocked_unsupported_semantics"
    assert row["selected_status"] == "official_complete"
    assert row["selected_lane"] == "official"
    assert row["trade_count"] is None
    assert row["official_trade_count"] > 0


def test_ledger_projects_official_success_separately(tmp_path: Path) -> None:
    record = create_verification_record(
        subject_kind="run",
        subject_id="run-1",
        state="official_complete",
        outcome="success",
        fingerprint=_fingerprint(),
    )
    with VerificationLedger(tmp_path / "ledger.sqlite") as ledger:
        ledger.append(record)
        run = ledger.project()["runs"][0]

    assert run["official_complete"]["state"] == "official_complete"
    assert run["highest_success"] is None
