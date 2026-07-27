"""Result presentation and run-registry command orchestration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..canonical import read_json
from ..errors import NfiBacktestError

COMMAND_NAMES = frozenset({"confirm", "report", "runs"})


def execute(args: argparse.Namespace) -> int:
    """Execute result confirmation, rendering, or registry inspection."""
    if args.command_name == "confirm":
        return execute_confirmation(args)
    if args.command_name == "report":
        return execute_result_report(args)
    if args.command_name == "runs":
        return execute_run_registry(args)
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
