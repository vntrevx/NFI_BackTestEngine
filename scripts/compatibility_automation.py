#!/usr/bin/env python3
"""Write one deterministic compatibility automation decision."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.compatibility_automation import classify_compatibility_automation
from nfi_backtest_engine.compatibility_automation_validation import (
    parse_compatibility_identity,
)
from nfi_backtest_engine.compatibility_input_status import write_identity_failure_status
from nfi_backtest_engine.compatibility_proof import (
    ProofFailureReason,
    UnavailableCompatibilityProof,
    load_compatibility_proof,
    seal_compatibility_proof,
)
from nfi_backtest_engine.compatibility_status import (
    CompatibilityRunObservation,
    DiscoveryExecution,
    WorkflowExecution,
    classify_compatibility_status,
)
from nfi_backtest_engine.errors import InputBoundaryError, SpecValidationError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--strategy-diff", type=Path)
    parser.add_argument("--compatibility", type=Path)
    parser.add_argument("--targeted", type=Path)
    parser.add_argument("--discovery", type=Path)
    parser.add_argument("--decision-dir", type=Path)
    parser.add_argument("--proof-dir", type=Path)
    parser.add_argument("--source-run-id")
    parser.add_argument("--seal-proof-manifest", action="store_true")
    parser.add_argument("--current-engine-sha")
    parser.add_argument("--current-upstream-sha")
    parser.add_argument("--workflow-execution", choices=tuple(WorkflowExecution))
    parser.add_argument("--spot-discovery-execution", choices=tuple(DiscoveryExecution))
    parser.add_argument("--futures-discovery-execution", choices=tuple(DiscoveryExecution))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    malformed = False
    seal_failed = False
    if args.decision_dir is not None:
        required = (
            args.current_engine_sha,
            args.current_upstream_sha,
            args.workflow_execution,
            args.spot_discovery_execution,
            args.futures_discovery_execution,
        )
        if any(value is None for value in required):
            parser.error("status classification requires current refs and execution states")
        observation = CompatibilityRunObservation(
            current_engine_sha=args.current_engine_sha,
            current_upstream_sha=args.current_upstream_sha,
            workflow_execution=WorkflowExecution(args.workflow_execution),
            discovery_execution={
                "spot": DiscoveryExecution(args.spot_discovery_execution),
                "futures": DiscoveryExecution(args.futures_discovery_execution),
            },
        )
        if not args.identity.is_file():
            identity_failure = "missing_identity"
            identity_document = None
        else:
            try:
                identity_value = read_json(args.identity)
            except (json.JSONDecodeError, UnicodeDecodeError, InputBoundaryError):
                identity_failure = "malformed_identity"
                identity_document = None
            else:
                if not isinstance(identity_value, dict):
                    identity_failure = "malformed_identity"
                    identity_document = None
                else:
                    try:
                        parse_compatibility_identity(identity_value)
                    except SpecValidationError:
                        identity_failure = "malformed_identity"
                        identity_document = None
                    else:
                        identity_failure = None
                        identity_document = identity_value
        if identity_failure is not None:
            report = write_identity_failure_status(
                observation,
                identity_failure,
                args.output,
            )
            print(json.dumps(report, sort_keys=True))
            return 1
        if identity_document is None:
            report = write_identity_failure_status(
                observation,
                "malformed_identity",
                args.output,
            )
            print(json.dumps(report, sort_keys=True))
            return 1
        decisions: dict[str, Mapping[str, Any]] = {}
        for mode in ("spot", "futures"):
            path = args.decision_dir / f"automation-decision-{mode}.json"
            if path.is_file():
                try:
                    decision = read_json(path)
                except (json.JSONDecodeError, UnicodeDecodeError, InputBoundaryError):
                    malformed = True
                    continue
                if not isinstance(decision, dict):
                    malformed = True
                    continue
                decisions[mode] = decision
        proof_root = args.proof_dir or args.decision_dir
        if malformed:
            proof = UnavailableCompatibilityProof(ProofFailureReason.MALFORMED)
        elif args.source_run_id is None:
            proof = UnavailableCompatibilityProof(ProofFailureReason.MISSING)
        else:
            if args.seal_proof_manifest:
                try:
                    seal_compatibility_proof(proof_root, args.source_run_id)
                except InputBoundaryError:
                    seal_failed = True
            proof = (
                UnavailableCompatibilityProof(ProofFailureReason.MISSING)
                if seal_failed
                else load_compatibility_proof(
                    proof_root,
                    expected_source_run_id=args.source_run_id,
                )
            )
        report = classify_compatibility_status(
            identity_document,
            decisions,
            observation,
            authoritative_proof=proof,
            output_path=args.output,
        )
    else:
        if args.strategy_diff is None or args.compatibility is None or args.targeted is None:
            parser.error("route classification requires strategy diff, compatibility, and targeted")
        report = classify_compatibility_automation(
            args.identity,
            args.strategy_diff,
            args.compatibility,
            args.targeted,
            discovery=args.discovery,
            output_path=args.output,
        )
    print(json.dumps(report, sort_keys=True))
    return 1 if args.decision_dir is not None and (malformed or seal_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
