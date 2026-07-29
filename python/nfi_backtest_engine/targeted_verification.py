"""Deterministic changed-path verification planning for future NFI revisions."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError, BranchCoverageError, SpecValidationError
from .fixture import sha256_file

TARGETED_VERIFICATION_PLAN_VERSION = "1.0.0"
_GRIND_LEVEL = re.compile(
    r"(?i)(?:grind|derisk|buyback|rebuy|(?:sg|gd|gm|gmd|dd|ddl|g|d))"
    r"(?:[_ -]*(?:level)?[_ -]*)?(\d+)"
)
Service = Callable[..., dict[str, Any]]


def verify_targeted_strategy(
    source: str | Path,
    strategy_diff: Mapping[str, Any] | str | Path,
    compatibility_report: Mapping[str, Any] | str | Path,
    fixtures_root: str | Path,
    output_directory: str | Path,
    *,
    class_name: str,
    trading_mode: str,
    upstream_repository: str,
    upstream_commit: str,
    timeout_seconds: int,
    workers: int | None = None,
    capture_service: Service | None = None,
    fixture_service: Service | None = None,
    profile_service: Service | None = None,
) -> dict[str, Any]:
    """Run only branch fixtures relevant to one static-compatible revision."""
    from .compatibility_qualification import qualify_compatibility
    from .fixture_engine import run_fixture_engine
    from .hardware import create_execution_profile
    from .probe_capture import capture_x7_probe

    source_path = Path(source).resolve()
    output = Path(output_directory).resolve()
    if not source_path.is_file():
        raise SpecValidationError(f"strategy source does not exist: {source_path}")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise BenchmarkError(f"targeted verification output must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    difference = _document(strategy_diff, "strategy diff")
    compatibility = _document(compatibility_report, "compatibility report")
    plan = plan_targeted_verification(
        difference,
        fixtures_root,
        trading_mode=trading_mode,
        output_path=output / "verification-plan.json",
    )
    capture = capture_service or capture_x7_probe
    run_fixture = fixture_service or run_fixture_engine
    create_profile = profile_service or create_execution_profile
    runs: list[dict[str, Any]] = []
    execution_blockers: list[dict[str, str]] = []
    proof: dict[str, Any] | None = None
    if compatibility.get("native_compatible") is True and plan["executable"]:
        profile_path = output / "execution-profile.json"
        create_profile(profile_path)
        target_by_id = {str(target["id"]): target for target in plan["targets"]}
        for index, selected in enumerate(plan["selected_fixtures"], start=1):
            template_manifest = Path(selected["manifest_path"]).resolve()
            root = output / "runs" / f"{index:02d}-{selected['fixture_id']}"
            root.mkdir(parents=True)
            spec_path = root / "probe-spec.json"
            selected_targets = [target_by_id[target_id] for target_id in selected["target_ids"]]
            _write_probe_spec(
                source_path,
                template_manifest,
                profile_path,
                spec_path,
                selected_targets=selected_targets,
                class_name=class_name,
                upstream_repository=upstream_repository,
                upstream_commit=upstream_commit,
            )
            fixture_output = root / "official-fixture"
            try:
                capture_report = capture(
                    spec_path,
                    fixture_output,
                    root / "capture-work",
                    timeout_seconds=timeout_seconds,
                    workers=workers,
                )
            except BranchCoverageError as exc:
                execution_blockers.append(
                    {
                        "code": "TARGETED_BRANCH_NOT_REACHED",
                        "message": str(exc),
                    }
                )
                runs.append(
                    {
                        "fixture_id": selected["fixture_id"],
                        "target_ids": selected["target_ids"],
                        "capture": None,
                        "native_report": str(root / "capture-work" / "engine" / "run.json"),
                        "coverage": {
                            "complete": False,
                            "changed_branch_reached": False,
                            "reached_target_ids": [],
                            "missing_target_ids": selected["target_ids"],
                        },
                        "trade_surface_exact": False,
                        "full_state_exact": False,
                    }
                )
                break
            except BenchmarkError:
                blocker = _completed_semantic_failure(root / "capture-work")
                if blocker is None:
                    raise
                execution_blockers.append(blocker)
                runs.append(
                    {
                        "fixture_id": selected["fixture_id"],
                        "target_ids": selected["target_ids"],
                        "capture": None,
                        "native_report": str(root / "capture-work" / "engine" / "run.json"),
                        "coverage": {
                            "complete": False,
                            "changed_branch_reached": False,
                            "reached_target_ids": [],
                            "missing_target_ids": selected["target_ids"],
                        },
                        "trade_surface_exact": False,
                        "full_state_exact": False,
                    }
                )
                break
            native_output = root / "native-exact"
            native = run_fixture(
                fixture_output / "manifest.json",
                native_output,
                timeout_seconds=timeout_seconds,
                verification_level="full",
            )
            coverage = assess_targeted_coverage(
                selected_targets,
                baseline_manifest=template_manifest,
                candidate_manifest=fixture_output / "manifest.json",
            )
            runs.append(
                {
                    "fixture_id": selected["fixture_id"],
                    "target_ids": selected["target_ids"],
                    "capture": capture_report,
                    "native_report": str(native_output / "run.json"),
                    "coverage": coverage,
                    "trade_surface_exact": (
                        native.get("parity", {}).get("trade_surface", {}).get("equal") is True
                    ),
                    "full_state_exact": (
                        native.get("parity", {}).get("state_trace", {}).get("equal") is True
                    ),
                }
            )
        proof = {
            "complete": bool(runs) and all(run["coverage"]["complete"] for run in runs),
            "changed_branch_reached": bool(runs)
            and all(run["coverage"]["changed_branch_reached"] for run in runs),
            "trade_surface_exact": bool(runs) and all(run["trade_surface_exact"] for run in runs),
            "full_state_exact": bool(runs) and all(run["full_state_exact"] for run in runs),
        }
    qualification = qualify_compatibility(
        compatibility,
        difference,
        branch_proof=proof,
        output_path=output / "qualification.json",
    )
    operational_blockers = _operational_blockers(
        compatibility,
        plan,
        qualification,
    )
    operational_blockers.extend(execution_blockers)
    report = {
        "schema_version": "1.0.0",
        "trading_mode": trading_mode,
        "source_sha256": compatibility.get("source", {}).get("sha256"),
        "plan": {
            "status": plan["status"],
            "selected_fixture_count": len(plan["selected_fixtures"]),
            "missing_target_count": len(plan["missing_targets"]),
        },
        "runs": runs,
        "proof": proof,
        "qualification": qualification,
        "blockers": operational_blockers,
        "verification_state": qualification["verification_state"],
        "complete": qualification["verification_state"] == "quick_verified",
    }
    write_json(output / "run.json", report)
    return report


def plan_targeted_verification(
    strategy_diff: Mapping[str, Any] | str | Path,
    fixtures_root: str | Path,
    *,
    trading_mode: str,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Select the smallest deterministic fixture set that reaches every target."""
    if trading_mode not in {"spot", "futures"}:
        raise SpecValidationError("targeted verification mode must be spot or futures")
    difference = _document(strategy_diff, "strategy diff")
    targets = behavior_targets(difference)
    fixtures = _fixture_inventory(Path(fixtures_root).resolve(), trading_mode)
    remaining = {str(target["id"]) for target in targets}
    candidates = [
        {
            **fixture,
            "target_ids": sorted(
                target["id"] for target in targets if target_observed(target, fixture["features"])
            ),
        }
        for fixture in fixtures
    ]
    selected: list[dict[str, Any]] = []
    while remaining:
        choices = [
            (len(remaining & set(candidate["target_ids"])), candidate)
            for candidate in candidates
            if remaining & set(candidate["target_ids"])
        ]
        if not choices:
            break
        _, chosen = min(
            choices,
            key=lambda item: (
                -item[0],
                item[1]["fixture_id"],
                item[1]["manifest_path"],
            ),
        )
        covered = sorted(remaining & set(chosen["target_ids"]))
        selected.append(
            {
                "fixture_id": chosen["fixture_id"],
                "manifest_path": chosen["manifest_path"],
                "target_ids": covered,
            }
        )
        remaining.difference_update(covered)
        candidates.remove(chosen)

    missing = [target for target in targets if str(target["id"]) in remaining]
    status = "no-changes" if not targets else "coverage-gap" if missing else "ready"
    report = {
        "schema_version": TARGETED_VERIFICATION_PLAN_VERSION,
        "trading_mode": trading_mode,
        "classification": difference.get("classification"),
        "status": status,
        "targets": targets,
        "selected_fixtures": selected,
        "missing_targets": missing,
        "executable": status == "ready",
    }
    if output_path is not None:
        write_json(output_path, report)
    return report


