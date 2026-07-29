"""Command-line entry point for Phase 0 and Phase 1 tools."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .benchmark import run_benchmark
from .commands import (
    certify as certify_commands,
)
from .commands import (
    clean as clean_commands,
)
from .commands import (
    fixture as fixture_commands,
)
from .commands import (
    reference as reference_commands,
)
from .commands import (
    release as release_commands,
)
from .commands import (
    report as report_commands,
)
from .commands import (
    run as run_commands,
)
from .commands import (
    system as system_commands,
)
from .config_loader import load_effective_config
from .errors import NfiBacktestError
from .hardware import create_execution_profile
from .parity import ParityMismatch
from .product_contract import (
    DEFAULT_CERTIFICATION_REPETITIONS,
    DEFAULT_FULL_X7_TIMEOUT_SECONDS,
)
from .reference_runtime import load_reference_leverage_tiers
from .state_trace import TraceMismatch


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


def _add_full_report_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--full-report",
        action="store_true",
        help=(
            "append complete pair, entry-tag, Signal-tag, Grind-level, "
            "and exit-reason tables to terminal output"
        ),
    )


def _add_fallback_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--fallback",
        choices=("ask", "official", "disabled"),
        default="ask",
        help=(
            "when Native safely blocks: ask interactively, run pinned official "
            "Freqtrade, or remain blocked (default: ask)"
        ),
    )
    parser.add_argument(
        "--fallback-timeout",
        type=int,
        help="optional timeout for an official fallback in seconds",
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
    universe_discover = universe_commands.add_parser(
        "discover",
        help="derive historically eligible pairs from a frozen market snapshot",
    )
    universe_discover.add_argument("--config", type=Path, required=True)
    universe_discover.add_argument("--markets", type=Path, required=True)
    universe_discover.add_argument("--timerange", required=True)
    universe_discover.add_argument("--output", type=Path, required=True)
    universe_discover.add_argument(
        "--history-coverage",
        choices=("strict", "listing-aware"),
        default="strict",
        help="release history contract (listing-aware is Futures-only)",
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
    universe_select.add_argument(
        "--history-coverage",
        choices=("strict", "listing-aware"),
        default="strict",
        help="must match the frozen candidate discovery contract",
    )
    universe_configure = universe_commands.add_parser(
        "configure",
        help="write a download config from the first frozen candidates",
    )
    universe_configure.add_argument("--candidates", type=Path, required=True)
    universe_configure.add_argument("--config", type=Path, required=True)
    universe_configure.add_argument("--timerange", required=True)
    universe_configure.add_argument("--output", type=Path, required=True)
    universe_configure.add_argument("--pair-count", type=int, default=80)
    universe_configure.add_argument(
        "--history-coverage",
        choices=("strict", "listing-aware"),
        default="strict",
    )
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
    platform_fixture_benchmark = platform_commands.add_parser(
        "fixture-benchmark",
        help="repeat one sealed exact fixture with the installed release wheel",
    )
    platform_fixture_benchmark.add_argument("manifest", type=Path)
    platform_fixture_benchmark.add_argument("--output-dir", type=Path, required=True)
    platform_fixture_benchmark.add_argument("--wheel", type=Path, required=True)
    platform_fixture_benchmark.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_CERTIFICATION_REPETITIONS,
    )
    platform_fixture_benchmark.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_FULL_X7_TIMEOUT_SECONDS,
    )
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
        "--storage-mode",
        choices=("in-memory", "spooled"),
        default="spooled",
        help="use bounded Arrow storage by default; in-memory is a diagnostic baseline",
    )
    reference_research.add_argument(
        "--swap-cap-gib",
        type=float,
        help="optional certification swap cap; never increases the detected daemon capacity",
    )
    _add_full_report_argument(reference_research)
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

    clean = subcommands.add_parser(
        "clean",
        help="audit or safely reclaim managed .nfi disk use",
    )
    clean_mode = clean.add_mutually_exclusive_group(required=True)
    clean_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="write a fresh audit without deleting files",
    )
    clean_mode.add_argument(
        "--apply",
        action="store_true",
        help="write a fresh audit, then delete only its safe candidates",
    )
    clean.add_argument(
        "--root",
        type=Path,
        default=Path(".nfi"),
        help="managed directory named .nfi (default: .nfi)",
    )
    clean.add_argument(
        "--output",
        "-o",
        type=Path,
        help="audit JSON path inside --root (default: ROOT/clean-audit.json)",
    )
    clean.add_argument(
        "--result",
        type=Path,
        help="apply receipt inside --root (default: ROOT/clean-result.json)",
    )
    clean.add_argument(
        "--preserve",
        action="append",
        type=Path,
        default=[],
        help="entry inside --root to protect explicitly; may be repeated",
    )
    clean.add_argument(
        "--no-runtime-probes",
        action="store_true",
        help="dry-run only: skip service/container probes and report fail-closed",
    )
    clean.add_argument(
        "--include-completed",
        action="store_true",
        help="also select completed runs; evidence, Oracle, ZIP, and preserved runs stay protected",
    )

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
    run.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "run pinned quick-level official verification after Native completion; "
            "otherwise ask only on an interactive terminal"
        ),
    )
    run.add_argument(
        "--verification-timeout",
        type=int,
        help="optional timeout for the consented official verification in seconds",
    )
    _add_fallback_arguments(run)
    run.add_argument(
        "--open-report",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=("open report.html after completion; otherwise ask only on an interactive terminal"),
    )
    _add_full_report_argument(run)

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
    markets_catalog = markets_commands.add_parser(
        "catalog",
        help="freeze all current markets matching the configured release mode",
    )
    markets_catalog.add_argument("--config", type=Path, required=True)
    markets_catalog.add_argument("--output", "-o", type=Path, required=True)

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
    strategy_check.add_argument(
        "--verification-ledger",
        type=Path,
        help="append the compatibility result to this verification ledger",
    )
    strategy_check.add_argument(
        "--upstream-repository",
        help="upstream repository identity recorded with the checked source",
    )
    strategy_check.add_argument(
        "--upstream-commit",
        help="40-character upstream commit recorded with the checked source",
    )
    strategy_check.add_argument(
        "--strategy-version",
        help="upstream strategy version recorded with the checked source",
    )
    strategy_diff = strategy_commands.add_parser(
        "diff",
        help="classify AST/IR-relevant changes between two strategy revisions",
    )
    strategy_diff.add_argument("old_source", type=Path)
    strategy_diff.add_argument("new_source", type=Path)
    strategy_diff.add_argument("--class", dest="class_name")
    strategy_diff.add_argument("--output", "-o", type=Path)
    strategy_qualify = strategy_commands.add_parser(
        "qualify",
        help="gate latest_checked to quick_verified with a branch-reaching proof",
    )
    strategy_qualify.add_argument("compatibility_report", type=Path)
    strategy_qualify.add_argument("strategy_diff", type=Path)
    strategy_qualify.add_argument("--branch-proof", type=Path)
    strategy_qualify.add_argument("--output", "-o", type=Path)
    strategy_state_machine = strategy_commands.add_parser(
        "state-machine",
        help="compile bounded stateful callbacks into generic VM IR",
    )
    strategy_state_machine.add_argument("source", type=Path)
    strategy_state_machine.add_argument("--class", dest="class_name")
    strategy_state_machine.add_argument("--output", "-o", type=Path, required=True)
    strategy_shadow_gate = strategy_commands.add_parser(
        "shadow-gate",
        help="compare independent legacy and generic state-machine executions",
    )
    strategy_shadow_gate.add_argument("legacy_run", type=Path)
    strategy_shadow_gate.add_argument("candidate_run", type=Path)
    strategy_shadow_gate.add_argument("--legacy-trace", type=Path, required=True)
    strategy_shadow_gate.add_argument("--candidate-trace", type=Path, required=True)
    strategy_shadow_gate.add_argument("--branch-proof", type=Path, required=True)
    strategy_shadow_gate.add_argument("--output", "-o", type=Path, required=True)
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
    _add_fallback_arguments(backtest)
    _add_full_report_argument(backtest)

    confirm = subcommands.add_parser(
        "confirm",
        help="normalize and exact-compare an official Freqtrade export",
    )
    confirm.add_argument("run_directory", type=Path)
    confirm.add_argument("freqtrade_export", type=Path)
    confirm.add_argument("--output-dir", type=Path, required=True)
    confirm.add_argument("--strategy")
    _add_full_report_argument(confirm)

    report = subcommands.add_parser(
        "report",
        help="build a readable HTML, JSON summary, and trades CSV for a research run",
    )
    report.add_argument("run_directory", type=Path)
    report.add_argument(
        "--confirmation",
        type=Path,
        help="optional confirmation.json or official reference run.json",
    )
    report.add_argument(
        "--verification-ledger",
        type=Path,
        help="append a derived verification-status.html from an existing ledger",
    )
    _add_full_report_argument(report)

    runs = subcommands.add_parser("runs", help="inspect the durable research-run index")
    runs_commands = runs.add_subparsers(dest="runs_command", required=True)
    runs_list = runs_commands.add_parser("list", help="list recent runs")
    runs_list.add_argument("--registry", type=Path, default=Path(".nfi/runs.sqlite"))
    runs_list.add_argument("--limit", type=int, default=20)
    runs_list.add_argument(
        "--json",
        action="store_true",
        help="emit the machine-readable registry records",
    )
    runs_list.add_argument(
        "--verification-ledger",
        type=Path,
        help="include latest checked, quick, and release verification states",
    )
    runs_show = runs_commands.add_parser("show", help="show one run and its report")
    runs_show.add_argument("run_id")
    runs_show.add_argument("--registry", type=Path, default=Path(".nfi/runs.sqlite"))
    runs_show.add_argument(
        "--json",
        action="store_true",
        help="emit the machine-readable registry record and run report",
    )
    _add_full_report_argument(runs_show)

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
    certify.add_argument(
        "--reference-markets",
        type=Path,
        help="reuse a frozen raw oracle snapshot; omit to capture it during warmup",
    )
    certify.add_argument(
        "--official-oracle",
        type=Path,
        help=(
            "import one completed continuous official Full X7 reference directory "
            "instead of running it again"
        ),
    )
    certify.add_argument(
        "--resume",
        action="store_true",
        help="resume completed Full X7 stages from the selected output directory",
    )
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
        help=("extends to 5 above 5%% spread; native default 3, continuous official oracle once"),
    )
    certify.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_FULL_X7_TIMEOUT_SECONDS,
        help="timeout for each engine or official run in seconds",
    )

    release = subcommands.add_parser(
        "release",
        help="combine independently certified mode and platform evidence",
    )
    release_commands = release.add_subparsers(
        dest="release_command",
        required=True,
    )
    release_combine = release_commands.add_parser(
        "combine",
        help="bind spot and futures certificates into one Full X7 release",
    )
    release_combine.add_argument("--spot-certificate", type=Path, required=True)
    release_combine.add_argument("--futures-certificate", type=Path, required=True)
    release_combine.add_argument(
        "--platform-evidence",
        action="append",
        type=Path,
        default=[],
        help="sealed three-OS evidence for one mode; repeat for spot and futures",
    )
    release_combine.add_argument("--output-dir", type=Path, required=True)
    release_gate = release_commands.add_parser(
        "gate",
        help="bind a build-once candidate to host and three-OS certificates",
    )
    release_gate.add_argument("--candidate-dir", type=Path, required=True)
    release_gate.add_argument("--certificate", type=Path, required=True)
    release_gate.add_argument("--certificate-evidence", type=Path, required=True)
    release_gate.add_argument("--platform-evidence", type=Path, required=True)
    release_gate.add_argument("--candidate-commit", required=True)
    release_gate.add_argument("--output-dir", type=Path, required=True)
    release_combined_gate = release_commands.add_parser(
        "gate-combined",
        help="seal a public candidate after both modes and three OSes certify it",
    )
    release_combined_gate.add_argument(
        "--candidate-dir",
        type=Path,
        required=True,
    )
    release_combined_gate.add_argument(
        "--combined-release",
        type=Path,
        required=True,
        help="full-x7-release-result.json produced by release combine",
    )
    release_combined_gate.add_argument("--candidate-commit", required=True)
    release_combined_gate.add_argument("--output-dir", type=Path, required=True)
    release_combined_verify = release_commands.add_parser(
        "verify-combined",
        help="verify the exact public Spot+Futures release asset set",
    )
    release_combined_verify.add_argument(
        "--release-dir",
        type=Path,
        required=True,
    )
    release_combined_verify.add_argument("--candidate-commit")

    contract = subcommands.add_parser(
        "contract",
        help="verify versioned, read-only regression contracts",
    )
    contract_commands = contract.add_subparsers(
        dest="contract_command",
        required=True,
    )
    contract_verify = contract_commands.add_parser(
        "verify",
        help="verify repository evidence and published release assets",
    )
    contract_verify.add_argument("--manifest", type=Path)
    contract_verify.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="repository root containing the referenced evidence (default: current directory)",
    )
    contract_verify.add_argument(
        "--release-assets",
        action="append",
        default=[],
        metavar="TAG=DIR",
        help="verify one release from an existing asset directory instead of downloading it",
    )
    contract_verify.add_argument(
        "--offline",
        action="store_true",
        help="verify repository evidence and pin release identities without downloading assets",
    )
    contract_verify.add_argument("--output", type=Path)
    return parser


def _dispatch_command(
    args: argparse.Namespace,
    *,
    benchmark_command: list[str] | None,
) -> int:
    """Route parsed arguments to one behavior-preserving command module."""
    command_name = args.command_name
    if command_name in fixture_commands.COMMAND_NAMES:
        return fixture_commands.execute(
            args,
            benchmark_command=benchmark_command,
            run_benchmark_service=run_benchmark,
        )
    if command_name in reference_commands.COMMAND_NAMES:
        return reference_commands.execute(args, market_capture=_execute_market_capture)
    if command_name in report_commands.COMMAND_NAMES:
        return report_commands.execute(args)
    if command_name in run_commands.COMMAND_NAMES:
        return run_commands.execute(args)
    if command_name in system_commands.COMMAND_NAMES:
        return system_commands.execute(
            args,
            create_profile=create_execution_profile,
        )
    if command_name in clean_commands.COMMAND_NAMES:
        return clean_commands.execute(args)
    if command_name in certify_commands.COMMAND_NAMES:
        return certify_commands.execute(args)
    if command_name in release_commands.COMMAND_NAMES:
        return release_commands.execute(args)
    raise AssertionError(f"unhandled command: {command_name}")


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    benchmark_command: list[str] | None = None
    if raw_args[:1] == ["benchmark"] and "--" in raw_args:
        separator = raw_args.index("--")
        benchmark_command = raw_args[separator + 1 :]
        raw_args = raw_args[:separator]
    args = build_parser().parse_args(raw_args)
    try:
        return _dispatch_command(args, benchmark_command=benchmark_command)
    except ParityMismatch as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except TraceMismatch as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (NfiBacktestError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


# Compatibility wrappers for callers that used the original private helpers.
def _execute_universe(args: argparse.Namespace) -> int:
    return release_commands.execute_universe(args)


def _execute_certification(args: argparse.Namespace) -> int:
    return certify_commands.execute(args)


def _execute_release(args: argparse.Namespace) -> int:
    return release_commands.execute_release(args)


def _execute_regression_contract(args: argparse.Namespace) -> int:
    return release_commands.execute_regression_contract(args)


def _execute_platform(args: argparse.Namespace) -> int:
    return release_commands.execute_platform(args)


def _execute_market_capture(args: argparse.Namespace) -> int:
    """Preserve the original private market-capture entry point."""
    return reference_commands.execute_market_capture(
        args,
        load_config=load_effective_config,
        load_leverage_tiers=load_reference_leverage_tiers,
    )


def _execute_research_reference(args: argparse.Namespace) -> int:
    return reference_commands.execute_research_reference(args)


def _execute_confirmation(args: argparse.Namespace) -> int:
    return report_commands.execute_confirmation(args)


def _execute_result_report(args: argparse.Namespace) -> int:
    return report_commands.execute_result_report(args)


def _execute_run_registry(args: argparse.Namespace) -> int:
    return report_commands.execute_run_registry(args)


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
    full_report: bool,
    print_summary: bool = True,
) -> int:
    return run_commands.execute_research_backtest(
        arguments,
        workers=workers,
        resume=resume,
        prepare_only=prepare_only,
        download_missing=download_missing,
        market_metadata_path=market_metadata_path,
        download_market_metadata=download_market_metadata,
        recalibrate=recalibrate,
        history_coverage_policy=history_coverage_policy,
        full_report=full_report,
        print_summary=print_summary,
    )


if __name__ == "__main__":
    raise SystemExit(main())
