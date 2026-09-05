"""Release-input, platform-evidence, and release-gate command orchestration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

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

    if args.platform_command == "prepare-attestation":
        from ..fixture import sha256_file
        from ..release_provenance import (
            create_platform_statement,
            prepare_statement_signing_bytes,
        )

        statement = create_platform_statement(
            args.report,
            repository=args.repository,
            repository_ref=args.repository_ref,
            workflow=args.workflow,
            workflow_ref=args.workflow_ref,
            job=args.job,
            commit=args.commit,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            candidate_id=args.candidate_id,
            bundle_id=args.bundle_id,
            challenge=args.challenge,
            nonce=args.nonce,
        )
        payload, pae = prepare_statement_signing_bytes(statement)
        args.payload_output.write_bytes(payload)
        args.pae_output.write_bytes(pae)
        args.checksum_output.write_text(
            f"{sha256_file(args.payload_output)}  {args.payload_output.name}\n"
            f"{sha256_file(args.pae_output)}  {args.pae_output.name}\n",
            encoding="utf-8",
        )
        print(f"platform provenance signing request: {args.pae_output}")
        return 0
    if args.platform_command == "assemble-attestation":
        from ..canonical import write_json
        from ..release_provenance import assemble_statement_envelope

        envelope = assemble_statement_envelope(
            args.payload.read_bytes(), args.signature.read_bytes()
        )
        write_json(args.output, envelope)
        print(f"platform provenance assembled: {args.output}")
        return 0
    if args.platform_command == "attest":
        from ..release_provenance import (
            PRODUCTION_KEY_ID,
            load_signing_key,
            write_signed_platform_provenance,
        )

        encoded_key = os.environ.get("NFI_RELEASE_PROVENANCE_PRIVATE_KEY")
        if not encoded_key:
            raise RuntimeError("NFI_RELEASE_PROVENANCE_PRIVATE_KEY is not configured")
        private_key = load_signing_key(encoded_key.encode())
        envelope = write_signed_platform_provenance(
            args.report,
            args.output,
            repository=args.repository,
            repository_ref=args.repository_ref,
            workflow=args.workflow,
            workflow_ref=args.workflow_ref,
            job=args.job,
            commit=args.commit,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            candidate_id=args.candidate_id,
            bundle_id=args.bundle_id,
            challenge=args.challenge,
            nonce=args.nonce,
            key_id=PRODUCTION_KEY_ID,
            private_key=private_key,
        )
        print(
            "platform provenance signed: "
            f"payload_type={envelope['payloadType']} -> {args.output}"
        )
        return 0
    if args.platform_command == "seal":
        from ..platform_benchmark import REQUIRED_PLATFORM_SLUGS

        evidence = seal_platform_evidence(
            args.report,
            args.output_dir,
            expected_commit=args.candidate_commit,
            expected_run_id=args.run_id,
            expected_run_attempt=args.run_attempt,
            expected_candidate_id=args.candidate_id,
            expected_bundle_id=args.bundle_id,
            expected_challenge=args.challenge,
            required_platform_slugs=(
                REQUIRED_PLATFORM_SLUGS
                if args.platform_contract == "v2-slugs"
                else None
            ),
        )
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
    if args.release_command == "cleanroom":
        from ..cleanroom_e2e import run_cleanroom_e2e

        report = run_cleanroom_e2e(
            args.fixture,
            args.output_dir,
            timeout_seconds=args.timeout,
        )
        print(
            "clean-room user journey: "
            f"complete={report['complete']}, commands={len(report['commands'])} -> "
            f"{args.output_dir / 'cleanroom-report.json'}"
        )
        return 0
    if args.release_command == "score":
        from ..native_scorecard import evaluate_native_scorecard

        report = evaluate_native_scorecard(
            args.evidence,
            expected_identity_path=args.identity,
            output_path=args.output,
            authorization_operation=args.operation,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["perfect_native"] else 1
    if args.release_command == "authorize-current":
        from ..native_scorecard import require_fresh_current_ref_for_authorization

        require_fresh_current_ref_for_authorization(
            args.evidence,
            args.identity,
            args.operation,
        )
        print("current-ref authorization valid")
        return 0
    if args.release_command == "combine":
        from ..combined_release import combine_full_x7_release

        report = combine_full_x7_release(
            spot_certificate_path=args.spot_certificate,
            futures_certificate_path=args.futures_certificate,
            platform_evidence_paths=args.platform_evidence,
            output_directory=args.output_dir,
            native_score_evidence_path=args.native_score_evidence,
            native_score_identity_path=args.native_score_identity,
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
            provenance_ledger_path=args.provenance_ledger,
            publication_attempt_id=args.publication_attempt,
            native_score_evidence_path=args.native_score_evidence,
            native_score_identity_path=args.native_score_identity,
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
            provenance_ledger_path=args.provenance_ledger,
            publication_attempt_id=args.publication_attempt,
            native_score_evidence_path=args.native_score_evidence,
            native_score_identity_path=args.native_score_identity,
        )
        print(
            f"combined release gate: status={report['status']}, "
            f"commit={report['candidate_commit']}, "
            f"distributions={len(report['distributions'])} -> "
            f"{args.output_dir / 'RELEASE-SHA256SUMS.txt'}"
        )
        return 0
    if args.release_command in {"finalize-combined", "abort-combined"}:
        from ..combined_release import (
            abort_combined_release_publication,
            finalize_combined_release_publication,
        )

        if args.release_command == "finalize-combined":
            finalize_combined_release_publication(
                args.release_dir,
                provenance_ledger_path=args.provenance_ledger,
                publication_attempt_id=args.publication_attempt,
                expected_commit=args.candidate_commit,
                native_score_evidence_path=args.native_score_evidence,
                native_score_identity_path=args.native_score_identity,
            )
            print("combined release durable publication finalized")
        else:
            abort_combined_release_publication(
                args.release_dir,
                provenance_ledger_path=args.provenance_ledger,
                publication_attempt_id=args.publication_attempt,
            )
            print("combined release durable publication aborted")
        return 0
    if args.release_command == "verify-combined":
        from ..combined_release import verify_combined_release_candidate

        report = verify_combined_release_candidate(
            args.release_dir,
            expected_commit=args.candidate_commit,
            provenance_ledger_path=args.provenance_ledger,
            native_score_evidence_path=args.native_score_evidence,
            native_score_identity_path=args.native_score_identity,
        )
        print(
            f"combined release valid: commit={report['candidate_commit']}, "
            f"distributions={len(report['distributions'])}, "
            f"version={report['package_version']}"
        )
        return 0
    if args.release_command == "verify-assets":
        from ..combined_release import verify_combined_release_assets

        report = verify_combined_release_assets(
            args.release_dir,
            expected_commit=args.candidate_commit,
        )
        print(
            f"combined public assets valid: commit={report['candidate_commit']}, "
            f"distributions={len(report['distributions'])}, "
            f"version={report['package_version']}"
        )
        return 0
    if args.release_command == "record-soak":
        from ..release_audit import record_operations_soak_cycle

        checks: dict[str, dict[str, object]] = {}
        for supplied in args.check:
            name, separator, raw_path = supplied.partition("=")
            if not separator or not name or name in checks:
                raise ValueError("--check must be a unique name=JSON-path value")
            document = read_json(Path(raw_path))
            if not isinstance(document, dict):
                raise ValueError(f"soak check must be a JSON object: {raw_path}")
            checks[name] = document
        report = record_operations_soak_cycle(
            candidate_commit=args.candidate_commit,
            release_tag=args.release_tag,
            cycle=args.cycle,
            checked_at=args.checked_at,
            public_manifest_sha256=args.public_manifest_sha256,
            checks=checks,
            output_path=args.output,
        )
        print(
            f"operations soak cycle sealed: cycle={report['cycle']}/7, "
            f"commit={report['candidate_commit']} -> {args.output}"
        )
        return 0
    if args.release_command == "audit":
        from ..release_audit import seal_ten_of_ten_release_audit

        report = seal_ten_of_ten_release_audit(
            release_directory=args.release_dir,
            candidate_commit=args.candidate_commit,
            release_tag=args.release_tag,
            soak_receipt_paths=args.soak_receipt,
            output_path=args.output,
            product_contract_path=args.product_contract,
        )
        print(
            f"10/10 public release audit sealed: cycles={report['operations']['cycles']}, "
            f"identity={report['identity_sha256']} -> {args.output}"
        )
        return 0
    raise AssertionError(f"unhandled release command: {args.release_command}")


def execute_regression_contract(args: argparse.Namespace) -> int:
    if args.contract_command == "support":
        from ..product_support_contract import load_product_support_contract

        contract = load_product_support_contract(args.contract)
        if args.json:
            print(json.dumps(contract, indent=2, sort_keys=True))
        else:
            native = ", ".join(
                item["family"] for item in contract["strategies"]["native_supported"]
            )
            platforms = ", ".join(contract["platforms"]["supported"])
            certification = contract["certification"]
            print(
                "product support contract: "
                f"native={native}; platforms={platforms}; "
                f"combined={certification['combined_status']}; "
                f"target={certification['target_release']}"
            )
        return 0

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
