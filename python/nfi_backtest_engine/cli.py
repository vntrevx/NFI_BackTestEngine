"""Command-line entry point for Phase 0 and Phase 1 tools."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .benchmark import run_benchmark
from .canonical import write_json
from .data_seal import prepare_data, validate_data_seal
from .doctor import run_doctor
from .engine_runtime import build_engine, run_engine
from .errors import NfiBacktestError
from .fixture import seal_fixture, validate_fixture
from .fixture_engine import run_fixture_engine
from .hardware import (
    GIB,
    create_execution_profile,
    inspect_hardware,
    load_execution_profile,
)
from .normalize import normalize_file
from .parity import ParityMismatch, compare_surface_files
from .performance_gate import run_performance_gate
from .profiling import aggregate_profile_file
from .reference_runtime import capture_reference_markets, run_reference_fixture
from .state_trace import TraceMismatch, compare_state_traces, trace_summary
from .strategy_ir import (
    analyze_strategy,
    prepare_strategy,
    validate_strategy_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nfi-bte")
    subcommands = parser.add_subparsers(dest="command_name", required=True)

    fixture = subcommands.add_parser("fixture", help="manage benchmark fixtures")
    fixture_commands = fixture.add_subparsers(dest="fixture_command", required=True)
    validate = fixture_commands.add_parser("validate", help="validate and hash-check a fixture")
    validate.add_argument("manifest", type=Path)
    validate.add_argument(
        "--skip-hashes",
        action="store_true",
        help="validate structure and files without SHA-256 verification",
    )
    seal = fixture_commands.add_parser("seal", help="refresh byte counts and SHA-256 values")
    seal.add_argument("manifest", type=Path)

    normalize = subcommands.add_parser(
        "normalize", help="normalize an official Freqtrade JSON export"
    )
    normalize.add_argument("source", type=Path)
    normalize.add_argument("--output", "-o", type=Path, required=True)
    normalize.add_argument("--strategy")
    normalize.add_argument(
        "--surface-version",
        choices=("1", "2"),
        default="1",
        help="normalized trade surface contract version (default: 1)",
    )

    parity = subcommands.add_parser("parity", help="compare two trade surfaces exactly")
    parity.add_argument("expected", type=Path)
    parity.add_argument("actual", type=Path)

    trace = subcommands.add_parser("trace", help="inspect or compare exact state traces")
    trace_commands = trace.add_subparsers(dest="trace_command", required=True)
    trace_inspect = trace_commands.add_parser("inspect", help="validate and summarize a trace")
    trace_inspect.add_argument("source", type=Path)
    trace_compare = trace_commands.add_parser("compare", help="compare two traces exactly")
    trace_compare.add_argument("expected", type=Path)
    trace_compare.add_argument("actual", type=Path)

    profile = subcommands.add_parser("profile", help="aggregate Phase 0 profile spans")
    profile.add_argument("events", type=Path)
    profile.add_argument("--output", "-o", type=Path, required=True)

    benchmark = subcommands.add_parser(
        "benchmark",
        help="measure a command against a sealed fixture",
        description=(
            "Measure the manifest command, or append `-- <command> [args...]` to override it."
        ),
    )
    benchmark.add_argument("manifest", type=Path)
    benchmark.add_argument("--output", "-o", type=Path, required=True)

    reference = subcommands.add_parser(
        "reference", help="run the pinned official Freqtrade reference"
    )
    reference_commands = reference.add_subparsers(dest="reference_command", required=True)
    reference_run = reference_commands.add_parser(
        "run", help="run and exact-compare one sealed captured fixture"
    )
    reference_run.add_argument("manifest", type=Path)
    reference_run.add_argument("--output-dir", type=Path, required=True)
    reference_run.add_argument(
        "--trace",
        choices=("off", "hash", "full"),
        default="off",
        help="reference state trace level (default: off)",
    )
    reference_run.add_argument(
        "--no-profile",
        action="store_true",
        help="disable low-overhead Phase 0 profiling",
    )
    reference_run.add_argument("--timeout", type=int)
    reference_capture = reference_commands.add_parser(
        "capture-markets",
        help="capture and freeze CCXT markets for later offline reference runs",
    )
    reference_capture.add_argument("manifest", type=Path)
    reference_capture.add_argument("--output", "-o", type=Path, required=True)
    reference_capture.add_argument("--timeout", type=int, default=180)

    doctor = subcommands.add_parser("doctor", help="check local execution prerequisites")
    doctor.add_argument("--profile", type=Path)
    doctor.add_argument("--output", "-o", type=Path)

    system = subcommands.add_parser("system", help="inspect and tune this computer")
    system_commands = system.add_subparsers(dest="system_command", required=True)
    system_inspect = system_commands.add_parser("inspect", help="print visible hardware resources")
    system_inspect.add_argument("--output", "-o", type=Path)
    system_tune = system_commands.add_parser(
        "tune", help="create a hardware-bound execution profile"
    )
    system_tune.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path(".nfi/execution-profile.json"),
    )
    system_tune.add_argument("--memory-cap-gib", type=float, default=8.0)
    system_tune.add_argument("--engine-peak-mib", type=float)
    system_tune.add_argument("--reference-peak-mib", type=float)
    system_tune.add_argument(
        "--force",
        action="store_true",
        help="replace an existing hardware profile",
    )
    system_show = system_commands.add_parser(
        "show", help="validate and print an execution profile"
    )
    system_show.add_argument("profile", type=Path)

    data = subcommands.add_parser("data", help="prepare and validate frozen candle inputs")
    data_commands = data.add_subparsers(dest="data_command", required=True)
    data_prepare = data_commands.add_parser(
        "prepare", help="fill missing coverage and write an immutable data seal"
    )
    data_prepare.add_argument("--config", type=Path, required=True)
    data_prepare.add_argument("--datadir", type=Path, required=True)
    data_prepare.add_argument("--timerange", required=True)
    data_prepare.add_argument("--timeframe", action="append", default=[])
    data_prepare.add_argument("--output", "-o", type=Path, required=True)
    data_prepare.add_argument(
        "--no-download",
        action="store_true",
        help="fail instead of downloading missing candle ranges",
    )
    data_validate = data_commands.add_parser(
        "validate", help="verify every hash and coverage value in a data seal"
    )
    data_validate.add_argument("seal", type=Path)

    strategy = subcommands.add_parser("strategy", help="inspect and prepare strategy sources")
    strategy_commands = strategy.add_subparsers(dest="strategy_command", required=True)
    strategy_inspect = strategy_commands.add_parser(
        "inspect", help="emit static capability IR and exact diagnostics"
    )
    strategy_inspect.add_argument("source", type=Path)
    strategy_inspect.add_argument("--class", dest="class_name")
    strategy_inspect.add_argument("--output", "-o", type=Path)
    strategy_prepare = strategy_commands.add_parser(
        "prepare", help="create a hash-bound, static-safe strategy bundle"
    )
    strategy_prepare.add_argument("source", type=Path)
    strategy_prepare.add_argument("--class", dest="class_name")
    strategy_prepare.add_argument("--output-dir", type=Path, required=True)
    strategy_validate = strategy_commands.add_parser(
        "validate", help="validate a prepared strategy bundle"
    )
    strategy_validate.add_argument("bundle", type=Path)

    engine = subcommands.add_parser("engine", help="build and run the Rust simulator")
    engine_commands = engine.add_subparsers(dest="engine_command", required=True)
    engine_build = engine_commands.add_parser("build", help="build the pinned Linux core")
    engine_build.add_argument("--force", action="store_true")
    engine_run = engine_commands.add_parser("run", help="run a simulator input JSON")
    engine_run.add_argument("input", type=Path)
    engine_run.add_argument("--output", "-o", type=Path, required=True)
    engine_run.add_argument("--profile", type=Path)
    engine_run.add_argument("--timeout", type=int)
    engine_run.add_argument(
        "--events",
        type=Path,
        help="stream compact every-candle engine states as JSONL",
    )
    engine_fixture = engine_commands.add_parser(
        "fixture", help="run and exact-compare a supported contract fixture"
    )
    engine_fixture.add_argument("manifest", type=Path)
    engine_fixture.add_argument("--output-dir", type=Path, required=True)
    engine_fixture.add_argument("--profile", type=Path)
    engine_fixture.add_argument("--timeout", type=int)
    engine_fixture.add_argument(
        "--level",
        choices=("quick", "full"),
        default="quick",
        help="quick compares final trade results; full also compares every-candle state",
    )

    performance = subcommands.add_parser(
        "performance",
        help="run a fresh same-fixture engine/reference parity and resource gate",
    )
    performance.add_argument("manifest", type=Path)
    performance.add_argument("--output-dir", type=Path, required=True)
    performance.add_argument("--profile", type=Path)
    performance.add_argument("--level", choices=("quick", "full"), default="full")
    performance.add_argument("--runs", type=int, default=1)
    performance.add_argument("--timeout", type=int, default=600)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    benchmark_command: list[str] | None = None
    if raw_args[:1] == ["benchmark"] and "--" in raw_args:
        separator = raw_args.index("--")
        benchmark_command = raw_args[separator + 1 :]
        raw_args = raw_args[:separator]
    args = build_parser().parse_args(raw_args)
    try:
        if args.command_name == "fixture":
            if args.fixture_command == "validate":
                manifest = validate_fixture(args.manifest, verify_hashes=not args.skip_hashes)
                print(f"fixture valid: {manifest['fixture_id']} ({manifest['evidence_status']})")
            else:
                manifest = seal_fixture(args.manifest)
                print(f"fixture sealed: {manifest['fixture_id']}")
            return 0

        if args.command_name == "normalize":
            surface = normalize_file(
                args.source,
                args.output,
                strategy=args.strategy,
                surface_version=args.surface_version,
            )
            print(f"normalized {len(surface['trades'])} trades -> {args.output}")
            return 0

        if args.command_name == "parity":
            compare_surface_files(args.expected, args.actual)
            print(f"exact parity: {args.expected} == {args.actual}")
            return 0

        if args.command_name == "trace":
            if args.trace_command == "inspect":
                summary = trace_summary(args.source)
                print(
                    f"state trace valid: {summary['event_count']} events, "
                    f"stream {summary['stream_hash']}"
                )
            else:
                compare_state_traces(args.expected, args.actual)
                print(f"exact state parity: {args.expected} == {args.actual}")
            return 0

        if args.command_name == "profile":
            report = aggregate_profile_file(args.events, args.output)
            print(f"profile aggregated: {len(report['phases'])} phases -> {args.output}")
            return 0 if not report["missing_phases"] else 1

        if args.command_name == "benchmark":
            report = run_benchmark(args.manifest, args.output, command_override=benchmark_command)
            print(f"benchmark report -> {args.output}")
            return 0 if report["complete"] else 1

        if args.command_name == "reference":
            if args.reference_command == "capture-markets":
                record = capture_reference_markets(
                    args.manifest,
                    args.output,
                    timeout_seconds=args.timeout,
                )
                print(
                    f"market snapshot captured: {record['bytes']} bytes, "
                    f"sha256={record['sha256']} -> {args.output}"
                )
                return 0
            report = run_reference_fixture(
                args.manifest,
                args.output_dir,
                trace_mode=args.trace,
                profile=not args.no_profile,
                timeout_seconds=args.timeout,
            )
            print(
                f"reference parity: trades={report['parity']['trade_surface']['equal']}, "
                f"state={report['parity']['state_trace']}, report={args.output_dir / 'run.json'}"
            )
            return 0 if report["complete"] else 1

        if args.command_name == "doctor":
            report = run_doctor(profile_path=args.profile)
            if args.output:
                write_json(args.output, report)
            print(
                f"doctor: {'healthy' if report['healthy'] else 'unhealthy'}; "
                + ", ".join(
                    f"{check['name']}={check['status']}" for check in report["checks"]
                )
            )
            return 0 if report["healthy"] else 1

        if args.command_name == "system":
            if args.system_command == "inspect":
                hardware = inspect_hardware()
                if args.output:
                    write_json(args.output, hardware)
                print(json.dumps(hardware, ensure_ascii=False, indent=2))
                return 0
            if args.system_command == "tune":
                if args.memory_cap_gib < 1:
                    raise NfiBacktestError("--memory-cap-gib must be at least 1")
                if args.output.exists() and not args.force:
                    raise NfiBacktestError(
                        f"execution profile already exists: {args.output}; "
                        "use --force to recalibrate"
                    )
                profile = create_execution_profile(
                    args.output,
                    memory_cap_bytes=int(args.memory_cap_gib * GIB),
                    observed_engine_peak_bytes=(
                        int(args.engine_peak_mib * 1024**2)
                        if args.engine_peak_mib is not None
                        else None
                    ),
                    observed_reference_peak_bytes=(
                        int(args.reference_peak_mib * 1024**2)
                        if args.reference_peak_mib is not None
                        else None
                    ),
                )
                tuning = profile["tuning"]
                print(
                    f"execution profile -> {args.output}; "
                    f"indicators={tuning['indicator_threads']}, "
                    f"engine_jobs={tuning['independent_engine_jobs']}, "
                    f"reference_jobs={tuning['independent_reference_jobs']}, "
                    f"memory={tuning['working_memory_bytes']}"
                )
                return 0
            profile = load_execution_profile(args.profile)
            print(json.dumps(profile, ensure_ascii=False, indent=2))
            return 0

        if args.command_name == "data":
            if args.data_command == "prepare":
                seal = prepare_data(
                    config_path=args.config,
                    data_directory=args.datadir,
                    timerange=args.timerange,
                    timeframes=args.timeframe,
                    destination=args.output,
                    download_missing=not args.no_download,
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
            if args.strategy_command == "prepare":
                manifest = prepare_strategy(
                    args.source,
                    args.output_dir,
                    class_name=args.class_name,
                )
                print(
                    f"strategy prepared: {manifest['selected_class']} -> {args.output_dir}"
                )
                return 0
            manifest = validate_strategy_bundle(args.bundle)
            print(
                f"strategy bundle valid: {manifest['selected_class']}, "
                f"sha256={manifest['strategy']['sha256']}"
            )
            return 0

        if args.command_name == "engine":
            if args.engine_command == "build":
                build = build_engine(force=args.force)
                print(
                    f"engine built: sha256={build['binary_sha256']}, "
                    f"source={build['source_fingerprint']}, "
                    f"seconds={build['build_seconds']}"
                )
                return 0
            if args.engine_command == "run":
                report = run_engine(
                    args.input,
                    args.output,
                    profile_path=args.profile,
                    timeout_seconds=args.timeout,
                    events_path=args.events,
                )
                print(
                    f"engine result: trades={report['trade_count']}, "
                    f"seconds={report['wall_time_seconds']} -> {args.output}"
                )
                return 0
            report = run_fixture_engine(
                args.manifest,
                args.output_dir,
                profile_path=args.profile,
                timeout_seconds=args.timeout,
                verification_level=args.level,
            )
            print(
                f"engine fixture parity ({report['verification_level']}): "
                f"trades={report['parity']['trade_surface']['equal']}, "
                f"state={report['parity']['state_trace']['equal']} -> "
                f"{args.output_dir / 'run.json'}"
            )
            return 0 if report["complete"] else 1

        if args.command_name == "performance":
            report = run_performance_gate(
                args.manifest,
                args.output_dir,
                profile_path=args.profile,
                verification_level=args.level,
                repetitions=args.runs,
                timeout_seconds=args.timeout,
            )
            speed = report["gates"]["speed"]
            memory = report["gates"]["memory"]
            print(
                f"performance gate: parity={report['gates']['parity']['met']}, "
                f"speedup={speed['observed_speedup']:.3f}x "
                f"({speed['verdict']}), memory={memory['observed_peak_bytes']} -> "
                f"{args.output_dir / 'performance.json'}"
            )
            return 0 if report["complete"] else 1

        raise AssertionError(f"unhandled command: {args.command_name}")
    except ParityMismatch as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except TraceMismatch as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (NfiBacktestError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