def assess_targeted_coverage(
    targets: Sequence[Mapping[str, Any]],
    *,
    baseline_manifest: str | Path,
    candidate_manifest: str | Path,
) -> dict[str, Any]:
    """Prove added/changed behavior in the candidate and removals against baseline."""
    baseline = _fixture_features(Path(baseline_manifest).resolve())
    candidate = _fixture_features(Path(candidate_manifest).resolve())
    reached: list[str] = []
    missing: list[str] = []
    target_proofs: list[dict[str, Any]] = []
    for target in targets:
        target_id = str(target.get("id", ""))
        baseline_observed = target_observed(target, baseline)
        candidate_observed = target_observed(target, candidate)
        proof_mode = _target_proof_mode(target)
        if proof_mode == "absence":
            covered = baseline_observed and not candidate_observed
        elif proof_mode == "transition":
            covered = baseline_observed and candidate_observed
        else:
            covered = candidate_observed
        (reached if covered else missing).append(target_id)
        target_proofs.append(
            {
                "target_id": target_id,
                "proof_mode": proof_mode,
                "baseline_observed": baseline_observed,
                "candidate_observed": candidate_observed,
                "complete": covered,
            }
        )
    return {
        "complete": not missing,
        "changed_branch_reached": not missing and bool(targets),
        "reached_target_ids": sorted(reached),
        "missing_target_ids": sorted(missing),
        "target_proofs": sorted(
            target_proofs,
            key=lambda item: item["target_id"],
        ),
    }


