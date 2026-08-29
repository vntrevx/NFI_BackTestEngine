from __future__ import annotations

import copy
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from changed_target_ledger_support import _documents, _target
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.changed_target_ledger import (
    _sha256_json,
    build_changed_target_ledger,
)

ROOT = Path(__file__).parents[1]
VALIDATOR = ROOT / "scripts" / "validate_changed_target_promotion.py"


@dataclass(frozen=True, slots=True)
class WorkflowFiles:
    ledger: Path
    decisions: Path


def _fresh_blocked_workflow(tmp_path: Path) -> WorkflowFiles:
    targets = [
        _target("1", value="562"),
        _target(
            "2",
            value="leaf",
            callers=["populate_entry_trend", "populate_exit_trend"],
        ),
    ]
    sources = _documents(tmp_path, targets=targets)
    fixtures = read_json(sources.fixture_registry)
    for bundle in fixtures["bundles"]:
        bundle["upstream_commit"] = "9" * 40
    write_json(sources.fixture_registry, fixtures)
    for report in sources.targeted_reports.values():
        report.unlink()
    ledger = build_changed_target_ledger(sources)
    ledger_path = tmp_path / "changed-target-ledger.json"
    write_json(ledger_path, ledger)
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    identity = ledger["identity"]
    decision = {
        "identity": {
            "upstream_sha": identity["upstream_head"],
            "strategy_sha256": identity["new_source_sha256"],
            "freqtrade_digest": identity["freqtrade_digest"],
            "semantic_profile_sha256": identity["semantic_profile_sha256"],
        },
        "action": {"native_promotion_allowed": False},
    }
    for mode in ("futures", "spot"):
        write_json(decisions / f"automation-decision-{mode}.json", decision)
    return WorkflowFiles(ledger=ledger_path, decisions=decisions)


def _run_validator(files: WorkflowFiles) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--ledger",
            str(files.ledger),
            "--decisions",
            str(files.decisions),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _reseal(ledger: dict) -> None:
    ledger["fingerprint"] = _sha256_json(
        {key: value for key, value in ledger.items() if key != "fingerprint"}
    )


def test_second_crossed_matching_run_blocks_at_production_boundary(tmp_path: Path) -> None:
    sources = _documents(tmp_path)
    report = read_json(sources.targeted_reports["spot"])
    duplicate = copy.deepcopy(report["runs"][0])
    duplicate["oracle_digest"] = "sha256:" + "9" * 64
    duplicate["capture"]["source_sha256"] = "8" * 64
    duplicate["capture"]["upstream_commit"] = "7" * 40
    report["runs"].append(duplicate)
    write_json(sources.targeted_reports["spot"], report)

    ledger = build_changed_target_ledger(sources)

    assert ledger["summary"]["native_promotion_allowed"] is False
    assert {item["code"] for item in ledger["hard_blockers"]} >= {
        "AMBIGUOUS_TARGETED_PROOF",
        "STALE_TARGETED_PROOF",
        "STALE_ORACLE_PROOF",
    }


def test_two_valid_duplicate_matching_runs_are_ambiguous(tmp_path: Path) -> None:
    sources = _documents(tmp_path)
    report = read_json(sources.targeted_reports["spot"])
    report["runs"].append(copy.deepcopy(report["runs"][0]))
    write_json(sources.targeted_reports["spot"], report)

    ledger = build_changed_target_ledger(sources)

    assert ledger["summary"]["native_promotion_allowed"] is False
    assert "AMBIGUOUS_TARGETED_PROOF" in {
        item["code"] for item in ledger["hard_blockers"]
    }


def test_current_style_multitarget_blocked_ledger_accepts_false_decisions(
    tmp_path: Path,
) -> None:
    files = _fresh_blocked_workflow(tmp_path)

    completed = _run_validator(files)

    assert completed.returncode == 0, completed.stderr
    assert read_json(files.ledger)["summary"]["native_promotion_allowed"] is False


def test_workflow_rejects_mutated_target_blocker_array(tmp_path: Path) -> None:
    files = _fresh_blocked_workflow(tmp_path)
    ledger = read_json(files.ledger)
    ledger["targets"][0]["hard_blockers"].pop()
    _reseal(ledger)
    write_json(files.ledger, ledger)

    completed = _run_validator(files)

    assert completed.returncode != 0


def test_workflow_rejects_mutated_top_level_blocker_array(tmp_path: Path) -> None:
    files = _fresh_blocked_workflow(tmp_path)
    ledger = read_json(files.ledger)
    ledger["hard_blockers"].pop()
    _reseal(ledger)
    write_json(files.ledger, ledger)

    completed = _run_validator(files)

    assert completed.returncode != 0


def test_workflow_rejects_mutated_blocker_count(tmp_path: Path) -> None:
    files = _fresh_blocked_workflow(tmp_path)
    ledger = read_json(files.ledger)
    ledger["summary"]["hard_blocker_count"] -= 1
    _reseal(ledger)
    write_json(files.ledger, ledger)

    completed = _run_validator(files)

    assert completed.returncode != 0


def test_workflow_rejects_mutated_promotion_booleans(tmp_path: Path) -> None:
    files = _fresh_blocked_workflow(tmp_path)
    ledger = read_json(files.ledger)
    ledger["targets"][0]["native_promotion_allowed"] = True
    ledger["summary"]["native_promotion_allowed"] = True
    _reseal(ledger)
    write_json(files.ledger, ledger)

    completed = _run_validator(files)

    assert completed.returncode != 0


def test_workflow_rejects_mutated_fingerprint(tmp_path: Path) -> None:
    files = _fresh_blocked_workflow(tmp_path)
    ledger = read_json(files.ledger)
    ledger["fingerprint"] = "0" * 64
    write_json(files.ledger, ledger)

    completed = _run_validator(files)

    assert completed.returncode != 0
