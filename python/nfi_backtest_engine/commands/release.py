"""Release-input, platform-evidence, and release-gate command orchestration."""

from __future__ import annotations

import argparse

from ..canonical import read_json, write_json

COMMAND_NAMES = frozenset({"universe", "platform", "release", "contract"})


def execute(args: argparse.Namespace) -> int:
    """Execute a release lifecycle command."""
    if args.command_name == "universe":
        return execute_universe(args)
    if args.command_name == "platform":
        return execute_platform(args)
    if args.command_name == "release":
        return execute_release(args)
    if args.command_name == "contract":
        return execute_regression_contract(args)
    raise AssertionError(f"unhandled release command: {args.command_name}")


def execute_universe(args: argparse.Namespace) -> int:
    from ..release_inputs import (
        discover_release_universe,
        materialize_release_candidate_config,
        select_release_universe,
        validate_release_input_lock,
    )

    if args.universe_command == "discover":
        report = discover_release_universe(
            config_path=args.config,
            market_snapshot_path=args.markets,
            timerange=args.timerange,
            destination=args.output,
            history_coverage_policy=args.history_coverage,
        )
        print(
            "release candidates discovered: "
            f"mode={report['mode_contract']}, pairs={len(report['pairs'])}, "
            f"rejected={len(report['rejected'])} -> {args.output}"
        )
        return 0
    if args.universe_command == "configure":
        report = materialize_release_candidate_config(
            candidates_path=args.candidates,
            config_path=args.config,
            timerange=args.timerange,
            destination=args.output,
            pair_count=args.pair_count,
            history_coverage_policy=args.history_coverage,
        )
        print(
            "release candidate config written: "
            f"mode={report['mode_contract']}, pairs={report['pair_count']}, "
            f"config={report['config_sha256']} -> {args.output}"
        )
        return 0
    if args.universe_command == "validate":
        document = read_json(args.lock)
        validate_release_input_lock(
            document,
            required_pair_count=args.pair_count,
        )
        print(
            "release universe valid: "
            f"mode={document['scope'].get('mode_contract', 'binance-spot')}, "
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
        history_coverage_policy=args.history_coverage,
    )
    print(
        "release universe sealed: "
        f"mode={lock['scope']['mode_contract']}, "
        f"pairs={lock['scope']['pair_count']}, "
        f"data={lock['data']['aggregate_sha256']} -> "
        f"{args.output_dir / 'release-input-lock.json'}"
    )
    return 0


def execute_platform(args: argparse.Namespace) -> int:
    from ..platform_benchmark import (
        run_platform_benchmark,
        run_platform_fixture_benchmark,
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
    if args.platform_command == "fixture-benchmark":
        report = run_platform_fixture_benchmark(
            args.manifest,
            args.output_dir,
            wheel_path=args.wheel,
            repetitions=args.runs,
            timeout_seconds=args.timeout,
        )
        print(
            "platform exact fixture: "
            f"complete={report['complete']}, "
            f"median={report['measurement']['wall_time_seconds']['median']:.3f}s, "
            f"peak_rss={report['measurement']['peak_rss_bytes']['maximum']} -> "
            f"{args.output_dir / 'platform-benchmark.json'}"
        )
        return 0 if report["complete"] else 1
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


def execute_release(args: argparse.Namespace) -> int:
    if args.release_command == "combine":
        from ..combined_release import combine_full_x7_release

        report = combine_full_x7_release(
            spot_certificate_path=args.spot_certificate,
            futures_certificate_path=args.futures_certificate,
            platform_evidence_paths=args.platform_evidence,
            output_directory=args.output_dir,
        )
        print(
            f"Full X7 release: status={report['status']}, "
            f"platform_modes={len(report['platform_evidence'])}/2, "
            f"bundle_sha256={report['bundle']['archive']['sha256']} -> "
            f"{args.output_dir / 'full-x7-release.json'}"
        )
        return 0 if report["release_certified"] else 1
    if args.release_command == "gate":
        from ..release_gate import seal_release_gate

        report = seal_release_gate(
            candidate_directory=args.candidate_dir,
            certificate_path=args.certificate,
            certificate_evidence_path=args.certificate_evidence,
            platform_evidence_path=args.platform_evidence,
            candidate_commit=args.candidate_commit,
            output_directory=args.output_dir,
        )
        print(
            f"release gate: status={report['status']}, "
            f"commit={report['candidate_commit']}, "
            f"assets={len(report['sealed_assets'])} -> "
            f"{args.output_dir / 'RELEASE-SHA256SUMS.txt'}"
        )
        return 0
    if args.release_command == "gate-combined":
        from ..combined_release import seal_combined_release_candidate

        report = seal_combined_release_candidate(
            candidate_directory=args.candidate_dir,
            combined_release_result_path=args.combined_release,
            candidate_commit=args.candidate_commit,
            output_directory=args.output_dir,
        )
        print(
            f"combined release gate: status={report['status']}, "
            f"commit={report['candidate_commit']}, "
            f"distributions={len(report['distributions'])} -> "
            f"{args.output_dir / 'RELEASE-SHA256SUMS.txt'}"
        )
        return 0
    if args.release_command == "verify-combined":
        from ..combined_release import verify_combined_release_candidate

        report = verify_combined_release_candidate(
            args.release_dir,
            expected_commit=args.candidate_commit,
        )
        print(
            f"combined release valid: commit={report['candidate_commit']}, "
            f"assets=10, version={report['package_version']}"
        )
        return 0
    raise AssertionError(f"unhandled release command: {args.release_command}")


def execute_regression_contract(args: argparse.Namespace) -> int:
    from ..regression_contract import (
        parse_release_asset_roots,
        verify_regression_contract,
    )

    if args.contract_command != "verify":
        raise AssertionError(f"unhandled contract command: {args.contract_command}")
    report = verify_regression_contract(
        args.manifest,
        repository_root=args.root,
        release_asset_roots=parse_release_asset_roots(args.release_assets),
        fetch_release_assets=not args.offline,
    )
    if args.output:
        write_json(args.output, report)
    checks = report["checks"]
    print(
        "regression contract valid: "
        f"version={report['contract_version']}, "
        f"files={checks['repository_files']}, "
        f"fixtures={checks['full_state_fixtures']}, "
        f"release={checks['release_mode']}"
    )
    return 0
