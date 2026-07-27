from __future__ import annotations

import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError
from nfi_backtest_engine.run_registry import RunRegistry
from nfi_backtest_engine.verification_ledger import (
    VerificationLedger,
    create_verification_record,
    format_verification_projection,
    write_verification_projection,
)


def _fingerprint(strategy_sha: str = "a" * 64, *, data_sha: str = "b" * 64) -> dict:
    return {
        "upstream_repository": "https://github.com/iterativv/NostalgiaForInfinity.git",
        "upstream_commit": "c" * 40,
        "strategy_version": "17.4.435",
        "strategy_source_sha256": strategy_sha,
        "strategy_ir_sha256": "d" * 64,
        "hot_callback_ir_sha256": "e" * 64,
        "config_sha256": "f" * 64,
        "pairlist_sha256": "1" * 64,
        "data_seal_sha256": data_sha,
        "market_snapshot_sha256": "2" * 64,
        "timerange": "20210101-20260101",
        "mode_contract": "binance-spot",
        "reference_version": "2026.5.1",
        "reference_image_index_digest": "sha256:index",
        "reference_image_platform_digest": "sha256:platform",
        "reference_platform": "linux/amd64",
        "package_sha256": "3" * 64,
        "wheel_sha256": "4" * 64,
        "native_binary_sha256": "5" * 64,
    }


def _record(
    *,
    state: str,
    recorded_at: str,
    subject_kind: str = "strategy_revision",
    subject_id: str = "a" * 64,
    fingerprint: dict | None = None,
    outcome: str = "success",
    failure: dict | None = None,
) -> dict:
    strategy_sha = subject_id if subject_kind == "strategy_revision" else "a" * 64
    return create_verification_record(
        subject_kind=subject_kind,
        subject_id=subject_id,
        state=state,
        outcome=outcome,
        fingerprint=fingerprint or _fingerprint(strategy_sha),
        evidence=[
            {
                "kind": "report",
                "location": "artifacts/report.json",
                "bytes": 10,
                "sha256": "6" * 64,
            }
        ],
        failure=failure,
        recorded_at=recorded_at,
    )


def test_strategy_states_project_independently(tmp_path: Path) -> None:
    path = tmp_path / "verification.sqlite"
    with VerificationLedger(path) as ledger:
        ledger.append(_record(state="latest_checked", recorded_at="2026-07-28T00:00:00Z"))
        ledger.append(_record(state="quick_verified", recorded_at="2026-07-28T00:01:00Z"))
        ledger.append(_record(state="release_certified", recorded_at="2026-07-28T00:02:00Z"))
        projection = ledger.project()

    assert projection["record_count"] == 3
    assert projection["strategy"]["latest_checked"]["state"] == "release_certified"
    assert projection["strategy"]["quick_verified"]["state"] == "release_certified"
    assert projection["strategy"]["release_certified"]["state"] == "release_certified"


def test_failed_latest_check_preserves_previous_successes(tmp_path: Path) -> None:
    certified_sha = "a" * 64
    checked_sha = "9" * 64
    with VerificationLedger(tmp_path / "verification.sqlite") as ledger:
        ledger.append(
            _record(
                state="release_certified",
                recorded_at="2026-07-28T00:00:00Z",
                subject_id=certified_sha,
            )
        )
        ledger.append(
            _record(
                state="latest_checked",
                recorded_at="2026-07-28T00:01:00Z",
                subject_id=checked_sha,
                fingerprint=_fingerprint(checked_sha),
                outcome="failure",
                failure={
                    "code": "EXACT_LOWERING_REVIEW_REQUIRED",
                    "message": "new callback shape",
                },
            )
        )
        projection = ledger.project()

    assert projection["strategy"]["latest_checked"]["subject"]["id"] == checked_sha
    assert projection["strategy"]["latest_checked"]["outcome"] == "failure"
    assert projection["strategy"]["quick_verified"]["subject"]["id"] == certified_sha
    assert projection["strategy"]["release_certified"]["subject"]["id"] == certified_sha


def test_success_cannot_regress_for_the_same_fingerprint(tmp_path: Path) -> None:
    with VerificationLedger(tmp_path / "verification.sqlite") as ledger:
        ledger.append(
            _record(
                state="native_complete",
                recorded_at="2026-07-28T00:00:00Z",
                subject_kind="run",
                subject_id="run-1",
            )
        )
        with pytest.raises(BenchmarkError, match="cannot regress"):
            ledger.append(
                _record(
                    state="prepared",
                    recorded_at="2026-07-28T00:01:00Z",
                    subject_kind="run",
                    subject_id="run-1",
                )
            )


