"""Fixture, parity, benchmark, and native-engine command orchestration."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from ..benchmark import run_benchmark
from ..engine_runtime import build_engine, run_engine
from ..fixture import seal_fixture, validate_fixture
from ..fixture_engine import run_fixture_engine
from ..normalize import normalize_file
from ..parity import compare_surface_files
from ..performance_gate import run_performance_gate
from ..profiling import aggregate_profile_file
from ..state_trace import compare_state_traces, trace_summary

COMMAND_NAMES = frozenset(
    {
        "fixture",
        "probe",
        "normalize",
        "parity",
        "trace",
        "profile",
        "benchmark",
        "engine",
        "performance",
    }
)


def execute(
    args: argparse.Namespace,
    *,
    benchmark_command: list[str] | None = None,
    run_benchmark_service: Callable[..., dict[str, Any]] = run_benchmark,
) -> int:
    """Execute fixture and native-engine lifecycle commands."""
    if args.command_name == "fixture":
        if args.fixture_command == "validate":
            manifest = validate_fixture(args.manifest, verify_hashes=not args.skip_hashes)
            print(f"fixture valid: {manifest['fixture_id']} ({manifest['evidence_status']})")
        elif args.fixture_command == "seal":
            manifest = seal_fixture(args.manifest)
            print(f"fixture sealed: {manifest['fixture_id']}")
        elif args.fixture_command == "upload":
            from ..object_storage import upload_artifact

            record = upload_artifact(
                args.source,
                args.destination,
                endpoint_url=args.endpoint_url,
            )
            print(
                f"S3 artifact uploaded and verified: {record['bytes']} bytes, "
                f"sha256={record['sha256']} -> {record['uri']}"
            )
        else:
            from ..object_storage import download_artifact

            record = download_artifact(
                args.source,
                args.output,
                expected_sha256=args.sha256,
                endpoint_url=args.endpoint_url,
            )
            print(
                f"S3 artifact downloaded and verified: {record['bytes']} bytes, "
                f"sha256={record['sha256']} -> {record['local_path']}"
            )
        return 0

    if args.command_name == "probe":
        from ..probe_capture import capture_x7_probe

        report = capture_x7_probe(
            args.spec,
            args.output_dir,
            args.work_dir,
            timeout_seconds=args.timeout,
            workers=args.workers,
        )
        print(
            "Full X7 probe captured: "
            f"fixture={report['fixture_id']}, "
            f"manifest_sha256={report['manifest_sha256']} -> "
            f"{args.output_dir / 'manifest.json'}"
        )
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
        elif args.trace_command == "compare":
            compare_state_traces(args.expected, args.actual)
            print(f"exact state parity: {args.expected} == {args.actual}")
        elif args.trace_command == "verify-schedule":
            from ..scheduler_verification import verify_scheduler_events

            report = verify_scheduler_events(
                args.manifest,
                args.official_semantic_trace,
                args.native_events,
                args.contract,
                output_path=args.output,
            )
            print(
                "scheduler event order: "
                f"exact={report['event_order_exact']}, "
                f"events={report['event_count']} -> {args.output}"
            )
            return 0 if report["event_order_exact"] else 1
        elif args.trace_command == "verify-portfolio":
            from ..portfolio_trace import verify_portfolio_trace

            report = verify_portfolio_trace(
                args.manifest,
                args.official_trace,
                args.native_events,
                output_path=args.output,
            )
            print(
                "portfolio semantic trace: "
                f"exact={report['exact']}, events={report['event_count']} -> {args.output}"
            )
            return 0 if report["exact"] else 1
        elif args.trace_command == "materialize-complete":
            from ..complete_semantic_trace import materialize_native_complete_trace

            report = materialize_native_complete_trace(
                args.manifest,
                args.native_events,
                args.output,
            )
            print(
                "complete semantic trace materialized: "
                f"events={report['event_count']} -> {args.output}"
            )
        elif args.trace_command == "verify-complete":
            from ..complete_semantic_trace import verify_complete_semantic_traces

            report = verify_complete_semantic_traces(
                args.expected,
                args.actual,
                output_path=args.output,
            )
            print(
                "complete semantic trace: "
                f"exact={report['exact']}, "
                f"events={report['actual_event_count']} -> {args.output}"
            )
            return 0 if report["exact"] else 1
        else:
            from ..execution_trace import verify_execution_trace

            report = verify_execution_trace(
                args.manifest,
                args.official_trace,
                args.native_events,
                output_path=args.output,
            )
            print(
                "execution semantic trace: "
                f"exact={report['exact']}, events={report['event_count']} -> {args.output}"
            )
            return 0 if report["exact"] else 1
        return 0

    if args.command_name == "profile":
        report = aggregate_profile_file(args.events, args.output)
        print(f"profile aggregated: {len(report['phases'])} phases -> {args.output}")
        return 0 if not report["missing_phases"] else 1

    if args.command_name == "benchmark":
        report = run_benchmark_service(
            args.manifest,
            args.output,
            command_override=benchmark_command,
        )
        print(f"benchmark report -> {args.output}")
        return 0 if report["complete"] else 1

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
                vector_manifest=args.vector_manifest,
                engine_profile_path=args.engine_profile,
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
            f"({speed['verdict']}), memory={memory['observed_peak_bytes']}, "
            f"release_certified={report['release_certified']} -> "
            f"{args.output_dir / 'performance.json'}"
        )
        return 0 if report["complete"] else 1

    raise AssertionError(f"unhandled fixture command: {args.command_name}")