def _write_probe_spec(
    source: Path,
    template_manifest: Path,
    profile_path: Path,
    destination: Path,
    *,
    selected_targets: list[Mapping[str, Any]],
    class_name: str,
    upstream_repository: str,
    upstream_commit: str,
) -> None:
    from .data_seal import candle_files_for
    from .strategy_ir import analyze_strategy

    manifest = _document(template_manifest, "fixture manifest")
    root = template_manifest.parent
    config_path = root / _one_input(manifest, "config")["path"]
    engine_markets = root / _one_input(manifest, "market_metadata")["path"]
    reference_markets = root / _one_input(manifest, "reference_market_metadata")["path"]
    config = _document(config_path, "fixture config")
    exchange = config.get("exchange")
    pairs = exchange.get("pair_whitelist") if isinstance(exchange, Mapping) else None
    if not isinstance(pairs, list) or not all(isinstance(pair, str) and pair for pair in pairs):
        raise SpecValidationError("fixture config pair whitelist is invalid")
    analysis = analyze_strategy(source, class_name=class_name)
    strategies = analysis.get("strategies")
    if not isinstance(strategies, list) or len(strategies) != 1:
        raise SpecValidationError("targeted strategy analysis did not select one class")
    strategy = strategies[0]
    timeframes = strategy.get("required_timeframes") if isinstance(strategy, Mapping) else None
    if not isinstance(timeframes, list) or not all(
        isinstance(timeframe, str) and timeframe for timeframe in timeframes
    ):
        raise SpecValidationError("targeted strategy timeframes are invalid")
    data_directory = _fixture_data_directory(root, manifest)
    trading_mode = manifest.get("freqtrade", {}).get("trading_mode")
    if trading_mode not in {"spot", "futures"}:
        raise SpecValidationError("fixture trading mode is invalid")
    sealed_pairs = _fixture_candle_pairs(
        manifest,
        timeframes=timeframes,
        trading_mode=trading_mode,
    )
    informative_pairs = sorted(set(sealed_pairs) - set(pairs))
    if not all(
        all(
            len(
                candle_files_for(
                    data_directory,
                    pair=pair,
                    timeframe=timeframe,
                    trading_mode=trading_mode,
                )
            )
            == 1
            for timeframe in timeframes
        )
        for pair in sealed_pairs
    ):
        raise SpecValidationError(
            "targeted fixture candle inputs do not cover every required timeframe"
        )
    freqtrade = manifest.get("freqtrade")
    if not isinstance(freqtrade, Mapping):
        raise SpecValidationError("fixture Freqtrade contract is invalid")
    fixture_id = str(manifest.get("fixture_id", "targeted"))
    required_coverage = _targeted_required_coverage(
        root,
        manifest,
        selected_targets,
    )
    spec = {
        "schema_version": "1.0.0",
        "fixture": {
            "id": f"{fixture_id}-targeted-{sha256_file(source)[:12]}",
            "description": (
                "Ephemeral changed-path verification generated from immutable "
                f"fixture {fixture_id}."
            ),
            "probe_kind": str(manifest.get("probe_kind", "targeted")),
            "required_coverage": required_coverage,
        },
        "upstream": {
            "repository": upstream_repository,
            "commit": upstream_commit,
        },
        "strategy": {
            "source": str(source),
            "class_name": class_name,
        },
        "config": {
            "source": str(config_path),
            "overrides": {},
            "remove_paths": [],
        },
        "data": {
            "directory": str(data_directory),
            "timerange": str(freqtrade.get("timerange", "")),
            "pairs": pairs,
            "informative_pairs": informative_pairs,
        },
        "markets": {
            "engine": str(engine_markets),
            "reference": str(reference_markets),
        },
        "execution": {
            "profile": str(profile_path),
            "audit_timestamps_ms": [],
        },
    }
    write_json(destination, spec)


