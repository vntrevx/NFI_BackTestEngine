"""Cleanup command orchestration."""

from __future__ import annotations

import argparse

COMMAND_NAMES = frozenset({"clean"})


def execute(args: argparse.Namespace) -> int:
    """Execute the evidence-aware cleanup command."""
    if args.command_name != "clean":
        raise AssertionError(f"unhandled clean command: {args.command_name}")

    from ..clean import create_clean_audit, format_clean_audit

    audit = create_clean_audit(
        args.root,
        output_path=args.output,
        preserve=args.preserve,
        inspect_runtime=not args.no_runtime_probes,
    )
    print(format_clean_audit(audit))
    return 0
