"""Project setup, data preparation, and native-run command orchestration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ..canonical import write_json
from ..config_loader import load_effective_config
from ..errors import NfiBacktestError
from ..strategy_ir import (
    analyze_strategy,
    prepare_strategy,
    validate_strategy_bundle,
)

COMMAND_NAMES = frozenset({"init", "run", "data", "strategy", "backtest", "batch"})


def execute(args: argparse.Namespace) -> int:
    """Execute project, input-preparation, strategy, or native-run commands."""
    if args.command_name == "init":
        from ..project_setup import initialize_project

        initialize_project(
            project_path=args.project,
            source=args.source,
            class_name=args.class_name,
            config_path=args.config,
            data_directory=args.datadir,
            timerange=args.timerange,
            output_directory=args.output_dir,
            pairs=args.pair,
            interactive=not args.yes,
            force=args.force,
        )
        return 0

    if args.command_name == "run":
        from ..project_setup import (
            initialize_project,
            load_project,
            project_run_arguments,
        )

        if args.verification_timeout is not None and args.verification_timeout <= 0:
            raise NfiBacktestError("--verification-timeout must be positive")
        if args.verify is False and args.verification_timeout is not None:
            raise NfiBacktestError(
                "--verification-timeout cannot be combined with --no-verify"
            )
        if args.prepare_only and args.verify is True:
            raise NfiBacktestError("--verify requires a completed Native run")

        project_path = args.project.resolve()
        if project_path.is_file():
            supplied = {
                "source": args.source,
                "--class": args.class_name,
                "--config": args.config,
                "--datadir": args.datadir,
                "--timerange": args.timerange,
                "--output-dir": args.output_dir,
                "--pair": args.pair,
            }
            changed = [name for name, value in supplied.items() if value is not None]
            if changed:
                raise NfiBacktestError(
                    "saved project already exists; reconfigure with "
                    f"`nfi-bte init --force` instead of overriding {', '.join(changed)}"
                )
            settings = load_project(project_path)
        else:
            settings = initialize_project(
                project_path=project_path,
                source=args.source,
                class_name=args.class_name,
                config_path=args.config,
                data_directory=args.datadir,
                timerange=args.timerange,
                output_directory=args.output_dir,
                pairs=args.pair,
                interactive=not args.yes,
            )
        output = settings.output_directory
        resume = output.is_dir() and any(output.iterdir())
        if resume:
            print(f"existing run found; resuming hash-valid stages from {output}")
        from ..user_flow import (
            finish_one_line_run,
            format_run_preflight,
            write_run_preflight,
        )

        preflight, preflight_path = write_run_preflight(
            settings,
            resume=resume,
            download_missing=not args.no_download,
        )
        print(format_run_preflight(preflight, preflight_path))
        native_status = execute_research_backtest(
            project_run_arguments(settings),
            workers=args.workers,
            resume=resume,
            prepare_only=args.prepare_only,
            download_missing=not args.no_download,
            market_metadata_path=args.markets,
            download_market_metadata=not args.no_market_download,
            recalibrate=args.recalibrate,
            history_coverage_policy=args.history_coverage,
            full_report=args.full_report,
            print_summary=False,
        )
        return finish_one_line_run(
            settings,
            native_status=native_status,
            verification=args.verify,
            verification_timeout_seconds=args.verification_timeout,
            open_report=args.open_report,
            interactive=not args.yes and sys.stdin.isatty(),
            include_breakdowns=args.full_report,
        )

    if args.command_name == "data":
        from ..data_seal import prepare_data, validate_data_seal

        if args.data_command == "prepare":
            seal = prepare_data(
                config_path=args.config,
                data_directory=args.datadir,
                timerange=args.timerange,
                timeframes=args.timeframe,
                destination=args.output,
                download_missing=not args.no_download,
                startup_candles=args.startup_candles,
                require_startup_coverage=args.require_startup_coverage,
                history_coverage_policy=args.history_coverage,
            )
            print(
                f"data sealed: {len(seal['files'])} files, "
                f"downloads={len(seal['downloads'])}, "
                f"aggregate={seal['aggregate_sha256']} -> {args.output}"
            )
        else:
            seal = validate_data_seal(args.seal)
            print(
                f"data seal valid: {len(seal['files'])} files, "
                f"aggregate={seal['aggregate_sha256']}"
            )
        return 0

    if args.command_name == "strategy":
        return _execute_strategy(args)

    if args.command_name == "backtest":
        return execute_research_backtest(
            {
                "strategy_path": args.source,
                "class_name": args.class_name,
                "config_path": args.config,
                "data_directory": args.datadir,
                "timerange": args.timerange,
                "output_directory": args.output_dir,
                "pairs": args.pair,
                "cache_directory": args.cache_dir,
                "profile_path": args.profile,
                "registry_path": args.registry,
            },
            workers=args.workers,
            resume=args.resume,
            prepare_only=args.prepare_only,
            download_missing=not args.no_download,
            market_metadata_path=args.markets,
            download_market_metadata=not args.no_market_download,
            recalibrate=args.recalibrate,
            history_coverage_policy=args.history_coverage,
            full_report=args.full_report,
        )

    if args.command_name == "batch":
        from ..batch_runner import run_batch

        report = run_batch(
            args.manifest,
            args.output_dir,
            profile_path=args.profile,
            cache_directory=args.cache_dir,
            registry_path=args.registry,
            resume=args.resume,
            download_missing=not args.no_download,
            max_jobs=args.max_jobs,
        )
        print(
            f"batch: complete={report['complete']}, "
            f"jobs={len(report['jobs'])}, parallel={report['parallel_jobs']} -> "
            f"{args.output_dir / 'batch.json'}"
        )
        return 0 if report["complete"] else 1

    raise AssertionError(f"unhandled run command: {args.command_name}")


def _execute_strategy(args: argparse.Namespace) -> int:
    if args.strategy_command == "inspect":
        analysis = analyze_strategy(args.source, class_name=args.class_name)
        if args.output:
            write_json(args.output, analysis)
        print(
            f"strategy inspection: classes={len(analysis['strategies'])}, "
            f"diagnostics={len(analysis['diagnostics'])}, "
            f"static_safe={analysis['static_safe']}"
        )
        for diagnostic in analysis["diagnostics"]:
            location = diagnostic["location"]
            print(
                f"{location['path']}:{location['line']}:{location['column']}: "
                f"{diagnostic['code']}: {diagnostic['message']}",
                file=sys.stderr,
            )
        return 0 if analysis["static_safe"] else 1
    if args.strategy_command == "check":
        from ..strategy_compatibility import check_strategy_compatibility
        from ..verification_ledger import (
            VerificationLedger,
            record_strategy_compatibility,
        )

        report = check_strategy_compatibility(
            args.source,
            class_name=args.class_name,
            config_path=args.config,
            trading_mode=args.trading_mode,
            output_path=args.output,
        )
        print(
            "strategy compatibility: "
            f"native_compatible={report['native_compatible']}, "
            f"class={report['selected_class']}, "
            f"source={report['source']['sha256']}"
        )
        for blocker in report["blockers"]:
            print(
                f"blocked: {blocker['code']} - {blocker['message']}",
                file=sys.stderr,
            )
        if args.verification_ledger is not None:
            with VerificationLedger(args.verification_ledger) as ledger:
                sequence = record_strategy_compatibility(
                    ledger,
                    report,
                    upstream_repository=args.upstream_repository,
                    upstream_commit=args.upstream_commit,
                    strategy_version=args.strategy_version,
                    report_path=args.output,
                )
            print(
                "verification ledger: "
                f"sequence={sequence}, state=latest_checked, "
                f"outcome={'success' if report['native_compatible'] else 'failure'}"
            )
        return 0 if report["native_compatible"] else 1
    if args.strategy_command == "prepare":
        manifest = prepare_strategy(
            args.source,
            args.output_dir,
            class_name=args.class_name,
        )
        print(f"strategy prepared: {manifest['selected_class']} -> {args.output_dir}")
        return 0
    if args.strategy_command == "vectors":
        from ..vector_runtime import prepare_vector_signals

        loaded = load_effective_config(args.config)
        report = prepare_vector_signals(
            strategy_path=args.source,
            class_name=args.class_name,
            config=loaded["config"],
            pairs=args.pair,
            data_directory=args.datadir,
            timerange=args.timerange,
            output_directory=args.output_dir,
            workers=args.workers,
            cache_directory=args.cache_dir,
        )
        print(
            f"strategy vectors: pairs={report['pair_count']}, "
            f"cache_hits={report['cache_hits']} -> {args.output_dir}"
        )
        return 0
    manifest = validate_strategy_bundle(args.bundle)
    print(
        f"strategy bundle valid: {manifest['selected_class']}, "
        f"sha256={manifest['strategy']['sha256']}"
    )
    return 0


def execute_research_backtest(
    arguments: dict[str, Any],
    *,
    workers: int | None,
    resume: bool,
    prepare_only: bool,
    download_missing: bool,
    market_metadata_path: Path | None,
    download_market_metadata: bool,
    recalibrate: bool,
    history_coverage_policy: str,
    full_report: bool,
    print_summary: bool = True,
) -> int:
    """Run the existing research contract for advanced and wizard-backed commands."""
    from ..research_runner import run_research_backtest
    from ..result_report import format_terminal_summary, load_result_summary

    report = run_research_backtest(
        **arguments,
        workers=workers,
        resume=resume,
        prepare_only=prepare_only,
        download_missing=download_missing,
        market_metadata_path=market_metadata_path,
        download_market_metadata=download_market_metadata,
        recalibrate=recalibrate,
        history_coverage_policy=history_coverage_policy,
    )
    output = Path(arguments["output_directory"])
    if (output / "summary.json").is_file():
        if print_summary:
            summary = load_result_summary(output)
            print(
                format_terminal_summary(
                    summary,
                    output,
                    include_breakdowns=full_report,
                )
            )
    else:
        # Third-party wrappers may substitute the research runner and return only
        # its historical dictionary contract. Keep that integration usable while
        # native runs always produce the richer presentation files.
        print(
            f"research backtest: status={report['status']}, "
            f"pairs={report['vectors']['pair_count']}, "
            f"cache_hits={report['vectors']['cache_hits']}, "
            f"resumed={','.join(report['resumed_stages']) or 'none'} -> "
            f"{output / 'run.json'}"
        )
    if not report["complete"] and not report["prepared_only"]:
        for blocker in report["capability"]["blockers"]:
            detail = blocker.get("callback", "")
            print(
                f"blocked: {blocker['code']} {detail} - {blocker['message']}",
                file=sys.stderr,
            )
        return 1
    return 0