def _fixture_candle_pairs(
    manifest: Mapping[str, Any],
    *,
    timeframes: list[str],
    trading_mode: str,
) -> list[str]:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list):
        raise SpecValidationError("fixture inputs are invalid")
    pairs: set[str] = set()
    for item in inputs:
        if (
            not isinstance(item, Mapping)
            or item.get("role") != "candles"
            or not isinstance(item.get("path"), str)
        ):
            continue
        stem = Path(str(item["path"])).stem
        normalized = next(
            (
                stem[: -len(suffix)]
                for timeframe in sorted(timeframes, key=len, reverse=True)
                if stem.endswith(
                    suffix := (
                        f"-{timeframe}-futures" if trading_mode == "futures" else f"-{timeframe}"
                    )
                )
            ),
            None,
        )
        if normalized is None:
            raise SpecValidationError(f"fixture candle filename has no required timeframe: {stem}")
        pieces = normalized.rsplit("_", 2 if trading_mode == "futures" else 1)
        expected_pieces = 3 if trading_mode == "futures" else 2
        if len(pieces) != expected_pieces or not all(pieces):
            raise SpecValidationError(f"fixture candle filename has no canonical pair: {stem}")
        if trading_mode == "futures":
            base, quote, settlement = pieces
            pairs.add(f"{base}/{quote}:{settlement}")
        else:
            base, quote = pieces
            pairs.add(f"{base}/{quote}")
    if not pairs:
        raise SpecValidationError("targeted fixture contains no candle pairs")
    return sorted(pairs)