def test_new_fingerprint_starts_a_new_run_state_chain(tmp_path: Path) -> None:
    with VerificationLedger(tmp_path / "verification.sqlite") as ledger:
        ledger.append(
            _record(
                state="release_certified",
                recorded_at="2026-07-28T00:00:00Z",
                subject_kind="run",
                subject_id="run-1",
            )
        )
        sequence = ledger.append(
            _record(
                state="prepared",
                recorded_at="2026-07-28T00:01:00Z",
                subject_kind="run",
                subject_id="run-1",
                fingerprint=_fingerprint(data_sha="7" * 64),
            )
        )

    assert sequence == 2


def test_database_triggers_reject_update_and_delete(tmp_path: Path) -> None:
    with VerificationLedger(tmp_path / "verification.sqlite") as ledger:
        ledger.append(_record(state="latest_checked", recorded_at="2026-07-28T00:00:00Z"))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ledger.connection.execute(
                "UPDATE verification_records SET state = 'failed' WHERE sequence = 1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ledger.connection.execute("DELETE FROM verification_records WHERE sequence = 1")


def test_record_hashes_and_release_fingerprint_are_fail_closed(tmp_path: Path) -> None:
    record = _record(state="latest_checked", recorded_at="2026-07-28T00:00:00Z")
    changed = deepcopy(record)
    changed["fingerprint"]["config_sha256"] = "8" * 64
    with (
        pytest.raises(SpecValidationError, match="fingerprint_sha256"),
        VerificationLedger(tmp_path / "verification.sqlite") as ledger,
    ):
        ledger.append(changed)

    incomplete = _fingerprint()
    incomplete["wheel_sha256"] = None
    with pytest.raises(SpecValidationError, match="complete fingerprint"):
        _record(
            state="release_certified",
            recorded_at="2026-07-28T00:01:00Z",
            fingerprint=incomplete,
        )


def test_cli_and_html_keep_latest_quick_and_release_labels_distinct(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "verification.sqlite"
    checked_sha = "9" * 64
    with VerificationLedger(ledger_path) as ledger:
        ledger.append(
            _record(
                state="release_certified",
                recorded_at="2026-07-28T00:00:00Z",
            )
        )
        ledger.append(
            _record(
                state="latest_checked",
                recorded_at="2026-07-28T00:01:00Z",
                subject_id=checked_sha,
                fingerprint=_fingerprint(checked_sha),
                outcome="failure",
                failure={"code": "PYTHON_SYNTAX", "message": "invalid source"},
            )
        )
        projection = ledger.project()

    snapshot = format_verification_projection(projection)
    assert snapshot == (
        "Verification ledger     2 records\n"
        "Latest checked          FAILED 999999999999 PYTHON_SYNTAX\n"
        "Quick verified          VERIFIED aaaaaaaaaaaa release_certified\n"
        "Release certified       CERTIFIED aaaaaaaaaaaa release_certified\n"
        "Tracked runs            0"
    )

    html_path = tmp_path / "verification-status.html"
    write_verification_projection(projection, html_path=html_path)
    html = html_path.read_text(encoding="utf-8")
    assert "Latest checked" in html
    assert "Quick verified" in html
    assert "Release certified" in html
    assert "FAILED 999999999999 PYTHON_SYNTAX" in html

    registry_path = tmp_path / "runs.sqlite"
    with RunRegistry(registry_path):
        pass
    exit_code = cli.main(
        [
            "runs",
            "list",
            "--registry",
            str(registry_path),
            "--verification-ledger",
            str(ledger_path),
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Latest checked          FAILED" in output
    assert "Release certified       CERTIFIED" in output


def test_strategy_check_appends_source_bound_latest_checked_event(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "SimpleStrategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class SimpleStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    stoploss = -0.1\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        return dataframe\n",
        encoding="utf-8",
    )
    output = tmp_path / "compatibility.json"
    ledger_path = tmp_path / "verification.sqlite"
    upstream_commit = "c" * 40

    exit_code = cli.main(
        [
            "strategy",
            "check",
            str(source),
            "--output",
            str(output),
            "--verification-ledger",
            str(ledger_path),
            "--upstream-repository",
            "https://github.com/iterativv/NostalgiaForInfinity.git",
            "--upstream-commit",
            upstream_commit,
            "--strategy-version",
            "17.4.435",
        ]
    )

    with VerificationLedger(ledger_path, create=False) as ledger:
        records = ledger.records()
        projection = ledger.project()
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "state=latest_checked, outcome=success" in captured.out
    assert len(records) == 1
    assert records[0]["subject"]["id"] == records[0]["fingerprint"]["strategy_source_sha256"]
    assert records[0]["fingerprint"]["upstream_commit"] == upstream_commit
    assert records[0]["fingerprint"]["strategy_version"] == "17.4.435"
    assert records[0]["evidence"][0]["location"] == str(output.resolve())
    assert projection["strategy"]["latest_checked"]["outcome"] == "success"
    assert projection["strategy"]["quick_verified"] is None
    assert projection["strategy"]["release_certified"] is None
