"""Full X7 candidate and sealed-input validation."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

from ..archive_security import read_zip_member, validate_zip_archive
from ..canonical import read_json
from ..config_loader import config_sha256, load_effective_config
from ..data_seal import validate_data_seal
from ..errors import BenchmarkError, SpecValidationError
from ..fixture import sha256_file
from ..market_snapshot import validate_release_market_snapshot
from ..product_contract import (
    FULL_X7_RELEASE_TIMEFRAMES,
    MIN_RELEASE_BACKTEST_DAYS,
    MIN_RELEASE_PAIR_COUNT,
)
from ..release_contract import (
    ReleaseModeContract,
    release_contract_for_config,
    release_contract_for_scope,
)
from ..release_inputs import (
    LEGACY_RELEASE_INPUT_LOCK_VERSION,
    RELEASE_INPUT_LOCK_VERSION,
    release_data_history_coverage_policy,
    release_history_coverage_policy,
    validate_listing_aware_market_snapshot,
    validate_release_data_roles,
    validate_release_input_lock,
)
from ..research_reference import (
    official_backtest_config,
    validate_reference_market_snapshot,
)
from ..timerange import parse_timerange_milliseconds


def verify_installed_wheel(
    wheel_path: str | Path,
    build: dict[str, Any],
    *,
    package_root: str | Path | None = None,
) -> dict[str, Any]:
    """Bind every installed package member to the exact candidate wheel bytes."""
    wheel = Path(wheel_path).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise BenchmarkError(f"release wheel does not exist: {wheel}")
    if build.get("kind") != "pyo3-extension":
        raise BenchmarkError("Full X7 certification must run an installed native wheel")
    suffixes = (".pyd", ".so", ".dylib")
    if wheel.is_symlink():
        raise BenchmarkError(f"release wheel must not be a symlink: {wheel}")
    with zipfile.ZipFile(wheel) as archive:
        archive_members = validate_zip_archive(archive)
        package_members = sorted(
            name
            for name in archive_members
            if name.startswith("nfi_backtest_engine/") and not name.endswith("/")
        )
        if not package_members:
            raise BenchmarkError("release wheel has no nfi_backtest_engine package files")
        candidates = sorted(
            name
            for name in package_members
            if name.startswith("nfi_backtest_engine/_rust") and name.endswith(suffixes)
        )
        if len(candidates) != 1:
            raise BenchmarkError(
                f"release wheel must contain exactly one native extension; found {len(candidates)}"
            )
        member_sha = hashlib.sha256(
            read_zip_member(archive, archive_members[candidates[0]])
        ).hexdigest()
        installed_root = (
            Path(package_root).resolve()
            if package_root is not None
            else Path(__file__).resolve().parents[1]
        )
        member_records: list[tuple[str, str]] = []
        portable_member_records: list[tuple[str, str]] = []
        for name in package_members:
            relative = Path(name).relative_to("nfi_backtest_engine")
            installed = installed_root / relative
            wheel_sha = hashlib.sha256(
                read_zip_member(archive, archive_members[name])
            ).hexdigest()
            if not installed.is_file() or sha256_file(installed) != wheel_sha:
                raise BenchmarkError(
                    f"installed package file does not match the candidate wheel: {relative}"
                )
            member_records.append((name, wheel_sha))
            if name != candidates[0]:
                portable_member_records.append((name, wheel_sha))
    installed_sha = build.get("binary_sha256")
    equal = member_sha == installed_sha
    if not equal:
        raise BenchmarkError("imported native extension does not match the candidate wheel")
    package_identity = hashlib.sha256(
        json.dumps(
            member_records,
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    portable_package_identity = hashlib.sha256(
        json.dumps(
            portable_member_records,
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "path": str(wheel),
        "sha256": sha256_file(wheel),
        "bytes": wheel.stat().st_size,
        "native_member": candidates[0],
        "native_member_sha256": member_sha,
        "installed_extension_sha256": installed_sha,
        "installed_extension_equal": equal,
        "installed_package_files": len(member_records),
        "installed_package_sha256": package_identity,
        "portable_package_files": len(portable_member_records),
        "portable_package_sha256": portable_package_identity,
    }


def validate_full_x7_inputs(
    *,
    release_lock_path: str | Path,
    strategy_path: str | Path,
    class_name: str,
    config_path: str | Path,
    data_directory: str | Path,
    engine_market_snapshot: str | Path,
    reference_market_snapshot: str | Path | None,
) -> dict[str, Any]:
    lock_path = Path(release_lock_path).resolve()
    lock = read_json(lock_path)
    validate_release_input_lock(lock, required_pair_count=MIN_RELEASE_PAIR_COUNT)
    contract = release_contract_for_scope(
        lock["scope"],
        legacy_spot=lock["schema_version"] == LEGACY_RELEASE_INPUT_LOCK_VERSION,
    )
    _validate_full_x7_timeframes(lock["scope"]["timeframes"])
    return _resolve_full_x7_inputs(
        lock_path=lock_path,
        lock=lock,
        contract=contract,
        strategy_path=strategy_path,
        class_name=class_name,
        config_path=config_path,
        data_directory=data_directory,
        engine_market_snapshot=engine_market_snapshot,
        reference_market_snapshot=reference_market_snapshot,
    )


def _validate_full_x7_timeframes(timeframes: Any) -> None:
    actual_timeframes = tuple(timeframes) if isinstance(timeframes, list) else ()
    if actual_timeframes != FULL_X7_RELEASE_TIMEFRAMES:
        raise SpecValidationError(
            "Full X7 release timeframes differ from the certified contract: "
            f"expected {list(FULL_X7_RELEASE_TIMEFRAMES)!r}, "
            f"got {list(actual_timeframes)!r}"
        )


def _resolve_full_x7_inputs(
    *,
    lock_path: Path,
    lock: dict[str, Any],
    contract: ReleaseModeContract,
    strategy_path: str | Path,
    class_name: str,
    config_path: str | Path,
    data_directory: str | Path,
    engine_market_snapshot: str | Path,
    reference_market_snapshot: str | Path | None,
) -> dict[str, Any]:
    source = Path(strategy_path).resolve()
    config = Path(config_path).resolve()
    data_root = Path(data_directory).resolve()
    engine_markets = Path(engine_market_snapshot).resolve()
    reference_markets = (
        Path(reference_market_snapshot).resolve() if reference_market_snapshot is not None else None
    )
    required_files = [
        (source, "strategy"),
        (config, "config"),
        (engine_markets, "engine market snapshot"),
    ]
    if reference_markets is not None:
        required_files.append((reference_markets, "reference market snapshot"))
    for path, label in required_files:
        if not path.is_file():
            raise BenchmarkError(f"Full X7 {label} does not exist: {path}")
    if not data_root.is_dir():
        raise BenchmarkError(f"Full X7 data directory does not exist: {data_root}")
    if class_name != lock["strategy"]["class_name"]:
        raise SpecValidationError("strategy class differs from the release input lock")
    if sha256_file(source) != lock["strategy"]["source_sha256"]:
        raise SpecValidationError("strategy source differs from the release input lock")
    loaded = load_effective_config(config)
    if config_sha256(loaded["config"]) != lock["config"]["selected_sha256"]:
        raise SpecValidationError("selected config differs from the release input lock")
    config_contract = release_contract_for_config(loaded["config"])
    if config_contract.contract_id != contract.contract_id:
        raise SpecValidationError("selected config mode differs from the release input lock")
    seal_path = lock_path.parent / "data-seal.json"
    seal = validate_data_seal(seal_path)
    _validate_release_data_seal(
        lock,
        seal,
        data_directory=data_root,
        contract=contract,
    )
    engine_market_document = read_json(engine_markets)
    validate_release_market_snapshot(
        engine_market_document,
        contract=contract,
        pairs=lock["pairlist"]["pairs"],
    )
    validate_listing_aware_market_snapshot(lock, engine_market_document)
    if reference_markets is not None:
        validate_reference_market_snapshot(
            read_json(reference_markets),
            expected_exchange=contract.exchange,
            expected_trading_mode=contract.trading_mode,
            required_pairs=lock["pairlist"]["pairs"],
        )
    start_ms, end_ms = parse_timerange_milliseconds(lock["scope"]["timerange"])
    actual_days = (end_ms - start_ms) // 86_400_000
    if actual_days < MIN_RELEASE_BACKTEST_DAYS:
        raise SpecValidationError(
            f"Full X7 timerange has {actual_days} days; {MIN_RELEASE_BACKTEST_DAYS} required"
        )
    return {
        "lock": lock,
        "contract": contract,
        "strategy_path": source,
        "config_path": config,
        "data_directory": data_root,
        "engine_market_snapshot": engine_markets,
        "reference_market_snapshot": reference_markets,
        "public": {
            "release_lock": {
                "sha256": sha256_file(lock_path),
                "identity_sha256": lock["identity_sha256"],
            },
            "mode_contract": contract.contract_id,
            "reference": lock["reference"],
            "strategy_sha256": sha256_file(source),
            "config_sha256": loaded["sha256"],
            "official_reference_config_sha256": config_sha256(
                official_backtest_config(loaded["config"])
            ),
            "data_aggregate_sha256": seal["aggregate_sha256"],
            "engine_market_snapshot_sha256": sha256_file(engine_markets),
            "reference_market_snapshot_sha256": (
                sha256_file(reference_markets) if reference_markets is not None else None
            ),
        },
    }


def _validate_release_data_seal(
    lock: dict[str, Any],
    seal: dict[str, Any],
    *,
    data_directory: Path,
    contract: ReleaseModeContract,
) -> None:
    """Bind the machine-local data seal to every portable lock invariant."""
    request = seal["request"]
    data = lock["data"]
    scope = lock["scope"]
    if Path(seal["data_root"]).resolve() != data_directory:
        raise SpecValidationError("selected data directory differs from the release data seal")
    if (
        seal["aggregate_sha256"] != data["aggregate_sha256"]
        or len(seal["files"]) != data["file_count"]
        or len(seal["coverage_shortfalls"]) != data["coverage_shortfall_count"]
        or len(seal["startup_shortfalls"]) != data["startup_shortfall_count"]
    ):
        raise SpecValidationError("data seal differs from the release input lock")
    if (
        lock.get("schema_version") == RELEASE_INPUT_LOCK_VERSION
        and seal["coverage_shortfalls"] != data["coverage_shortfalls"]
    ):
        raise SpecValidationError(
            "data seal coverage shortfalls differ from the release input lock"
        )
    if (
        request["pairs"] != lock["pairlist"]["pairs"]
        or request["timerange"] != scope["timerange"]
        or request["timeframes"] != scope["timeframes"]
        or request["history_coverage_policy"] != release_data_history_coverage_policy(lock)
        or request["startup_coverage_policy"] != data["startup_coverage_policy"]
    ):
        raise SpecValidationError("data seal request differs from the release input lock")
    role_counts = validate_release_data_roles(
        seal,
        contract=contract,
        history_coverage_policy=release_history_coverage_policy(lock),
        market_onboarding_ms=data.get("market_onboarding_ms"),
    )
    locked_role_counts = data.get("role_counts")
    if (
        lock.get("schema_version") != LEGACY_RELEASE_INPUT_LOCK_VERSION
        and role_counts != locked_role_counts
    ):
        raise SpecValidationError("data seal roles differ from the release input lock")