def _targeted_required_coverage(
    root: Path,
    manifest: Mapping[str, Any],
    targets: list[Mapping[str, Any]],
) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise SpecValidationError("targeted template artifacts are invalid")
    coverage_record = artifacts.get("coverage_report")
    surface_record = artifacts.get("trade_surface")
    if not isinstance(coverage_record, Mapping) or not isinstance(
        surface_record,
        Mapping,
    ):
        raise SpecValidationError("targeted template lacks coverage or trade surface")
    coverage = _document(
        _bound_artifact(root, coverage_record, "coverage"),
        "coverage report",
    )
    surface = _document(
        _bound_artifact(root, surface_record, "trade surface"),
        "trade surface",
    )
    observed = coverage.get("observed")
    trades = surface.get("trades")
    if not isinstance(observed, Mapping) or not isinstance(trades, list):
        raise SpecValidationError("targeted template observations are invalid")
    callbacks = _string_set(observed.get("callbacks"))
    entry_tags = _string_set(observed.get("entry_tags"))
    compound_tags = _string_set(observed.get("compound_tags"))
    exit_reasons = _string_set(observed.get("exit_reasons"))
    target_methods = {
        str(method)
        for target in targets
        for method in target.get("methods", [])
        if isinstance(method, str)
    }
    target_callbacks = {
        str(target.get("value")) for target in targets if target.get("kind") == "callback"
    }
    target_values = {value for target in targets for value in _target_observation_values(target)}
    required_callbacks = sorted(callbacks & (target_methods | target_callbacks))
    required_entry_tags = sorted(entry_tags & target_values)
    required_compound_tags = sorted(compound_tags & target_values)
    required_exit_reasons = sorted(exit_reasons & target_values)
    required_sides = sorted(
        {
            str(direction)
            for trade in trades
            if isinstance(trade, Mapping)
            and isinstance((direction := trade.get("direction")), str)
            and direction in {"long", "short"}
            and _trade_matches_targets(trade, target_values)
        }
    )
    require_distinct_leverages = any(
        target.get("kind") == "callback" and target.get("value") == "leverage" for target in targets
    )
    template_required = manifest.get("required_coverage")
    template_minimum_leverages = (
        template_required.get("minimum_distinct_leverages", 0)
        if isinstance(template_required, Mapping)
        else 0
    )
    minimum_leverages = (
        template_minimum_leverages
        if require_distinct_leverages
        and isinstance(template_minimum_leverages, int)
        and not isinstance(template_minimum_leverages, bool)
        else 0
    )
    required = {
        "callbacks": required_callbacks,
        "entry_tags": required_entry_tags,
        "compound_tags": required_compound_tags,
        "protection_methods": [],
        "exit_reasons": required_exit_reasons,
        "sides": required_sides,
        "minimum_lock_count": 0,
        "minimum_distinct_leverages": minimum_leverages,
        "minimum_funded_trades": 0,
        "require_rejected_locked_entry": False,
    }
    if not any(
        (
            required_callbacks,
            required_entry_tags,
            required_compound_tags,
            required_exit_reasons,
            required_sides,
            minimum_leverages,
        )
    ):
        raise SpecValidationError(
            "selected fixture cannot express required coverage for its targets"
        )
    return required


def _target_observation_values(target: Mapping[str, Any]) -> set[str]:
    values = {
        str(target.get("value", "")).strip(),
        *(str(tag).strip() for tag in target.get("tags", []) if isinstance(tag, str)),
    }
    return {value for item in values for value in (item, *item.split()) if value}


def _trade_matches_targets(
    trade: Mapping[str, Any],
    target_values: set[str],
) -> bool:
    tags = {
        tag.strip()
        for tag in (
            trade.get("entry_tag"),
            trade.get("exit_reason"),
        )
        if isinstance(tag, str) and tag.strip()
    }
    tags.update(
        tag.strip()
        for order in _orders(trade)
        if isinstance(order, Mapping) and isinstance((tag := order.get("tag")), str) and tag.strip()
    )
    observed_values = {value for tag in tags for value in (tag, *tag.split())}
    return bool(target_values & observed_values)


def _operational_blockers(
    compatibility: Mapping[str, Any],
    plan: Mapping[str, Any],
    qualification: Mapping[str, Any],
) -> list[dict[str, str]]:
    blockers = [
        {
            "code": str(item.get("code", "NATIVE_COMPATIBILITY_BLOCKED")),
            "message": str(item.get("message", "")),
        }
        for item in compatibility.get("blockers", [])
        if isinstance(item, Mapping)
    ]
    missing = plan.get("missing_targets")
    if isinstance(missing, list) and missing:
        kinds = sorted(
            {
                str(target.get("kind", "unknown"))
                for target in missing
                if isinstance(target, Mapping)
            }
        )
        blockers.append(
            {
                "code": "TARGETED_COVERAGE_GAP",
                "message": (
                    f"{len(missing)} changed behavior target(s) have no "
                    f"branch-reaching fixture: {', '.join(kinds)}"
                ),
            }
        )
    if plan.get("status") == "no-changes":
        blockers.append(
            {
                "code": "BASELINE_ONLY",
                "message": "no upstream behavior difference was available to qualify",
            }
        )
    for item in qualification.get("blockers", []):
        if isinstance(item, Mapping):
            blockers.append(
                {
                    "code": str(item.get("code", "QUALIFICATION_BLOCKED")),
                    "message": str(item.get("message", "")),
                }
            )
    unique = {(item["code"], item["message"]): item for item in blockers}
    return [unique[key] for key in sorted(unique)]


