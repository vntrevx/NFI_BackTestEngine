"""Command-line entry point for Phase 0 and Phase 1 tools."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .benchmark import run_benchmark
from .canonical import read_json, write_json
from .config_loader import load_effective_config
from .doctor import run_doctor
from .engine_runtime import build_engine, run_engine
from .errors import NfiBacktestError, SpecValidationError
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
from .product_contract import (
    DEFAULT_CERTIFICATION_REPETITIONS,
    DEFAULT_FULL_X7_TIMEOUT_SECONDS,
)
from .profiling import aggregate_profile_file
from .reference_runtime import capture_reference_markets, run_reference_fixture
from .state_trace import TraceMismatch, compare_state_traces, trace_summary
from .strategy_ir import (
    analyze_strategy,
    prepare_strategy,
    validate_strategy_bundle,
)


def _add_project_setup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        help="strategy file; omit after the first setup",
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=Path(".nfi/project.json"),
        help="saved project file (default: .nfi/project.json)",
    )
    parser.add_argument("--class", dest="class_name")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--datadir", type=Path)
    parser.add_argument("--timerange")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--pair", action="append")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="accept detected paths and the previous-five-years default without prompting",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nfi-bte")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    fixture_upload = fixture_commands.add_parser(
        "upload",
        help="upload a hash-verified fixture or certification bundle to S3",
    )
    fixture_upload.add_argument("source", type=Path)
    fixture_upload.add_argument("destination", help="s3://bucket/key")
    fixture_upload.add_argument("--endpoint-url")
    fixture_download = fixture_commands.add_parser(
        "download",
        help="download and verify an S3 fixture or certification bundle",
    )
    fixture_download.add_argument("source", help="s3://bucket/key")
    fixture_download.add_argument("--output", "-o", type=Path, required=True)
    fixture_download.add_argument("--sha256")
    fixture_download.add_argument("--endpoint-url")

    probe = subcommands.add_parser(
        "probe",
        help="capture branch-reaching Full X7 official fixtures",
    )
    probe_commands = probe.add_subparsers(dest="probe_command", required=True)
    probe_capture = probe_commands.add_parser(
        "capture",
        help="run native and official lanes, then seal one v3 fixture",
    )
    probe_capture.add_argument("spec", type=Path)
    probe_capture.add_argument("--output-dir", type=Path, required=True)
    probe_capture.add_argument("--work-dir", type=Path, required=True)
    probe_capture.add_argument("--workers", type=int)
    probe_capture.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_FULL_X7_TIMEOUT_SECONDS,
    )

    universe = subcommands.add_parser(
        "universe",
        help="select and seal a strict release-grade pair universe",
    )
    universe_commands = universe.add_subparsers(
        dest="universe_command",
        required=True,
    )
    universe_select = universe_commands.add_parser(
        "select",
        help="select the first fully covered pairs in frozen candidate order",
    )
    universe_select.add_argument("--candidates", type=Path, required=True)
    universe_select.add_argument("--strategy", type=Path, required=True)
    universe_select.add_argument("--class-name", required=True)
    universe_select.add_argument("--config", type=Path, required=True)
    universe_select.add_argument("--data-dir", type=Path, required=True)
    universe_select.add_argument("--timerange", required=True)
    universe_select.add_argument("--output-dir", type=Path, required=True)
    universe_select.add_argument("--pair-count", type=int, default=80)
    universe_select.add_argument("--upstream-repository", required=True)
    universe_select.add_argument("--upstream-commit", required=True)
    universe_validate = universe_commands.add_parser(
        "validate",
        help="validate a sealed release input lock",
    )
    universe_validate.add_argument("lock", type=Path)
    universe_validate.add_argument("--pair-count", type=int, default=80)

    platform_evidence = subcommands.add_parser(
        "platform",
        help="measure and seal installed-wheel platform evidence",
    )
    platform_commands = platform_evidence.add_subparsers(
        dest="platform_command",
        required=True,
    )
    platform_benchmark = platform_commands.add_parser(
        "benchmark",
        help="run the portable native workload on this host",
    )
    platform_benchmark.add_argument("release_lock", type=Path)
    platform_benchmark.add_argument("--output-dir", type=Path, required=True)
    platform_benchmark.add_argument("--strategy", type=Path, required=True)
    platform_benchmark.add_argument("--class-name", required=True)
    platform_benchmark.add_argument("--config", type=Path, required=True)
    platform_benchmark.add_argument("--data-dir", type=Path, required=True)
    platform_benchmark.add_argument("--engine-markets", type=Path, required=True)
    platform_benchmark.add_argument("--wheel", type=Path, required=True)
    platform_benchmark.add_argument("--profile", type=Path, required=True)
    platform_benchmark.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_CERTIFICATION_REPETITIONS,
    )
    platform_benchmark.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_FULL_X7_TIMEOUT_SECONDS,
    )
    platform_benchmark.add_argument("--pair-count", type=int, default=20)
    platform_seal = platform_commands.add_parser(
        "seal",
        help="combine Windows, Linux, and macOS benchmark reports",
    )
    platform_seal.add_argument(
        "--report",
        action="append",
        type=Path,
        required=True,
    )
    platform_seal.add_argument("--output-dir", type=Path, required=True)

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
    reference_research = reference_commands.add_parser(
        "research",
        help="rerun one completed research run in pinned official Freqtrade",
    )
    reference_research.add_argument("run_directory", type=Path)
    reference_research.add_argument("--output-dir", type=Path, required=True)
    reference_research.add_argument(
        "--markets",
        type=Path,
        help="reuse a pinned raw reference market snapshot instead of capturing one",
    )
    reference_research.add_argument(
        "--no-market-capture",
        action="store_true",
        help="require --markets and keep every Docker invocation offline",
    )
    reference_research.add_argument(
        "--audit-timestamp-ms",
        action="append",
        type=int,
        help="retain callback state at this exact timestamp; may be repeated",
    )
    reference_research.add_argument("--timeout", type=int)
    reference_research.add_argument(
        "--memory-mode",
        choices=("normal", "certification-swap"),
        default="normal",
        help="allow measured Docker daemon swap only for continuous release certification",
    )
    reference_research.add_argument(
        "--swap-cap-gib",
        type=float,
        help="optional certification swap cap; never increases the detected daemon capacity",
    )
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

    init = subcommands.add_parser(
        "init",
        help="create a reusable NFI project with a small setup wizard",
    )
    _add_project_setup_arguments(init)
    init.add_argument(
        "--force",
        action="store_true",
        help="replace the saved project without deleting run data",
    )

    run = subcommands.add_parser(
        "run",
        help="run the saved project; first use starts the setup wizard",
    )
    _add_project_setup_arguments(run)
    run.add_argument("--workers", type=int)
    run.add_argument(
        "--recalibrate",
        action="store_true",
        help="remeasure this strategy/data workload before scheduling pair workers",
    )
    run.add_argument(
        "--prepare-only",
        action="store_true",
        help="prepare immutable vectors without requesting simulation",
    )
    run.add_argument(
        "--no-download",
        action="store_true",
        help="fail if required candle coverage is missing",
    )
    run.add_argument(
        "--history-coverage",
        choices=("available", "strict"),
        default="available",
        help="accept post-listing starts or require every pair at the range start",
    )
    run.add_argument(
        "--markets",
        type=Path,
        help="use an existing frozen CCXT market snapshot",
    )
    run.add_argument(
        "--no-market-download",
        action="store_true",
        help="require --markets instead of capturing public market metadata",
    )

    system = subcommands.add_parser("system", help="inspect and tune this computer")
    system_commands = system.add_subparsers(dest="system_command", required=True)
    system_inspect = system_commands.add_parser("inspect", help="print visible hardware resources")
    system_inspect.add_argument("--output", "-o", type=Path)
    system_docker = system_commands.add_parser(
        "docker",
        help="show Docker daemon resources and managed containers",
    )
    system_docker.add_argument("--output", "-o", type=Path)
    system_docker.add_argument(
        "--cleanup-stopped",
        action="store_true",
        help="remove only stopped containers owned by this project",
    )
    system_tune = system_commands.add_parser(
        "tune", help="create a hardware-bound execution profile"
    )
    system_tune.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path(".nfi/execution-profile.json"),
    )
    system_tune.add_argument(
        "--memory-cap-gib",
        type=float,
        help="optional hard cap; default resolves available host memory before each run",
    )
    system_tune.add_argument(
        "--spool-directory",
        type=Path,
        help="optional disk-backed directory for bounded-memory engine rows",
    )
    system_tune.add_argument(
        "--force",
        action="store_true",
        help="replace an existing hardware profile",
    )
    system_show = system_commands.add_parser("show", help="validate and print an execution profile")
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
    data_prepare.add_argument(
        "--startup-candles",
        type=int,
        default=0,
        help="record this many requested pre-timerange candles per timeframe",
    )
    data_prepare.add_argument(
        "--require-startup-coverage",
        action="store_true",
        help="download or fail instead of sealing Freqtrade-compatible startup shortfalls",
    )
    data_prepare.add_argument(
        "--history-coverage",
        choices=("strict", "available"),
        default="strict",
        help="strict requires the range start; available records later listings",
    )
    data_validate = data_commands.add_parser(
        "validate", help="verify every hash and coverage value in a data seal"
    )
    data_validate.add_argument("seal", type=Path)

    markets = subcommands.add_parser("markets", help="capture public CCXT market metadata")
    markets_commands = markets.add_subparsers(dest="markets_command", required=True)
    markets_capture = markets_commands.add_parser(
        "capture",
        help="freeze fee and precision metadata for selected pairs",
    )
    markets_capture.add_argument("--config", type=Path, required=True)
    markets_capture.add_argument("--pair", action="append")
    markets_capture.add_argument(
        "--leverage-tiers",
        type=Path,
        help="optional Freqtrade exchange leverage-tier JSON for exact futures liquidation",
    )
    markets_capture.add_argument("--output", "-o", type=Path, required=True)

    strategy = subcommands.add_parser("strategy", help="inspect and prepare strategy sources")
    strategy_commands = strategy.add_subparsers(dest="strategy_command", required=True)
    strategy_inspect = strategy_commands.add_parser(
        "inspect", help="emit static capability IR and exact diagnostics"
    )
    strategy_inspect.add_argument("source", type=Path)
    strategy_inspect.add_argument("--class", dest="class_name")
    strategy_inspect.add_argument("--output", "-o", type=Path)
    strategy_check = strategy_commands.add_parser(
        "check",
        help="check whether a new strategy revision has exact native callback lowerings",
    )
    strategy_check.add_argument("source", type=Path)
    strategy_check.add_argument("--class", dest="class_name")
    strategy_check.add_argument("--config", type=Path)
    strategy_check.add_argument("--trading-mode", choices=("spot", "futures", "margin"))
    strategy_check.add_argument("--output", "-o", type=Path)
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
    strategy_vectors = strategy_commands.add_parser(
        "vectors",
        help="execute batched vector methods for one or more pairs",
    )
    strategy_vectors.add_argument("source", type=Path)
    strategy_vectors.add_argument("--class", dest="class_name", required=True)
    strategy_vectors.add_argument("--config", type=Path, required=True)
    strategy_vectors.add_argument("--datadir", type=Path, required=True)
    strategy_vectors.add_argument("--timerange", required=True)
    strategy_vectors.add_argument("--pair", action="append", required=True)
    strategy_vectors.add_argument("--output-dir", type=Path, required=True)
    strategy_vectors.add_argument("--workers", type=int, default=1)
    strategy_vectors.add_argument("--cache-dir", type=Path)

    backtest = subcommands.add_parser(
        "backtest",
        help="prepare and run one checkpointed research backtest",
    )
    backtest.add_argument("source", type=Path)
    backtest.add_argument("--class", dest="class_name", required=True)
    backtest.add_argument("--config", type=Path, required=True)
    backtest.add_argument("--datadir", type=Path, required=True)
    backtest.add_argument("--timerange", required=True)
    backtest.add_argument("--pair", action="append")
    backtest.add_argument("--output-dir", type=Path, required=True)
    backtest.add_argument("--workers", type=int)
    backtest.add_argument(
        "--recalibrate",
        action="store_true",
        help="remeasure this strategy/data workload before scheduling pair workers",
    )
    backtest.add_argument("--cache-dir", type=Path, default=Path(".nfi/cache"))
    backtest.add_argument(
        "--markets",
        type=Path,
        help="frozen CCXT market snapshot required by the generic exact adapter",
    )
    backtest.add_argument(
        "--no-market-download",
        action="store_true",
        help="require --markets instead of capturing public CCXT metadata",
    )
    backtest.add_argument(
        "--registry",
        type=Path,
        default=Path(".nfi/runs.sqlite"),
        help="durable run index (default: .nfi/runs.sqlite)",
    )
    backtest.add_argument(
        "--profile",
        type=Path,
        default=Path(".nfi/execution-profile.json"),
    )
    backtest.add_argument(
        "--resume",
        action="store_true",
        help="reuse hash-validated completed stages in the output directory",
    )
    backtest.add_argument(
        "--prepare-only",
        action="store_true",
        help="stop successfully after immutable vector preparation",
    )
    backtest.add_argument(
        "--no-download",
        action="store_true",
        help="fail if required candle coverage is missing",
    )
    backtest.add_argument(
        "--history-coverage",
        choices=("available", "strict"),
        default="available",
        help="accept post-listing starts or require every pair at the range start",
    )

    confirm = subcommands.add_parser(
        "confirm",
        help="normalize and exact-compare an official Freqtrade export",
    )
    confirm.add_argument("run_directory", type=Path)
    confirm.add_argument("freqtrade_export", type=Path)
    confirm.add_argument("--output-dir", type=Path, required=True)
    confirm.add_argument("--strategy")

    runs = subcommands.add_parser("runs", help="inspect the durable research-run index")
    runs_commands = runs.add_subparsers(dest="runs_command", required=True)
    runs_list = runs_commands.add_parser("list", help="list recent runs")
    runs_list.add_argument("--registry", type=Path, default=Path(".nfi/runs.sqlite"))
    runs_list.add_argument("--limit", type=int, default=20)
    runs_show = runs_commands.add_parser("show", help="show one run and its report")
    runs_show.add_argument("run_id")
    runs_show.add_argument("--registry", type=Path, default=Path(".nfi/runs.sqlite"))

    batch = subcommands.add_parser("batch", help="run independent candidate jobs safely")
    batch.add_argument("manifest", type=Path)
    batch.add_argument("--output-dir", type=Path, required=True)
    batch.add_argument("--profile", type=Path, default=Path(".nfi/execution-profile.json"))
    batch.add_argument("--cache-dir", type=Path, default=Path(".nfi/cache"))
    batch.add_argument("--registry", type=Path, default=Path(".nfi/runs.sqlite"))
    batch.add_argument("--max-jobs", type=int)
    batch.add_argument("--resume", action="store_true")
    batch.add_argument("--no-download", action="store_true")

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
        "--vector-manifest",
        action="store_true",
        help="read a SHA-verified Feather vector manifest instead of expanded JSON",
    )
    engine_run.add_argument(
        "--events",
        type=Path,
        help="stream compact every-candle engine states as JSONL",
    )
    engine_run.add_argument(
        "--engine-profile",
        type=Path,
        help="write aggregate Rust input and simulation phase timings",
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

    certify = subcommands.add_parser(
        "certify",
        help="run release-grade exact parity and package a verified evidence bundle",
    )
    certify.add_argument(
        "manifest",
        type=Path,
        help="contract fixture manifest or Full X7 release-input-lock.json",
    )
    certify.add_argument(
        "--certification-profile",
        choices=("contract", "full-x7"),
        default="contract",
    )
    certify.add_argument("--output-dir", type=Path, required=True)
    certify.add_argument("--profile", type=Path)
    certify.add_argument("--strategy", type=Path)
    certify.add_argument("--class-name")
    certify.add_argument("--config", type=Path)
    certify.add_argument("--data-dir", type=Path)
    certify.add_argument("--engine-markets", type=Path)
    certify.add_argument("--reference-markets", type=Path)
    certify.add_argument("--wheel", type=Path)
    certify.add_argument("--swap-cap-gib", type=float)
    certify.add_argument(
        "--state-probe",
        action="append",
        type=Path,
        required=True,
        help="small branch-reaching fixture verified with full state; may be repeated",
    )
    certify.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_CERTIFICATION_REPETITIONS,
        help="initial engine/reference repetitions (default: 3; extends to 5 above 5% spread)",
    )
    certify.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_FULL_X7_TIMEOUT_SECONDS,
        help="timeout for each engine or official run in seconds",
    )
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
            elif args.fixture_command == "seal":
                manifest = seal_fixture(args.manifest)
                print(f"fixture sealed: {manifest['fixture_id']}")
            elif args.fixture_command == "upload":
                from .object_storage import upload_artifact

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
                from .object_storage import download_artifact

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

        if args.command_name == "universe":
            return _execute_universe(args)

        if args.command_name == "probe":
            from .probe_capture import capture_x7_probe

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

        if args.command_name == "platform":
            return _execute_platform(args)

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
            if args.reference_command == "research":
                from .research_reference import run_research_reference

                report = run_research_reference(
                    args.run_directory,
                    args.output_dir,
                    market_snapshot_path=args.markets,
                    capture_markets=not args.no_market_capture,
                    audit_timestamps_ms=args.audit_timestamp_ms,
                    timeout_seconds=args.timeout,
                    reference_memory_mode=args.memory_mode,
                    swap_cap_bytes=(
                        int(args.swap_cap_gib * 1024**3)
                        if args.swap_cap_gib is not None
                        else None
                    ),
                )
                print(
                    "official research parity: "
                    f"equal={report['exact_parity']}, "
                    f"trades={report['official_trade_surface'] is not None}, "
                    f"report={args.output_dir / 'run.json'}"
                )
                memory_verdict = report["container_memory"]["verdict"]
                if memory_verdict in {"oom_killed", "possible_oom", "near_limit"}:
                    print(
                        "reference container memory: "
                        f"{memory_verdict}, peak={report['container_memory']['peak_bytes']}, "
                        f"limit={report['container_memory']['limit_bytes']}",
                        file=sys.stderr,
                    )
                return 0 if report["complete"] else 1
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
            memory_verdict = report["container_memory"]["verdict"]
            if memory_verdict in {"oom_killed", "possible_oom", "near_limit"}:
                print(
                    "reference container memory: "
                    f"{memory_verdict}, peak={report['container_memory']['peak_bytes']}, "
                    f"limit={report['container_memory']['limit_bytes']}",
                    file=sys.stderr,
                )
            return 0 if report["complete"] else 1

        if args.command_name == "doctor":
            report = run_doctor(profile_path=args.profile)
            if args.output:
                write_json(args.output, report)
            print(
                f"doctor: {'healthy' if report['healthy'] else 'unhealthy'}; "
                + ", ".join(f"{check['name']}={check['status']}" for check in report["checks"])
            )
            return 0 if report["healthy"] else 1

        if args.command_name == "init":
            from .project_setup import initialize_project

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
            from .project_setup import (
                initialize_project,
                load_project,
                project_run_arguments,
            )

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
            return _execute_research_backtest(
                project_run_arguments(settings),
                workers=args.workers,
                resume=resume,
                prepare_only=args.prepare_only,
                download_missing=not args.no_download,
                market_metadata_path=args.markets,
                download_market_metadata=not args.no_market_download,
                recalibrate=args.recalibrate,
                history_coverage_policy=args.history_coverage,
            )

        if args.command_name == "system":
            if args.system_command == "inspect":
                hardware = inspect_hardware()
                if args.output:
                    write_json(args.output, hardware)
                print(json.dumps(hardware, ensure_ascii=False, indent=2))
                return 0
            if args.system_command == "docker":
                from .docker_resources import (
                    derive_docker_policy,
                    inspect_docker_daemon,
                )
                from .docker_runtime import (
                    cleanup_stopped_managed_containers,
                    list_managed_containers,
                )
                from .reference_runtime import ensure_docker_config

                docker_config = ensure_docker_config()
                cleaned = (
                    cleanup_stopped_managed_containers(docker_config=docker_config)
                    if args.cleanup_stopped
                    else []
                )
                daemon = inspect_docker_daemon(docker_config=docker_config)
                report = {
                    "schema_version": "1.0.0",
                    "daemon": daemon,
                    "policy": derive_docker_policy(daemon),
                    "managed_containers": list_managed_containers(
                        docker_config=docker_config
                    ),
                    "cleaned_stopped_containers": cleaned,
                }
                if args.output:
                    write_json(args.output, report)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            if args.system_command == "tune":
                if args.memory_cap_gib is not None and args.memory_cap_gib <= 0:
                    raise NfiBacktestError("--memory-cap-gib must be positive")
                if args.output.exists() and not args.force:
                    raise NfiBacktestError(
                        f"execution profile already exists: {args.output}; "
                        "use --force to recalibrate"
                    )
                profile = create_execution_profile(
                    args.output,
                    memory_cap_bytes=(
                        int(args.memory_cap_gib * GIB)
                        if args.memory_cap_gib is not None
                        else None
                    ),
                    spool_directory=args.spool_directory,
                )
                limits = profile["limits"]
                print(
                    f"execution profile -> {args.output}; "
                    f"cpu_process_limit={limits['cpu_process_limit']}, "
                    f"memory_cap={limits['memory_cap_bytes']}; "
                    "workload process counts are measured on the first run"
                )
                return 0
            profile = load_execution_profile(args.profile)
            print(json.dumps(profile, ensure_ascii=False, indent=2))
            return 0

        if args.command_name == "data":
            from .data_seal import prepare_data, validate_data_seal

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

        if args.command_name == "markets":
            from .config_loader import freeze_pairlist, sanitize_config
            from .market_snapshot import capture_market_snapshot

            loaded = load_effective_config(args.config)
            pairlist = freeze_pairlist(loaded["config"], resolved_pairs=args.pair)
            config = sanitize_config(loaded["config"])
            if not isinstance(config, dict):
                raise NfiBacktestError("effective config must be an object")
            leverage_tiers = read_json(args.leverage_tiers) if args.leverage_tiers else None
            report = capture_market_snapshot(
                config,
                pairlist["pairs"],
                args.output,
                leverage_tiers=leverage_tiers,
            )
            print(
                f"markets captured: exchange={report['exchange']}, "
                f"pairs={len(report['pairs'])}, sha256={report['sha256']} -> "
                f"{args.output}"
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
            if args.strategy_command == "check":
                from .strategy_compatibility import check_strategy_compatibility

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
                from .vector_runtime import prepare_vector_signals

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

        if args.command_name == "backtest":
            return _execute_research_backtest(
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
            )

        if args.command_name == "confirm":
            from .confirmation import confirm_research_run

            report = confirm_research_run(
                args.run_directory,
                args.freqtrade_export,
                args.output_dir,
                strategy=args.strategy,
            )
            if report["equal"]:
                print(
                    f"official exact parity: run={report['run_id']} -> "
                    f"{args.output_dir / 'confirmation.json'}"
                )
                return 0
            difference = report["difference"]
            print(
                f"official parity mismatch at {difference['path']}: "
                f"{difference['reason']} -> {args.output_dir / 'confirmation.json'}",
                file=sys.stderr,
            )
            return 1

        if args.command_name == "runs":
            from .run_registry import RunRegistry

            with RunRegistry(args.registry) as registry:
                if args.runs_command == "list":
                    records = registry.list(limit=args.limit)
                    print(json.dumps(records, ensure_ascii=False, indent=2))
                else:
                    print(
                        json.dumps(
                            registry.show(args.run_id),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
            return 0

        if args.command_name == "batch":
            from .batch_runner import run_batch

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

        if args.command_name == "certify":
            return _execute_certification(args)

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


def _execute_universe(args: argparse.Namespace) -> int:
    from .release_inputs import (
        select_release_universe,
        validate_release_input_lock,
    )

    if args.universe_command == "validate":
        document = read_json(args.lock)
        validate_release_input_lock(
            document,
            required_pair_count=args.pair_count,
        )
        print(
            "release universe valid: "
            f"pairs={document['scope']['pair_count']}, "
            f"identity={document['identity_sha256']}"
        )
        return 0
    lock = select_release_universe(
        candidates_path=args.candidates,
        strategy_path=args.strategy,
        class_name=args.class_name,
        config_path=args.config,
        data_directory=args.data_dir,
        timerange=args.timerange,
        output_directory=args.output_dir,
        pair_count=args.pair_count,
        upstream_repository=args.upstream_repository,
        upstream_commit=args.upstream_commit,
    )
    print(
        "release universe sealed: "
        f"pairs={lock['scope']['pair_count']}, "
        f"data={lock['data']['aggregate_sha256']} -> "
        f"{args.output_dir / 'release-input-lock.json'}"
    )
    return 0


def _execute_certification(args: argparse.Namespace) -> int:
    if args.certification_profile == "full-x7":
        from .full_x7_certification import run_full_x7_certification

        required = {
            "--profile": args.profile,
            "--strategy": args.strategy,
            "--class-name": args.class_name,
            "--config": args.config,
            "--data-dir": args.data_dir,
            "--engine-markets": args.engine_markets,
            "--reference-markets": args.reference_markets,
            "--wheel": args.wheel,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise SpecValidationError(
                "Full X7 certification requires " + ", ".join(missing)
            )
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
            swap_cap_bytes=(
                int(args.swap_cap_gib * 1024**3)
                if args.swap_cap_gib is not None
                else None
            ),
        )
        print(
            f"Full X7 certification: status={report['status']}, "
            f"speedup={report['gates']['speed']['observed_speedup']:.3f}x, "
            f"bundle_sha256={report['bundle']['archive']['sha256']} -> "
            f"{args.output_dir / 'full-x7-certification.json'}"
        )
        return 0 if report["release_certified"] else 1

    from .certification import run_certification

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


def _execute_platform(args: argparse.Namespace) -> int:
    from .platform_benchmark import (
        run_platform_benchmark,
        seal_platform_evidence,
    )

    if args.platform_command == "seal":
        evidence = seal_platform_evidence(args.report, args.output_dir)
        print(
            "platform evidence sealed: "
            f"result={evidence['result_sha256']}, "
            f"bundle={evidence['bundle']['archive']['sha256']} -> "
            f"{args.output_dir / 'platform-evidence.json'}"
        )
        return 0
    report = run_platform_benchmark(
        args.release_lock,
        args.output_dir,
        strategy_path=args.strategy,
        class_name=args.class_name,
        config_path=args.config,
        data_directory=args.data_dir,
        engine_market_snapshot=args.engine_markets,
        wheel_path=args.wheel,
        execution_profile_path=args.profile,
        repetitions=args.runs,
        timeout_seconds=args.timeout,
        pair_count=args.pair_count,
    )
    print(
        "platform benchmark: "
        f"complete={report['complete']}, "
        f"median={report['measurement']['wall_time_seconds']['median']:.3f}s, "
        f"peak_rss={report['measurement']['peak_rss_bytes']['maximum']} -> "
        f"{args.output_dir / 'platform-benchmark.json'}"
    )
    return 0 if report["complete"] else 1


def _execute_research_backtest(
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
) -> int:
    """Run the existing research contract for advanced and wizard-backed commands."""
    from .research_runner import run_research_backtest

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


if __name__ == "__main__":
    raise SystemExit(main())
