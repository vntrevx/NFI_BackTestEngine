"""Official-reference and market-snapshot command orchestration."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any

from ..canonical import read_json
from ..config_loader import load_effective_config
from ..errors import NfiBacktestError
from ..fixture import sha256_file
from ..reference_runtime import (
    capture_reference_markets,
    load_reference_leverage_tiers,
    run_reference_fixture,
)

COMMAND_NAMES = frozenset({"reference", "markets"})


def execute(
    args: argparse.Namespace,
    *,
    market_capture: Callable[[argparse.Namespace], int] | None = None,
) -> int:
    """Execute an official-reference or market-snapshot command."""
    if args.command_name == "markets":
        return (market_capture or execute_market_capture)(args)
    if args.command_name != "reference":
        raise AssertionError(f"unhandled reference command: {args.command_name}")

    if args.reference_command == "semantic-profile":
        from ..freqtrade_semantic_profile import write_current_freqtrade_semantic_profile

        profile = write_current_freqtrade_semantic_profile(args.output)
        print(
            "Freqtrade semantic profile: "
            f"version={profile['reference']['version']}, "
            f"fingerprint={profile['fingerprint']} -> {args.output}"
        )
        return 0
    if args.reference_command == "scheduler-contract":
        from ..scheduler_contract import write_scheduler_contract

        contract = write_scheduler_contract(args.semantic_profile, args.output)
        print(
            "Freqtrade scheduler contract: "
            f"fingerprint={contract['fingerprint']} -> {args.output}"
        )
        return 0
    if args.reference_command == "execution-contract":
        from ..execution_contract import write_execution_contract

        contract = write_execution_contract(
            args.semantic_profile,
            args.scheduler_contract,
            args.output,
        )
        print(
            "Freqtrade Spot execution contract: "
            f"fingerprint={contract['fingerprint']} -> {args.output}"
        )
        return 0
    if args.reference_command == "semantic-observe":
        from ..semantic_observer import project_official_semantic_trace

        report = project_official_semantic_trace(
            args.manifest,
            args.profile,
            args.output_trace,
            report_path=args.output_report,
        )
        print(
            "official semantic observer: "
            f"events={report['projected_trace']['event_count']}, "
            f"stream={report['projected_trace']['stream_hash']} -> {args.output_report}"
        )
        return 0
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
        return execute_research_reference(args)
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


def execute_market_capture(
    args: argparse.Namespace,
    *,
    load_config: Callable[[Any], dict[str, Any]] = load_effective_config,
    load_leverage_tiers: Callable[[list[str]], dict[str, Any]] = (
        load_reference_leverage_tiers
    ),
) -> int:
    """Capture one sealed market snapshot, including pinned futures tiers."""
    from ..config_loader import freeze_pairlist, sanitize_config
    from ..market_snapshot import capture_market_catalog, capture_market_snapshot

    loaded = load_config(args.config)
    config = sanitize_config(loaded["config"])
    if not isinstance(config, dict):
        raise NfiBacktestError("effective config must be an object")
    # Direct library callers from the original capture-only CLI do not carry
    # the newer subcommand discriminator. Treat that shape as `capture`;
    # argparse still supplies an explicit value for every command-line call.
    if getattr(args, "markets_command", "capture") == "catalog":
        report = capture_market_catalog(config, args.output)
        print(
            f"market catalog captured: exchange={report['exchange']}, "
            f"pairs={len(report['pairs'])}, sha256={report['sha256']} -> "
            f"{args.output}"
        )
        return 0

    pairlist = freeze_pairlist(loaded["config"], resolved_pairs=args.pair)

    leverage_tier_source = None
    if args.leverage_tiers:
        tier_path = args.leverage_tiers.resolve()
        leverage_tiers = read_json(tier_path)
        leverage_tier_source = {
            "kind": "sealed-file",
            "path": str(tier_path),
            "bytes": tier_path.stat().st_size,
            "sha256": sha256_file(tier_path),
        }
    elif config.get("trading_mode") == "futures":
        exchange = config.get("exchange")
        exchange_name = str(exchange.get("name", "")).lower() if isinstance(exchange, dict) else ""
        if exchange_name != "binance":
            raise NfiBacktestError(
                "automatic futures leverage-tier capture requires Binance; "
                "provide --leverage-tiers for this exchange"
            )
        captured_tiers = load_leverage_tiers(pairlist["pairs"])
        leverage_tiers = captured_tiers["tiers"]
        leverage_tier_source = captured_tiers["source"]
    else:
        leverage_tiers = None

    report = capture_market_snapshot(
        config,
        pairlist["pairs"],
        args.output,
        leverage_tiers=leverage_tiers,
        leverage_tier_source=leverage_tier_source,
    )
    print(
        f"markets captured: exchange={report['exchange']}, "
        f"pairs={len(report['pairs'])}, sha256={report['sha256']} -> "
        f"{args.output}"
    )
    return 0


def execute_research_reference(args: argparse.Namespace) -> int:
    from ..research_reference import run_research_reference
    from ..result_report import format_terminal_summary, write_result_presentation

    report = run_research_reference(
        args.run_directory,
        args.output_dir,
        market_snapshot_path=args.markets,
        capture_markets=not args.no_market_capture,
        audit_timestamps_ms=args.audit_timestamp_ms,
        timeout_seconds=args.timeout,
        reference_memory_mode=args.memory_mode,
        reference_storage_mode=args.storage_mode,
        swap_cap_bytes=(
            int(args.swap_cap_gib * 1024**3) if args.swap_cap_gib is not None else None
        ),
    )
    summary = write_result_presentation(
        args.run_directory,
        verification=report,
        verification_path=args.output_dir / "run.json",
    )
    print(
        "official research parity: "
        f"equal={report['exact_parity']}, "
        f"trades={report['official_trade_surface'] is not None}, "
        f"report={args.output_dir / 'run.json'}"
    )
    print(
        format_terminal_summary(
            summary,
            args.run_directory,
            include_breakdowns=args.full_report,
        )
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