def _completed_semantic_failure(work: Path) -> dict[str, str] | None:
    engine_path = work / "engine" / "run.json"
    if engine_path.is_file():
        engine = _document(engine_path, "targeted Native run")
        capability = engine.get("capability")
        blockers = capability.get("blockers") if isinstance(capability, Mapping) else None
        if isinstance(blockers, list) and blockers:
            codes = sorted(
                {str(item.get("code", "UNKNOWN")) for item in blockers if isinstance(item, Mapping)}
            )
            return {
                "code": "TARGETED_NATIVE_EXECUTION_BLOCKED",
                "message": ("targeted Native execution remained unsupported: " + ", ".join(codes)),
            }
    reference_path = work / "reference" / "run.json"
    if reference_path.is_file():
        reference = _document(reference_path, "targeted official run")
        if reference.get("exit_code") == 0 and reference.get("exact_parity") is False:
            difference = reference.get("difference")
            path = difference.get("path") if isinstance(difference, Mapping) else None
            return {
                "code": "OFFICIAL_TRADE_SURFACE_MISMATCH",
                "message": (
                    "targeted official and Native trade surfaces differ"
                    + (f" at {path}" if isinstance(path, str) else "")
                ),
            }
    return None


def _fixture_data_directory(
    root: Path,
    manifest: Mapping[str, Any],
) -> Path:
    data_roots: set[Path] = set()
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list):
        raise SpecValidationError("fixture inputs are invalid")
    for item in inputs:
        if (
            not isinstance(item, Mapping)
            or item.get("role") not in {"candles", "funding_candles", "mark_candles"}
            or not isinstance(item.get("path"), str)
        ):
            continue
        parent = Path(str(item["path"])).parent
        data_roots.add(parent.parent if parent.name == "futures" else parent)
    if len(data_roots) != 1:
        raise SpecValidationError("fixture candle inputs do not share one data directory")
    data_directory = (root / data_roots.pop()).resolve()
    if not data_directory.is_relative_to(root) or not data_directory.is_dir():
        raise SpecValidationError("fixture candle data directory is invalid")
    return data_directory


def _one_input(
    manifest: Mapping[str, Any],
    role: str,
) -> dict[str, Any]:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list):
        raise SpecValidationError("fixture inputs are invalid")
    candidates = [
        dict(item) for item in inputs if isinstance(item, Mapping) and item.get("role") == role
    ]
    if len(candidates) != 1 or not isinstance(candidates[0].get("path"), str):
        raise SpecValidationError(f"fixture requires exactly one {role!r} input")
    return candidates[0]


def _fixture_inventory(root: Path, trading_mode: str) -> list[dict[str, Any]]:
    if not root.is_dir():
        raise SpecValidationError(f"fixture root does not exist: {root}")
    records = []
    for manifest_path in sorted(root.rglob("manifest.json")):
        manifest = read_json(manifest_path)
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != "3.0.0"
            or not isinstance(manifest.get("freqtrade"), dict)
            or manifest["freqtrade"].get("trading_mode") != trading_mode
            or not isinstance(manifest.get("fixture_id"), str)
        ):
            continue
        records.append(
            {
                "fixture_id": manifest["fixture_id"],
                "manifest_path": str(manifest_path),
                "features": _fixture_features(
                    manifest_path,
                    manifest=manifest,
                ),
            }
        )
    return records


def _fixture_features(
    manifest_path: Path,
    *,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, set[str] | set[int]]:
    document = (
        dict(manifest)
        if manifest is not None
        else _document(
            manifest_path,
            "fixture manifest",
        )
    )
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise SpecValidationError(f"fixture artifacts are invalid: {manifest_path}")
    coverage_record = artifacts.get("coverage_report")
    surface_record = artifacts.get("trade_surface")
    if not isinstance(coverage_record, Mapping) or not isinstance(
        surface_record,
        Mapping,
    ):
        raise SpecValidationError(
            f"targeted fixture lacks coverage or trade surface: {manifest_path}"
        )
    root = manifest_path.parent
    coverage_path = _bound_artifact(root, coverage_record, "coverage")
    surface_path = _bound_artifact(root, surface_record, "trade surface")
    coverage = _document(coverage_path, "coverage report")
    observed = coverage.get("observed")
    surface = _document(surface_path, "trade surface")
    if not isinstance(observed, Mapping) or not isinstance(
        surface.get("trades"),
        list,
    ):
        raise SpecValidationError(f"targeted fixture observations are invalid: {manifest_path}")
    entry_tags = _string_set(observed.get("entry_tags"))
    compound_tags = _string_set(observed.get("compound_tags"))
    exit_reasons = _string_set(observed.get("exit_reasons"))
    callbacks = _string_set(observed.get("callbacks"))
    order_tags = {
        tag.strip()
        for trade in surface["trades"]
        if isinstance(trade, Mapping)
        for order in _orders(trade)
        if isinstance(order, Mapping) and isinstance((tag := order.get("tag")), str) and tag.strip()
    }
    tags = entry_tags | compound_tags | exit_reasons | order_tags
    tokens = {token for tag in tags for token in tag.split() if token}
    grind_levels = {int(match.group(1)) for tag in tags for match in _GRIND_LEVEL.finditer(tag)}
    return {
        "callbacks": callbacks,
        "tags": tags,
        "tokens": tokens,
        "grind_levels": grind_levels,
    }


