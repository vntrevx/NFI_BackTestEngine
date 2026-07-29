"""Cleanup command orchestration."""

from __future__ import annotations

import argparse

from ..errors import SpecValidationError

COMMAND_NAMES = frozenset({"clean"})


def execute(args: argparse.Namespace) -> int:
    """Execute the evidence-aware cleanup command."""
    if args.command_name != "clean":
        raise AssertionError(f"unhandled clean command: {args.command_name}")

    if args.apply:
        if args.no_runtime_probes:
            raise SpecValidationError("--no-runtime-probes cannot be used with --apply")
        from ..clean_apply import apply_clean, format_clean_result

        result = apply_clean(
            args.root,
            audit_path=args.output,
            result_path=args.result,
            preserve=args.preserve,
            include_completed=args.include_completed,
        )
        print(format_clean_result(result))
        return 0

    from ..clean import create_clean_audit, format_clean_audit

    audit = create_clean_audit(
        args.root,
        output_path=args.output,
        preserve=args.preserve,
        inspect_runtime=not args.no_runtime_probes,
        include_completed=args.include_completed,
    )
    print(format_clean_audit(audit))
    return 0
