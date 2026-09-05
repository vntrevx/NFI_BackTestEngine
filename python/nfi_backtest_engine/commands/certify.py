"""Certification command orchestration."""

from __future__ import annotations

import argparse

from ..errors import SpecValidationError

COMMAND_NAMES = frozenset({"certify"})


def execute(args: argparse.Namespace) -> int:
    """Run quick or Full X7 certification."""
    if args.command_name != "certify":
        raise AssertionError(f"unhandled certification command: {args.command_name}")

    if args.certification_profile == "full-x7":
        from ..full_x7_certification import run_full_x7_certification

        required = {
            "--profile": args.profile,
            "--strategy": args.strategy,
            "--class-name": args.class_name,
            "--config": args.config,
            "--data-dir": args.data_dir,
            "--engine-markets": args.engine_markets,
            "--wheel": args.wheel,
            "--swap-cap-gib": args.swap_cap_gib,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise SpecValidationError("Full X7 certification requires " + ", ".join(missing))
        report = run_full_x7_certification(
            args.manifest,
            args.output_dir,
            strategy_path=args.strategy,
            class_name=args.class_name,
            config_path=args.config,
            data_directory=args.data_dir,
            engine_market_snapshot=args.engine_markets,
            reference_market_snapshot=args.reference_markets,
            wheel_path=args.wheel,
            execution_profile_path=args.profile,
            state_probe_manifests=args.state_probe,
            repetitions=args.runs,
            timeout_seconds=args.timeout,
            official_oracle_directory=args.official_oracle,
            capture_oracle_only=args.capture_oracle_only,
            resume=args.resume,
            swap_cap_bytes=(
                int(args.swap_cap_gib * 1024**3) if args.swap_cap_gib is not None else None
            ),
        )
        if args.capture_oracle_only:
            print(
                f"Full X7 Oracle capture: status={report['status']}, "
                f"result={report['result_sha256']} -> "
                f"{args.output_dir / 'oracle-capture.json'}"
            )
            return 0 if report["complete"] else 1
        print(
            f"Full X7 certification: status={report['status']}, "
            f"speedup={report['gates']['speed']['observed_speedup']:.3f}x, "
            f"bundle_sha256={report['bundle']['archive']['sha256']} -> "
            f"{args.output_dir / 'full-x7-certification.json'}"
        )
        return 0 if report["release_certified"] else 1

    from ..certification import run_certification

    report = run_certification(
        args.manifest,
        args.output_dir,
        profile_path=args.profile,
        state_probe_manifests=args.state_probe,
        repetitions=args.runs,
        timeout_seconds=args.timeout,
    )
    print(
        f"certification: status={report['status']}, "
        f"speedup={report['measurements']['observed_speedup']:.3f}x, "
        f"bundle_sha256={report['bundle']['archive']['sha256']} -> "
        f"{args.output_dir / 'certification.json'}"
    )
    return 0 if report["release_certified"] else 1