def target_observed(
    target: Mapping[str, Any],
    features: Mapping[str, set[str] | set[int]],
) -> bool:
    """Return whether independently derived runtime features reach one diff target."""
    if target.get("runtime_observable") is not True:
        return False
    kind = target.get("kind")
    value = target.get("value")
    callbacks = features["callbacks"]
    tags = features["tags"]
    tokens = features["tokens"]
    grind_levels = features["grind_levels"]
    if kind == "signal":
        return str(value) in tokens
    if kind == "tag":
        return str(value).strip() in tags
    if kind == "grind_level":
        return isinstance(value, int) and value in grind_levels
    target_tags = target.get("tags")
    tag_observed = isinstance(target_tags, list) and any(
        isinstance(tag, str) and tag.strip() in tags for tag in target_tags
    )
    if kind != "callback":
        return tag_observed
    # A changed callback invocation alone does not prove that its changed
    # source branch ran. Require one independently visible route/tag when the
    # diff supplied such selectors. Added callbacks without a selector retain
    # the callback-level proof used by existing fixtures.
    if _target_proof_mode(target) == "transition" and target_tags:
        return tag_observed
    return str(value) in callbacks or tag_observed


def behavior_targets(difference: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate and return the generic runtime targets emitted by strategy diff."""
    raw = difference.get("behavior_targets")
    if not isinstance(raw, list):
        raise SpecValidationError("strategy diff has no behavior_targets array")
    targets = []
    for target in raw:
        if (
            not isinstance(target, Mapping)
            or not isinstance(target.get("id"), str)
            or not isinstance(target.get("kind"), str)
            or target.get("change") not in {"added", "removed", "changed"}
            or not isinstance(target.get("runtime_observable"), bool)
        ):
            raise SpecValidationError("strategy diff behavior target is invalid")
        targets.append(dict(target))
    return targets


def _target_proof_mode(target: Mapping[str, Any]) -> str:
    proof = target.get("proof")
    explicit = proof.get("mode") if isinstance(proof, Mapping) else None
    if explicit in {"presence", "absence", "transition"}:
        return str(explicit)
    return {
        "added": "presence",
        "removed": "absence",
        "changed": "transition",
    }.get(str(target.get("change")), "presence")


# Private aliases preserve compatibility for tests and internal callers that predate
# the discovery API.  New code should use the public names above.
_target_observed = target_observed
_behavior_targets = behavior_targets


def _bound_artifact(
    root: Path,
    record: Mapping[str, Any],
    label: str,
) -> Path:
    value = record.get("path")
    if not isinstance(value, str):
        raise SpecValidationError(f"fixture {label} path is invalid")
    path = (root / value).resolve()
    if (
        not path.is_relative_to(root)
        or not path.is_file()
        or path.stat().st_size != record.get("bytes")
        or sha256_file(path) != record.get("sha256")
    ):
        raise SpecValidationError(f"fixture {label} failed its hash binding")
    return path


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SpecValidationError("fixture coverage string array is invalid")
    return {item.strip() for item in value if item.strip()}


def _orders(trade: Mapping[str, Any]) -> list[Any]:
    value = trade.get("orders")
    return value if isinstance(value, list) else []


def _document(
    value: Mapping[str, Any] | str | Path,
    label: str,
) -> dict[str, Any]:
    document = read_json(value) if isinstance(value, str | Path) else dict(value)
    if not isinstance(document, dict):
        raise SpecValidationError(f"{label} must be an object")
    return document
