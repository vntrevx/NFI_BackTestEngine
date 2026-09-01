"""Project setup, data preparation, and native-run command orchestration."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..canonical import read_json, write_json
from ..config_loader import load_effective_config
from ..errors import NfiBacktestError
from ..strategy_ir import (
    analyze_strategy,
    prepare_strategy,
    validate_strategy_bundle,
)

COMMAND_NAMES = frozenset({"init", "run", "data", "strategy", "backtest", "batch"})

def _completed_run_exists(output: Path) -> bool:
    run_path = output / "run.json"
    if not run_path.is_file():
        return False
    try:
        report = read_json(run_path)
    except (OSError, ValueError):
        return False
    return isinstance(report, dict) and report.get("complete") is True


def _next_output_directory(output: Path) -> Path:
    candidate = output.with_name(f"{output.name}-new")
    suffix = 2
    while candidate.exists():
        candidate = output.with_name(f"{output.name}-new-{suffix}")
        suffix += 1
    return candidate


def execute(args: argparse.Namespace) -> int:
    """Execute project, input-preparation, strategy, or native-run commands."""
    if args.command_name == "init":
        from ..project_setup import initialize_project

        initialize_project(
            project_path=args.project,
            source=args.source,
            class_name=args.class_name,
            config_path=args.config,
            trading_mode=args.trading_mode,
            data_directory=args.datadir,
            timerange=args.timerange,
            output_directory=args.output_dir,
            pairs=args.pair,
            pair_count=args.pair_count,
            interactive=not args.yes,
            force=args.force,
        )
        return 0

    if args.command_name == "run":
        from ..project_setup import (
            initialize_project,
            load_project,
            project_run_arguments,
            retarget_project_output,
        )

        if args.verification_timeout is not None and args.verification_timeout <= 0:
            raise NfiBacktestError("--verification-timeout must be positive")
        if args.fallback_timeout is not None and args.fallback_timeout <= 0:
            raise NfiBacktestError("--fallback-timeout must be positive")
        if args.verify is False and args.verification_timeout is not None:
            raise NfiBacktestError("--verification-timeout cannot be combined with --no-verify")
        if args.prepare_only and args.verify is True:
            raise NfiBacktestError("--verify requires a completed Native run")
        if args.prepare_only and args.fallback == "official":
            raise NfiBacktestError("--fallback official cannot be combined with --prepare-only")

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
                "--pair-count": args.pair_count,
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
                trading_mode=args.trading_mode,
                data_directory=args.datadir,
                timerange=args.timerange,
                output_directory=args.output_dir,
                pairs=args.pair,
                pair_count=args.pair_count,
                interactive=not args.yes,
            )
        from ..user_flow import (
            RunProgress,
            finish_one_line_run,
            format_run_banner,
            format_run_preflight,
            resolve_consent,
            write_run_preflight,
        )

        interactive = sys.stdin.isatty()
        if not args.yes and not interactive:
            raise NfiBacktestError(
                "non-interactive run requires --yes to confirm CPU-intensive work"
            )

        saved_settings = settings
        output = settings.output_directory
        output_had_files = output.is_dir() and any(output.iterdir())
        completed_run = output_had_files and _completed_run_exists(output)
        start_fresh = args.new_run
        if start_fresh and not output_had_files:
            raise NfiBacktestError("--new-run requires existing run output")
        if completed_run and not start_fresh and not args.yes:
            print(f"Completed run found: {output}")
            start_fresh = resolve_consent(
                None,
                interactive=interactive,
                question="Start a new backtest with the same saved settings?",
            )
            print()

        if start_fresh:
            settings = replace(
                settings,
                output_directory=_next_output_directory(output),
            )
            resume = False
        else:
            resume = output_had_files
        reuse_completed = completed_run and not start_fresh

        if reuse_completed:
            print(format_run_banner(settings, resume=True))
            print()
            print(f"Using completed run: {settings.output_directory}")
            print("No input preparation or simulation will be started.")
            print()
        else:
            preflight, preflight_path = write_run_preflight(
                settings,
                resume=resume,
                download_missing=not args.no_download,
            )
            print(format_run_banner(settings, resume=resume))
            print()
            if start_fresh:
                print(f"  New run output  ·  {settings.output_directory}")
                print()
            print(format_run_preflight(preflight, preflight_path))
            print()
            cpu_worker_limit = int(preflight["host"]["cpu_worker_limit"])
            planned_workers = (
                args.workers if args.workers is not None else cpu_worker_limit
            )
            if planned_workers <= 0:
                raise NfiBacktestError("--workers must be positive")
            if planned_workers > cpu_worker_limit:
                raise NfiBacktestError(
                    f"--workers {planned_workers} exceeds this system's safe limit "
                    f"of {cpu_worker_limit}"
                )
            action = (
                "Prepare inputs now?"
                if args.prepare_only
                else "Resume this run now?"
                if resume
                else "Start this backtest now?"
            )
            approved = resolve_consent(
                True if args.yes else None,
                interactive=interactive,
                question=(
                    f"{action} Up to {planned_workers} CPU workers may run at full load"
                ),
            )
            if not approved:
                print("Backtest cancelled. No simulation was started.")
                return 0
            if start_fresh:
                settings = retarget_project_output(
                    saved_settings,
                    settings.output_directory,
                )
            print()
        with RunProgress() as progress:
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
                progress=progress.update,
            )
        return finish_one_line_run(
            settings,
            native_status=native_status,
            verification=args.verify,
            verification_timeout_seconds=args.verification_timeout,
            interactive=not args.yes and sys.stdin.isatty(),
            include_breakdowns=args.full_report,
            fallback_policy=args.fallback,
            fallback_timeout_seconds=args.fallback_timeout,
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
                f"data seal valid: {len(seal['files'])} files, aggregate={seal['aggregate_sha256']}"
            )
        return 0

    if args.command_name == "strategy":
        return _execute_strategy(args)

    if args.command_name == "backtest":
        if args.fallback_timeout is not None and args.fallback_timeout <= 0:
            raise NfiBacktestError("--fallback-timeout must be positive")
        if args.prepare_only and args.fallback == "official":
            raise NfiBacktestError("--fallback official cannot be combined with --prepare-only")
        native_status = execute_research_backtest(
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
            print_summary=False,
        )
        from ..result_report import format_terminal_summary, load_result_summary
        from ..user_flow import finish_official_fallback

        final_status = finish_official_fallback(
            args.output_dir,
            ledger_path=args.registry.parent / "verification-ledger.sqlite",
            native_status=native_status,
            fallback_policy=args.fallback,
            timeout_seconds=args.fallback_timeout,
            interactive=sys.stdin.isatty(),
            registry_path=args.registry,
        )
        if (args.output_dir / "summary.json").is_file():
            print(
                format_terminal_summary(
                    load_result_summary(args.output_dir),
                    args.output_dir,
                    include_breakdowns=args.full_report,
                )
            )
        return final_status

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
    if args.strategy_command == "semantic-inventory":
        from ..semantic_inventory import build_semantic_inventory

        report = build_semantic_inventory(
            args.source,
            class_name=args.class_name,
            trading_mode=args.trading_mode,
            config_path=args.config,
            fixtures_root=args.fixtures_root,
            source_root=args.source_root,
            upstream_repository=args.upstream_repository,
            upstream_commit=args.upstream_commit,
            upstream_ref=args.upstream_ref,
            upstream_source_path=args.upstream_source_path,
            upstream_fetch_timeout_seconds=args.upstream_fetch_timeout,
            output_path=args.output,
        )
        summary = report["summary"]
        print(
            "semantic inventory: "
            f"class={report['selected_class']}, mode={report['trading_mode']}, "
            f"callbacks={summary['rust_callback_count']}/{summary['active_callback_count']} rust, "
            f"source_bound={summary['source_bound_callback_count']}, "
            f"exact_fixtures={summary['exact_source_fixture_count']}, "
            f"complete={summary['inventory_complete']}"
        )
        if args.output:
            print(f"semantic inventory report: {args.output}")
        return 0 if summary["native_promotion"] else 1
    if args.strategy_command == "semantic-registry":
        from ..semantic_inventory import build_semantic_obligation_registry

        registry = build_semantic_obligation_registry(
            args.source,
            class_name=args.class_name,
            trading_mode=args.trading_mode,
            config_path=args.config,
            source_root=args.source_root,
            upstream_repository=args.upstream_repository,
            upstream_commit=args.upstream_commit,
            upstream_ref=args.upstream_ref,
            upstream_source_path=args.upstream_source_path,
            upstream_fetch_timeout_seconds=args.upstream_fetch_timeout,
            output_path=args.output,
        )
        summary = registry["summary"]
        print(
            "semantic registry: "
            f"class={registry['strategy']['selected_class']}, "
            f"obligations={summary['total_obligations']}, "
            f"unknown={summary['unknown_obligations']}, "
            f"native_promotion={summary['native_promotion']}"
        )
        print(f"semantic registry report: {args.output}")
        return 0 if summary["native_promotion"] else 1
    if args.strategy_command == "semantic-registry-packaged":
        from ..semantic_registry import load_packaged_semantic_obligation_registry

        registry = load_packaged_semantic_obligation_registry()
        summary = registry["summary"]
        upstream = registry["strategy"]["upstream"]
        print(
            "packaged semantic registry current-ref proof: "
            f"commit={upstream['observed_commit']}, "
            f"obligations={summary['total_obligations']}, "
            f"unknown={summary['unknown_obligations']}, "
            "native_promotion=True"
        )
        return 0
    if args.strategy_command == "semantic-registry-packaged-integrity":
        from ..semantic_registry import (
            validate_packaged_semantic_obligation_registry_integrity,
        )

        integrity = validate_packaged_semantic_obligation_registry_integrity()
        print(json.dumps(integrity, sort_keys=True, separators=(",", ":")))
        return 0
    if args.strategy_command == "indicator-inventory":
        from ..indicator_inventory import build_indicator_inventory

        report = build_indicator_inventory(
            args.source,
            class_name=args.class_name,
            upstream_repository=args.upstream_repository,
            upstream_commit=args.upstream_commit,
            output_path=args.output,
        )
        summary = report["summary"]
        print(
            "indicator inventory: "
            f"class={report['selected_class']}, "
            f"nodes={summary['reachable_node_count']}, "
            f"operations={summary['operation_count']}, "
            f"unresolved={summary['unresolved_call_site_count']}, "
            f"complete={summary['inventory_complete']}"
        )
        if args.output:
            print(f"indicator inventory report: {args.output}")
        return 0
    if args.strategy_command == "indicator-program":
        from ..indicator_program import compile_indicator_program

        config = load_effective_config(args.config)["config"] if args.config else None
        program = compile_indicator_program(
            args.source,
            class_name=args.class_name,
            config=config,
        )
        write_json(args.output, program)
        print(
            "indicator program: "
            f"class={program['selected_class']}, "
            f"functions={len(program['functions'])}, "
            f"nodes={len(program['nodes'])}, "
            f"inputs={len(program['required_input_columns'])}"
        )
        print(f"indicator program report: {args.output}")
        return 0
    if args.strategy_command == "signal-program":
        from ..signal_program import compile_signal_program

        config = load_effective_config(args.config)["config"] if args.config else None
        program = compile_signal_program(
            args.source,
            class_name=args.class_name,
            trading_mode=args.trading_mode,
            config=config,
        )
        write_json(args.output, program)
        print(
            "signal program: "
            f"class={program['selected_class']}, "
            f"mode={program['compile_context']['trading_mode']}, "
            f"nodes={len(program['nodes'])}, "
            f"mutations={len(program['mutation_nodes'])}"
        )
        print(f"signal program report: {args.output}")
        return 0
    if args.strategy_command == "tag-program":
        from ..tag_program import compile_tag_program

        config = load_effective_config(args.config)["config"] if args.config else None
        program = compile_tag_program(
            args.source,
            class_name=args.class_name,
            trading_mode=args.trading_mode,
            config=config,
        )
        write_json(args.output, program)
        print(
            "tag program: "
            f"class={program['selected_class']}, "
            f"mode={program['compile_context']['trading_mode']}, "
            f"nodes={len(program['nodes'])}, "
            f"tag_mutations={len(program['tag_mutation_nodes'])}"
        )
        print(f"tag program report: {args.output}")
        return 0
    if args.strategy_command == "callback-ir":
        from ..callback_source_ir import compile_callback_source_ir

        program = compile_callback_source_ir(
            args.source,
            class_name=args.class_name,
            trading_mode=args.trading_mode,
        )
        write_json(args.output, program)
        print(
            "callback source IR: "
            f"entrypoints={len(program['entrypoints'])}, "
            f"routes={len(program['route_keys'])}, "
            f"tags={len(program['emitted_tags'])}, "
            f"columns={len(program['required_columns'])} -> {args.output}"
        )
        return 0
    if args.strategy_command == "stateful-coverage":
        from ..stateful_coverage import build_stateful_coverage

        report = build_stateful_coverage(
            args.source,
            class_name=args.class_name,
            trading_mode=args.trading_mode,
            config_path=args.config,
            upstream_repository=args.upstream_repository,
            upstream_commit=args.upstream_commit,
            output_path=args.output,
        )
        summary = report["summary"]
        print(
            "stateful coverage: "
            f"class={report['selected_class']}, mode={report['trading_mode']}, "
            f"callbacks={summary['active_callback_count']}, "
            f"tags={summary['emitted_entry_tag_count']}, "
            f"dormant={summary['dormant_unsupported_tag_count']}, "
            f"reachable_gaps={summary['reachable_stateful_gap_count']}, "
            f"complete={summary['closure_complete']}"
        )
        print(f"stateful coverage report: {args.output}")
        return 0 if summary["closure_complete"] else 1
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
    if args.strategy_command == "diff":
        from ..strategy_diff import diff_strategies

        report = diff_strategies(
            args.old_source,
            args.new_source,
            class_name=args.class_name,
            output_path=args.output,
        )
        changes = report["changes"]
        callbacks = changes["callbacks"]
        callback_change_count = sum(len(callbacks[key]) for key in ("added", "removed", "changed"))
        print(
            "strategy diff: "
            f"classification={report['classification']}, "
            f"callbacks={callback_change_count}, "
            f"signals=+{len(changes['signals']['added'])}/-{len(changes['signals']['removed'])}, "
            f"state_keys=+{len(changes['custom_state_keys']['added'])}/"
            f"-{len(changes['custom_state_keys']['removed'])}"
        )
        if args.output:
            print(f"strategy diff report: {args.output}")
        return 0
    if args.strategy_command == "qualify":
        from ..compatibility_qualification import qualify_compatibility

        report = qualify_compatibility(
            args.compatibility_report,
            args.strategy_diff,
            branch_proof=args.branch_proof,
            output_path=args.output,
        )
        print(
            "strategy qualification: "
            f"state={report['verification_state']}, "
            f"changed_branch_reached={report['changed_branch_reached']}, "
            f"full_state_exact={report['full_state_exact']}"
        )
        return 0 if report["verification_state"] == "quick_verified" else 1
    if args.strategy_command == "verify-targeted":
        from ..targeted_verification import verify_targeted_strategy

        if args.timeout <= 0:
            raise NfiBacktestError("--timeout must be positive")
        report = verify_targeted_strategy(
            args.source,
            args.strategy_diff,
            args.compatibility_report,
            args.fixtures_root,
            args.output_dir,
            class_name=args.class_name,
            trading_mode=args.trading_mode,
            upstream_repository=args.upstream_repository,
            upstream_commit=args.upstream_commit,
            timeout_seconds=args.timeout,
            workers=args.workers,
        )
        print(
            "targeted strategy verification: "
            f"state={report['verification_state']}, "
            f"fixtures={report['plan']['selected_fixture_count']}, "
            f"missing_targets={report['plan']['missing_target_count']} -> "
            f"{args.output_dir / 'run.json'}"
        )
        return 0 if report["complete"] else 1
    if args.strategy_command in {"discover", "discover-futures"}:
        from datetime import date

        from ..futures_discovery import discover_futures_targets, discover_targets

        try:
            as_of = date.fromisoformat(args.as_of) if args.as_of else None
        except ValueError as exc:
            raise NfiBacktestError("--as-of must be a valid YYYY-MM-DD date") from exc
        trading_mode = args.trading_mode if args.strategy_command == "discover" else "futures"
        policy = (
            args.policy
            if args.policy is not None
            else Path(f"planning/{trading_mode}-discovery-policy.json")
        )
        service = (
            discover_targets if args.strategy_command == "discover" else discover_futures_targets
        )
        report = service(
            args.source,
            args.strategy_diff,
            args.compatibility_report,
            args.fixtures_root,
            policy,
            args.output_dir,
            class_name=args.class_name,
            upstream_repository=args.upstream_repository,
            upstream_commit=args.upstream_commit,
            baseline_source=args.baseline_source,
            baseline_upstream_commit=args.baseline_upstream_commit,
            engine_commit=args.engine_commit,
            profile_path=args.profile,
            cursor_path=args.cursor,
            as_of=as_of,
            workers=args.workers,
        )
        print(
            f"{trading_mode.capitalize()} target discovery: "
            f"status={report['status']}, "
            f"searched={report['searched_shard_count']}/{report['shard_count']}, "
            f"next={report['next_shard']} -> "
            f"{args.output_dir / 'discovery-report.json'}"
        )
        print(f"{report['message']} Official Freqtrade fallback: available.")
        return 1 if report["status"] == "infrastructure_failed" else 0
    if args.strategy_command == "state-machine":
        from ..state_machine_ir import compile_state_machine_program

        program = compile_state_machine_program(
            args.source,
            class_name=args.class_name,
            schema_version=args.schema_version,
            max_order_iterations=args.max_order_iterations,
        )
        write_json(args.output, program)
        print(
            "state-machine IR: "
            f"entrypoints={','.join(program['entrypoints']) or 'none'}, "
            f"opcodes={len(program['opcodes'])}, "
            f"state_keys={len(program['required_state_keys'])} -> {args.output}"
        )
        return 0
    if args.strategy_command == "shadow-gate":
        from ..state_machine_shadow import evaluate_state_machine_shadow_gate

        report = evaluate_state_machine_shadow_gate(
            args.legacy_run,
            args.candidate_run,
            legacy_trace=args.legacy_trace,
            candidate_trace=args.candidate_trace,
            branch_proof=args.branch_proof,
            output_path=args.output,
        )
        print(
            "state-machine shadow gate: "
            f"trade_surface_exact={report['trade_surface_exact']}, "
            f"full_state_exact={report['full_state_exact']}, "
            f"promoted={report['promoted']} -> {args.output}"
        )
        return 0 if report["promoted"] else 1
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
    progress: Callable[[int, str], None] | None = None,
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
        progress=progress,
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
