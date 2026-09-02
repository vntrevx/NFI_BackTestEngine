"""Result presentation and run-registry command orchestration."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ..canonical import read_json
from ..errors import NfiBacktestError

COMMAND_NAMES = frozenset({"confirm", "report", "runs", "status"})


def execute(args: argparse.Namespace) -> int:
    """Execute result confirmation, rendering, or registry inspection."""
    if args.command_name == "confirm":
        return execute_confirmation(args)
    if args.command_name == "report":
        return execute_result_report(args)
    if args.command_name == "runs":
        return execute_run_registry(args)
    if args.command_name == "status":
        return execute_status(args)
    raise AssertionError(f"unhandled report command: {args.command_name}")


def execute_confirmation(args: argparse.Namespace) -> int:
    from ..confirmation import confirm_research_run
    from ..result_report import format_terminal_summary, write_result_presentation

    report = confirm_research_run(
        args.run_directory,
        args.freqtrade_export,
        args.output_dir,
        strategy=args.strategy,
    )
    confirmation_path = args.output_dir / "confirmation.json"
    summary = write_result_presentation(
        args.run_directory,
        verification=report,
        verification_path=confirmation_path,
    )
    if report["equal"]:
        print(f"official exact parity: run={report['run_id']} -> {confirmation_path}")
    else:
        difference = report["difference"]
        print(
            f"official parity mismatch at {difference['path']}: "
            f"{difference['reason']} -> {confirmation_path}",
            file=sys.stderr,
        )
    print(
        format_terminal_summary(
            summary,
            args.run_directory,
            include_breakdowns=args.full_report,
        )
    )
    return 0 if report["equal"] else 1


def execute_result_report(args: argparse.Namespace) -> int:
    from ..result_report import format_terminal_summary, write_result_presentation
    from ..verification_ledger import (
        VerificationLedger,
        format_verification_projection,
        write_verification_projection,
    )

    verification = read_json(args.confirmation) if args.confirmation else None
    if verification is not None and not isinstance(verification, dict):
        raise NfiBacktestError("confirmation report must be a JSON object")
    summary = write_result_presentation(
        args.run_directory,
        verification=verification,
        verification_path=args.confirmation,
    )
    print(
        format_terminal_summary(
            summary,
            args.run_directory,
            include_breakdowns=args.full_report,
        )
    )
    if args.verification_ledger is not None:
        with VerificationLedger(args.verification_ledger, create=False) as ledger:
            projection = ledger.project()
        output = Path(args.run_directory).resolve() / "verification-status.html"
        write_verification_projection(projection, html_path=output)
        print(format_verification_projection(projection))
        print(f"Verification status  {output}")
    return 0


def execute_run_registry(args: argparse.Namespace) -> int:
    from ..result_report import format_run_list, format_run_record
    from ..run_registry import RunRegistry
    from ..verification_ledger import (
        VerificationLedger,
        format_verification_projection,
    )

    with RunRegistry(args.registry) as registry:
        if args.runs_command == "list":
            records = registry.list(limit=args.limit)
            projection = None
            if args.verification_ledger is not None:
                with VerificationLedger(args.verification_ledger, create=False) as ledger:
                    projection = ledger.project()
            if args.json:
                payload: Any = (
                    {"runs": records, "verification": projection}
                    if projection is not None
                    else records
                )
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(format_run_list(records))
                if projection is not None:
                    print()
                    print(format_verification_projection(projection))
        else:
            record = registry.show(args.run_id)
            print(
                json.dumps(record, ensure_ascii=False, indent=2)
                if args.json
                else format_run_record(
                    record,
                    include_breakdowns=args.full_report,
                )
            )
    return 0


def execute_status(args: argparse.Namespace) -> int:
    """Show a stable status view, including an active progress checkpoint when present."""
    from ..result_report import format_run_record
    from ..run_registry import RunRegistry

    record = None if args.run_id else _active_project_record(args.registry)
    if record is None:
        if not args.registry.is_file():
            raise NfiBacktestError("no research runs are registered")
        with RunRegistry(args.registry) as registry:
            if args.run_id:
                record = registry.show(args.run_id)
            else:
                records = registry.list(limit=1)
                if not records:
                    raise NfiBacktestError("no research runs are registered")
                record = registry.show(str(records[0]["run_id"]))
    progress_path = Path(str(record["output_directory"])) / "progress.json"
    progress = read_json(progress_path) if progress_path.is_file() else None
    if isinstance(progress, dict) and progress.get("status") == "running":
        pid = progress.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or not _pid_is_active(pid):
            progress = {**progress, "status": "interrupted", "eta_status": "not_applicable"}
    payload = {"schema_version": "1.0.0", "run": record, "progress": progress}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif isinstance(progress, dict) and progress.get("status") == "running":
        eta = progress.get("eta_seconds")
        eta_text = f"{eta}s" if isinstance(eta, int) else "estimating"
        print(
            f"Run {record['run_id']}\n"
            f"Status: running\n"
            f"Progress: {progress['percent']}% - {progress['label']}\n"
            f"Elapsed: {progress['elapsed_seconds']}s\n"
            f"ETA: {eta_text}\n"
            f"Output: {record['output_directory']}"
        )
    else:
        print(format_run_record(record, include_breakdowns=args.full_report))
    return 0


def _active_project_record(registry_path: Path) -> dict[str, Any] | None:
    from ..project_config import load_project

    project_path = registry_path.parent / "project.json"
    if not project_path.is_file():
        return None
    settings = load_project(project_path)
    progress_path = settings.output_directory / "progress.json"
    if not progress_path.is_file():
        return None
    progress = read_json(progress_path)
    if not isinstance(progress, dict) or progress.get("status") != "running":
        return None
    return {
        "run_id": "pending",
        "status": "running",
        "output_directory": str(settings.output_directory),
        "strategy_class": settings.class_name,
        "pair_count": len(settings.pairs) if settings.pairs is not None else 0,
        "trade_count": None,
        "report": None,
    }


def _pid_is_active(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True
