#!/usr/bin/env python3
"""Fail-closed planning for protected Full X7 certification workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def read_object(path: str | Path, *, label: str) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is not valid JSON: {source}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {source}")
    return value


def load_contract(path: str | Path) -> dict[str, Any]:
    value = read_object(path, label="long-certification contract")
    concurrency = value.get("concurrency")
    oracle = value.get("oracle")
    storage = value.get("storage")
    if (
        value.get("schema_version") != "2.0.0"
        or value.get("events") != ["workflow_dispatch"]
        or value.get("permissions")
        != {"actions": "read", "contents": "read", "id-token": "write"}
        or not _nonempty_string(value.get("environment"))
        or not _unique_strings(value.get("runner_labels"))
        or not _unique_strings(value.get("modes"))
        or not isinstance(concurrency, dict)
        or not _nonempty_string(concurrency.get("group"))
        or concurrency.get("cancel_in_progress") is not False
        or not isinstance(oracle, dict)
        or oracle.get("allows_new_run") is not False
        or oracle.get("index_schema_versions") != ["1.0.0", "2.0.0"]
        or oracle.get("preferred_index_schema_version") != "2.0.0"
        or oracle.get("input_identity_schema_version") != "oracle-input-identity-v2"
        or oracle.get("required_status") != "exact_parity"
        or oracle.get("requires_immutable_record") is not True
        or value.get("candidate")
        != {"identity_schema_version": "candidate-certification-identity-v2"}
        or not isinstance(storage, dict)
        or storage.get("provider") != "aws-s3"
        or not all(
            _nonempty_string(storage.get(name))
            for name in (
                "environment_role_variable",
                "environment_region_variable",
                "environment_bucket_variable",
                "object_prefix",
            )
        )
        or storage.get("content_addressed_key") is not True
        or storage.get("conditional_create") is not True
        or storage.get("server_side_encryption") != "AES256"
    ):
        raise ValueError("long-certification contract is incomplete or unsafe")
    return value


def load_oracle_capture_contract(path: str | Path) -> dict[str, Any]:
    """Validate the narrower protected workflow allowed to create one Oracle."""
    value = read_object(path, label="Oracle capture contract")
    concurrency = value.get("concurrency")
    oracle = value.get("oracle")
    storage = value.get("storage")
    if (
        value.get("schema_version") != "1.0.0"
        or value.get("workflow") != ".github/workflows/capture-full-x7-oracle.yml"
        or value.get("events") != ["workflow_dispatch"]
        or value.get("permissions")
        != {"actions": "read", "contents": "read", "id-token": "write"}
        or not _nonempty_string(value.get("environment"))
        or not _unique_strings(value.get("runner_labels"))
        or value.get("modes") != ["spot", "futures"]
        or not isinstance(concurrency, dict)
        or concurrency.get("group") != "full-x7-oracle-${{ inputs.mode }}"
        or concurrency.get("cancel_in_progress") is not False
        or not isinstance(oracle, dict)
        or oracle.get("allows_new_run") is not True
        or oracle.get("maximum_runs_per_input") != 1
        or oracle.get("index_schema_version") != "2.0.0"
        or oracle.get("input_identity_schema_version") != "oracle-input-identity-v2"
        or oracle.get("required_status") != "exact_parity"
        or not isinstance(storage, dict)
        or storage.get("provider") != "aws-s3"
        or storage.get("object_prefix") != "full-x7-oracles"
        or storage.get("content_addressed_key") is not True
        or storage.get("conditional_create") is not True
        or storage.get("server_side_encryption") != "AES256"
    ):
        raise ValueError("Oracle capture contract is incomplete or unsafe")
    return value


def build_plan(
    *,
    contract: Mapping[str, Any],
    config_path: str | Path,
    release_candidate_plan_path: str | Path,
    mode: str,
    candidate_commit: str,
    candidate_wheel: str | Path,
    output_directory: str | Path,
    executable: str,
    resume: bool,
) -> dict[str, Any]:
    """Bind a candidate and sealed inputs to one indexed immutable Oracle."""
    if mode not in contract["modes"]:
        raise ValueError(f"unsupported certification mode: {mode}")
    if not COMMIT_PATTERN.fullmatch(candidate_commit):
        raise ValueError("candidate commit must be a lowercase 40-character Git SHA")
    config_source = Path(config_path).resolve()
    config = read_object(config_source, label="protected certification config")
    required_strings = (
        "mode",
        "release_lock",
        "execution_profile",
        "strategy",
        "strategy_class",
        "config",
        "data_directory",
        "engine_markets",
        "oracle_index",
        "host_lock",
    )
    config_version = config.get("schema_version")
    fingerprint_field = (
        "oracle_fingerprint"
        if config_version == "1.0.0"
        else "oracle_input_fingerprint"
    )
    if (
        config_version not in {"1.0.0", "2.0.0"}
        or any(not _nonempty_string(config.get(name)) for name in required_strings)
        or not _nonempty_string(config.get(fingerprint_field))
        or config["mode"] != mode
        or not _unique_strings(config.get("state_probes"))
        or not isinstance(config.get("swap_cap_bytes"), int)
        or isinstance(config.get("swap_cap_bytes"), bool)
        or config["swap_cap_bytes"] <= 0
        or (
            config.get("reference_markets") is not None
            and not _nonempty_string(config.get("reference_markets"))
        )
        or not SHA256_PATTERN.fullmatch(config[fingerprint_field])
    ):
        raise ValueError("protected certification config is incomplete or inconsistent")

    wheel = Path(candidate_wheel).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise ValueError(f"candidate wheel does not exist: {wheel}")
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()) and not resume:
        raise ValueError("non-empty certification output requires explicit resume")
    execution_profile = Path(config["execution_profile"]).resolve()
    data_directory = Path(config["data_directory"]).resolve()
    state_probes = [Path(value).resolve() for value in config["state_probes"]]
    if not execution_profile.is_file():
        raise ValueError("execution profile does not exist")
    if not data_directory.is_dir():
        raise ValueError("certification data directory does not exist")
    if any(not probe.is_file() for probe in state_probes):
        raise ValueError("one or more state probe manifests do not exist")
    declared_state_probes = _load_release_candidate_probes(
        release_candidate_plan_path,
        mode=mode,
    )
    configured_probe_hashes = [sha256_file(path) for path in state_probes]
    declared_probe_hashes = [
        record["sha256"] for record in declared_state_probes
    ]
    if (
        len(configured_probe_hashes) != len(declared_probe_hashes)
        or set(configured_probe_hashes) != set(declared_probe_hashes)
    ):
        raise ValueError(
            "protected state probes differ from the sealed release-candidate plan"
        )
    state_probes = [record["path"] for record in declared_state_probes]
    if Path(config["host_lock"]).resolve().is_dir():
        raise ValueError("host lock path must not be a directory")

    input_identity = _input_identity(
        config,
        version=(
            contract["oracle"]["input_identity_schema_version"]
            if config_version == "2.0.0"
            else "oracle-input-identity-v1"
        ),
    )
    fingerprint = canonical_sha256(input_identity)
    if fingerprint != config[fingerprint_field]:
        raise ValueError("configured Oracle fingerprint differs from sealed inputs")
    oracle = _lookup_oracle(
        Path(config["oracle_index"]).resolve(),
        contract=contract,
        mode=mode,
        fingerprint=fingerprint,
        input_identity=input_identity,
    )
    oracle_directory = Path(oracle["directory"]).resolve()
    run_report = oracle_directory / "run.json"
    if not run_report.is_file():
        raise ValueError(f"indexed Oracle has no run report: {oracle_directory}")
    if sha256_file(run_report) != oracle["run_json_sha256"]:
        raise ValueError("indexed Oracle run report differs from its immutable record")

    candidate_wheel_record = {
        "path": str(wheel),
        "bytes": wheel.stat().st_size,
        "sha256": sha256_file(wheel),
    }
    candidate_identity = {
        "schema_version": contract["candidate"]["identity_schema_version"],
        "candidate_commit": candidate_commit,
        "candidate_wheel_sha256": candidate_wheel_record["sha256"],
        "mode": mode,
        "oracle_input_fingerprint": fingerprint,
        "release_candidate_plan_sha256": sha256_file(
            Path(release_candidate_plan_path).resolve()
        ),
        "state_probe_sha256": [record["sha256"] for record in declared_state_probes],
        "swap_cap_bytes": config["swap_cap_bytes"],
    }
    candidate_fingerprint = canonical_sha256(candidate_identity)

    command = [
        executable,
        "certify",
        str(Path(config["release_lock"]).resolve()),
        "--certification-profile",
        "full-x7",
        "--output-dir",
        str(output),
        "--profile",
        str(execution_profile),
        "--strategy",
        str(Path(config["strategy"]).resolve()),
        "--class-name",
        config["strategy_class"],
        "--config",
        str(Path(config["config"]).resolve()),
        "--data-dir",
        str(data_directory),
        "--engine-markets",
        str(Path(config["engine_markets"]).resolve()),
        "--official-oracle",
        str(oracle_directory),
        "--wheel",
        str(wheel),
        "--swap-cap-gib",
        str(config["swap_cap_bytes"] / 1024**3),
    ]
    reference_markets = config.get("reference_markets")
    if reference_markets is not None:
        command.extend(
            ["--reference-markets", str(Path(reference_markets).resolve())]
        )
    for probe in state_probes:
        command.extend(["--state-probe", str(probe)])
    if resume:
        command.append("--resume")
    return {
        "schema_version": "2.0.0",
        "candidate_commit": candidate_commit,
        "candidate_wheel": candidate_wheel_record,
        "candidate_certification": {
            "fingerprint": candidate_fingerprint,
            "identity": candidate_identity,
        },
        "mode": mode,
        "resume": resume,
        "output_directory": str(output),
        "host_lock": str(Path(config["host_lock"]).resolve()),
        "oracle": {
            "fingerprint": fingerprint,
            "input_fingerprint": fingerprint,
            "input_identity": input_identity,
            "directory": str(oracle_directory),
            "run_json_sha256": oracle["run_json_sha256"],
            "tree_sha256": oracle["tree_sha256"],
            "status": oracle["status"],
            "immutable": oracle["immutable"],
            "reused": True,
            "new_run_allowed": False,
        },
        "state_probes": [
            {
                "path": str(record["path"]),
                "sha256": record["sha256"],
            }
            for record in declared_state_probes
        ],
        "command": command,
    }


def build_oracle_capture_plan(
    *,
    contract: Mapping[str, Any],
    config_path: str | Path,
    release_candidate_plan_path: str | Path,
    mode: str,
    candidate_commit: str,
    candidate_wheel: str | Path,
    output_directory: str | Path,
    executable: str,
    resume: bool,
) -> dict[str, Any]:
    """Plan one protected Oracle-only run without adding candidate identity to it."""
    if mode not in contract["modes"]:
        raise ValueError(f"unsupported Oracle capture mode: {mode}")
    if not COMMIT_PATTERN.fullmatch(candidate_commit):
        raise ValueError("candidate commit must be a lowercase 40-character Git SHA")
    config_source = Path(config_path).resolve()
    config = read_object(config_source, label="protected certification config")
    required_strings = (
        "mode",
        "release_lock",
        "execution_profile",
        "strategy",
        "strategy_class",
        "config",
        "data_directory",
        "engine_markets",
        "oracle_index",
        "oracle_input_fingerprint",
        "host_lock",
    )
    if (
        config.get("schema_version") != "2.0.0"
        or any(not _nonempty_string(config.get(name)) for name in required_strings)
        or config["mode"] != mode
        or not _unique_strings(config.get("state_probes"))
        or not isinstance(config.get("swap_cap_bytes"), int)
        or isinstance(config.get("swap_cap_bytes"), bool)
        or config["swap_cap_bytes"] <= 0
        or (
            config.get("reference_markets") is not None
            and not _nonempty_string(config.get("reference_markets"))
        )
        or not SHA256_PATTERN.fullmatch(config["oracle_input_fingerprint"])
    ):
        raise ValueError("protected Oracle capture config is incomplete or inconsistent")
    wheel = Path(candidate_wheel).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise ValueError(f"candidate wheel does not exist: {wheel}")
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()) and not resume:
        raise ValueError("non-empty Oracle capture output requires explicit resume")
    execution_profile = Path(config["execution_profile"]).resolve()
    data_directory = Path(config["data_directory"]).resolve()
    if not execution_profile.is_file() or not data_directory.is_dir():
        raise ValueError("Oracle capture profile or data directory does not exist")
    if Path(config["host_lock"]).resolve().is_dir():
        raise ValueError("host lock path must not be a directory")
    declared_state_probes = _load_release_candidate_probes(
        release_candidate_plan_path,
        mode=mode,
    )
    configured_probe_hashes = [
        sha256_file(Path(value).resolve()) for value in config["state_probes"]
    ]
    declared_probe_hashes = [record["sha256"] for record in declared_state_probes]
    if (
        len(configured_probe_hashes) != len(declared_probe_hashes)
        or set(configured_probe_hashes) != set(declared_probe_hashes)
    ):
        raise ValueError("protected state probes differ from the sealed release-candidate plan")
    input_identity = _input_identity(
        config,
        version=contract["oracle"]["input_identity_schema_version"],
    )
    fingerprint = canonical_sha256(input_identity)
    if fingerprint != config["oracle_input_fingerprint"]:
        raise ValueError("configured Oracle fingerprint differs from sealed inputs")
    index_path = Path(config["oracle_index"]).resolve()
    _require_oracle_not_indexed(
        index_path,
        mode=mode,
        fingerprint=fingerprint,
        input_identity=input_identity,
    )
    command = [
        executable,
        "certify",
        str(Path(config["release_lock"]).resolve()),
        "--certification-profile",
        "full-x7",
        "--capture-oracle-only",
        "--output-dir",
        str(output),
        "--profile",
        str(execution_profile),
        "--strategy",
        str(Path(config["strategy"]).resolve()),
        "--class-name",
        config["strategy_class"],
        "--config",
        str(Path(config["config"]).resolve()),
        "--data-dir",
        str(data_directory),
        "--engine-markets",
        str(Path(config["engine_markets"]).resolve()),
        "--wheel",
        str(wheel),
        "--swap-cap-gib",
        str(config["swap_cap_bytes"] / 1024**3),
    ]
    reference_markets = config.get("reference_markets")
    if reference_markets is not None:
        command.extend(["--reference-markets", str(Path(reference_markets).resolve())])
    for probe in declared_state_probes:
        command.extend(["--state-probe", str(probe["path"])])
    if resume:
        command.append("--resume")
    return {
        "schema_version": "1.0.0",
        "mode": mode,
        "host_lock": str(Path(config["host_lock"]).resolve()),
        "resume": resume,
        "oracle": {
            "input_identity": input_identity,
            "input_fingerprint": fingerprint,
            "index": str(index_path),
            "directory": str(output / "warmups" / "reference"),
            "capture_report": str(output / "oracle-capture.json"),
        },
        "execution": {
            "candidate_commit": candidate_commit,
            "candidate_wheel_sha256": sha256_file(wheel),
        },
        "command": command,
    }


def register_oracle_capture(plan_path: str | Path) -> dict[str, Any]:
    """Atomically add one hash-sealed local Oracle record to the v2 index."""
    import fcntl

    plan = read_object(plan_path, label="Oracle capture plan")
    record = build_oracle_capture_record(plan_path)
    oracle = plan["oracle"]
    index_path = Path(oracle["index"]).resolve()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = index_path.with_suffix(f"{index_path.suffix}.lock")
    if index_path.is_symlink() or lock_path.is_symlink():
        raise ValueError("Oracle index and lock must not be symlinks")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        index = (
            read_object(index_path, label="Oracle fingerprint index")
            if index_path.exists()
            else {"schema_version": "2.0.0", "oracles": []}
        )
        if index.get("schema_version") != "2.0.0" or not isinstance(
            index.get("oracles"), list
        ):
            raise ValueError("Oracle capture requires a v2 fingerprint index")
        matching = [
            item
            for item in index["oracles"]
            if isinstance(item, dict)
            and item.get("mode") == record["mode"]
            and item.get("input_fingerprint") == record["input_fingerprint"]
        ]
        if matching:
            if matching != [record]:
                raise ValueError("Oracle fingerprint is already registered with other bytes")
            return {**record, "registration_status": "reused"}
        index["oracles"].append(record)
        index["oracles"].sort(
            key=lambda item: (item["mode"], item["input_fingerprint"])
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=index_path.parent,
            prefix=f".{index_path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(index, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.replace(temporary_path, index_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return {**record, "registration_status": "created"}


def build_oracle_capture_record(plan_path: str | Path) -> dict[str, Any]:
    """Hash one completed capture without mutating its protected local index."""
    plan = read_object(plan_path, label="Oracle capture plan")
    oracle = plan.get("oracle")
    if (
        plan.get("schema_version") != "1.0.0"
        or plan.get("mode") not in {"spot", "futures"}
        or not isinstance(oracle, dict)
        or not SHA256_PATTERN.fullmatch(str(oracle.get("input_fingerprint", "")))
        or not isinstance(oracle.get("input_identity"), dict)
        or canonical_sha256(oracle["input_identity"]) != oracle["input_fingerprint"]
        or not _nonempty_string(oracle.get("index"))
        or not _nonempty_string(oracle.get("directory"))
        or not _nonempty_string(oracle.get("capture_report"))
    ):
        raise ValueError("Oracle capture plan is incomplete or has identity drift")
    directory = Path(oracle["directory"]).resolve()
    run_report = directory / "run.json"
    capture_report = Path(oracle["capture_report"]).resolve()
    capture = read_object(capture_report, label="Oracle capture report")
    if (
        capture.get("status") != "exact_parity"
        or capture.get("complete") is not True
        or not SHA256_PATTERN.fullmatch(str(capture.get("result_sha256", "")))
        or not run_report.is_file()
    ):
        raise ValueError("Oracle capture did not complete exact parity")
    record = {
        "mode": plan["mode"],
        "input_fingerprint": oracle["input_fingerprint"],
        "input_identity": oracle["input_identity"],
        "directory": str(directory),
        "run_json_sha256": sha256_file(run_report),
        "tree_sha256": directory_tree_sha256(directory),
        "capture_report_sha256": sha256_file(capture_report),
        "status": "exact_parity",
        "immutable": True,
    }
    return record


def _require_oracle_not_indexed(
    index_path: Path,
    *,
    mode: str,
    fingerprint: str,
    input_identity: Mapping[str, Any],
) -> None:
    if not index_path.exists():
        return
    if index_path.is_symlink():
        raise ValueError("Oracle index must not be a symlink")
    index = read_object(index_path, label="Oracle fingerprint index")
    if index.get("schema_version") != "2.0.0" or not isinstance(
        index.get("oracles"), list
    ):
        raise ValueError("Oracle capture requires a v2 fingerprint index")
    matching = [
        record
        for record in index["oracles"]
        if isinstance(record, dict)
        and record.get("mode") == mode
        and record.get("input_fingerprint") == fingerprint
    ]
    if matching:
        if len(matching) != 1 or matching[0].get("input_identity") != input_identity:
            raise ValueError("Oracle fingerprint index contains conflicting records")
        raise ValueError("exact Oracle input is already indexed; reuse it for certification")


def _load_release_candidate_probes(
    plan_path: str | Path,
    *,
    mode: str,
) -> list[dict[str, Any]]:
    path = Path(plan_path).resolve()
    plan = read_object(path, label="release-candidate plan")
    probes = plan.get("certification_probes")
    modes = probes.get("modes") if isinstance(probes, dict) else None
    if (
        plan.get("schema_version") != "1.0.0"
        or not isinstance(modes, list)
    ):
        raise ValueError("release-candidate plan has no certification probes")
    matches = [
        item
        for item in modes
        if isinstance(item, dict) and item.get("slug") == mode
    ]
    if len(matches) != 1:
        raise ValueError(
            "release-candidate plan must contain exactly one selected mode"
        )
    selected = matches[0]
    manifests = selected.get("manifests")
    required = selected.get("required_manifests")
    if (
        not isinstance(manifests, list)
        or not isinstance(required, int)
        or isinstance(required, bool)
        or required < 1
        or len(manifests) != required
    ):
        raise ValueError(
            "release-candidate certification probe count is invalid"
        )
    result: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for record in manifests:
        if (
            not isinstance(record, dict)
            or not _nonempty_string(record.get("manifest"))
            or not SHA256_PATTERN.fullmatch(
                str(record.get("manifest_sha256", ""))
            )
        ):
            raise ValueError(
                "release-candidate certification probe record is invalid"
            )
        relative = Path(record["manifest"])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != record["manifest"]
        ):
            raise ValueError(
                "release-candidate certification probe path is unsafe"
            )
        manifest = (path.parent / relative).resolve()
        if (
            not manifest.is_relative_to(path.parent)
            or manifest in seen_paths
            or not manifest.is_file()
            or sha256_file(manifest) != record["manifest_sha256"]
        ):
            raise ValueError(
                "release-candidate certification probe failed hash validation"
            )
        seen_paths.add(manifest)
        result.append(
            {
                "path": manifest,
                "sha256": record["manifest_sha256"],
            }
        )
    return result


def _input_identity(
    config: Mapping[str, Any],
    *,
    version: str = "oracle-input-identity-v1",
) -> dict[str, Any]:
    release_lock_path = Path(str(config["release_lock"])).resolve()
    release_lock = read_object(release_lock_path, label="release input lock")
    strategy = Path(str(config["strategy"])).resolve()
    selected_config = Path(str(config["config"])).resolve()
    engine_markets = Path(str(config["engine_markets"])).resolve()
    reference_value = config.get("reference_markets")
    reference_markets = (
        Path(str(reference_value)).resolve() if reference_value is not None else None
    )
    required = (strategy, selected_config, engine_markets)
    if any(not path.is_file() for path in required):
        raise ValueError("one or more sealed certification input files do not exist")
    if reference_markets is not None and not reference_markets.is_file():
        raise ValueError("reference market snapshot does not exist")
    identity_sha = release_lock.get("identity_sha256")
    data = release_lock.get("data")
    reference = release_lock.get("reference")
    if (
        not isinstance(identity_sha, str)
        or not SHA256_PATTERN.fullmatch(identity_sha)
        or not isinstance(data, dict)
        or not SHA256_PATTERN.fullmatch(str(data.get("aggregate_sha256", "")))
        or not isinstance(reference, dict)
        or not _nonempty_string(reference.get("image_platform_digest"))
    ):
        raise ValueError("release input lock lacks certification identities")
    identity = {
        "mode": config["mode"],
        "release_lock_sha256": sha256_file(release_lock_path),
        "release_lock_identity_sha256": identity_sha,
        "strategy_sha256": sha256_file(strategy),
        "config_file_sha256": sha256_file(selected_config),
        "data_aggregate_sha256": data["aggregate_sha256"],
        "engine_markets_sha256": sha256_file(engine_markets),
        "reference_markets_sha256": (
            sha256_file(reference_markets) if reference_markets is not None else None
        ),
        "reference_image_digest": reference["image_platform_digest"],
    }
    if version == "oracle-input-identity-v1":
        return identity
    if version != "oracle-input-identity-v2":
        raise ValueError(f"unsupported Oracle input identity schema: {version}")
    return {"schema_version": version, **identity}


def _lookup_oracle(
    index_path: Path,
    *,
    contract: Mapping[str, Any],
    mode: str,
    fingerprint: str,
    input_identity: Mapping[str, Any],
) -> dict[str, Any]:
    index = read_object(index_path, label="Oracle fingerprint index")
    records = index.get("oracles")
    index_version = index.get("schema_version")
    if index_version not in contract["oracle"]["index_schema_versions"] or not isinstance(
        records, list
    ):
        raise ValueError("Oracle fingerprint index has an unsupported schema")
    matches = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("mode") == mode
        and (
            record.get("fingerprint")
            if index_version == "1.0.0"
            else record.get("input_fingerprint")
        )
        == fingerprint
    ]
    if len(matches) != 1:
        raise ValueError("Oracle fingerprint lookup must resolve exactly one record")
    record = matches[0]
    if (
        (
            record.get("identity")
            if index_version == "1.0.0"
            else record.get("input_identity")
        )
        != input_identity
        or record.get("status") != contract["oracle"]["required_status"]
        or record.get("immutable") is not True
        or not _nonempty_string(record.get("directory"))
        or not SHA256_PATTERN.fullmatch(str(record.get("run_json_sha256", "")))
        or not SHA256_PATTERN.fullmatch(str(record.get("tree_sha256", "")))
    ):
        raise ValueError("indexed Oracle record is incomplete, mutable, or mismatched")
    if directory_tree_sha256(Path(record["directory"]).resolve()) != record["tree_sha256"]:
        raise ValueError("indexed Oracle directory differs from its immutable tree seal")
    return record


def directory_tree_sha256(directory: Path) -> str:
    if not directory.is_dir():
        raise ValueError(f"Oracle directory does not exist: {directory}")
    records = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Oracle directory contains a symlink: {path}")
        if path.is_file():
            records.append(
                {
                    "path": path.relative_to(directory).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    if not records:
        raise ValueError("Oracle directory is empty")
    return canonical_sha256(records)


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _unique_strings(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_nonempty_string(item) for item in value)
        and len(set(value)) == len(value)
    )


def write_json(path: str | Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser()
    command.add_argument(
        "--contract",
        type=Path,
        default=Path(".github/long-certification-contract.json"),
    )
    commands = command.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--config", type=Path, required=True)
    plan.add_argument(
        "--release-candidate-plan",
        type=Path,
        required=True,
    )
    plan.add_argument("--mode", required=True)
    plan.add_argument("--candidate-commit", required=True)
    plan.add_argument("--candidate-wheel", type=Path, required=True)
    plan.add_argument("--output-directory", type=Path, required=True)
    plan.add_argument("--executable", required=True)
    plan.add_argument("--resume", action="store_true")
    plan.add_argument("--output", type=Path, required=True)
    capture = commands.add_parser("capture-plan")
    capture.add_argument("--config", type=Path, required=True)
    capture.add_argument("--release-candidate-plan", type=Path, required=True)
    capture.add_argument("--mode", required=True)
    capture.add_argument("--candidate-commit", required=True)
    capture.add_argument("--candidate-wheel", type=Path, required=True)
    capture.add_argument("--output-directory", type=Path, required=True)
    capture.add_argument("--executable", required=True)
    capture.add_argument("--resume", action="store_true")
    capture.add_argument("--output", type=Path, required=True)
    register = commands.add_parser("register-oracle")
    register.add_argument("--plan", type=Path, required=True)
    register.add_argument("--output", type=Path, required=True)
    seal = commands.add_parser("seal-oracle")
    seal.add_argument("--plan", type=Path, required=True)
    seal.add_argument("--output", type=Path, required=True)
    return command


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    contract = (
        load_oracle_capture_contract(args.contract)
        if args.command in {"capture-plan", "register-oracle", "seal-oracle"}
        else load_contract(args.contract)
    )
    if args.command == "plan":
        plan = build_plan(
            contract=contract,
            config_path=args.config,
            release_candidate_plan_path=args.release_candidate_plan,
            mode=args.mode,
            candidate_commit=args.candidate_commit,
            candidate_wheel=args.candidate_wheel,
            output_directory=args.output_directory,
            executable=args.executable,
            resume=args.resume,
        )
        write_json(args.output, plan)
        print(
            json.dumps(
                {
                    "candidate_commit": plan["candidate_commit"],
                    "mode": plan["mode"],
                    "oracle_fingerprint": plan["oracle"]["fingerprint"],
                    "oracle_reused": plan["oracle"]["reused"],
                    "resume": plan["resume"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "capture-plan":
        plan = build_oracle_capture_plan(
            contract=contract,
            config_path=args.config,
            release_candidate_plan_path=args.release_candidate_plan,
            mode=args.mode,
            candidate_commit=args.candidate_commit,
            candidate_wheel=args.candidate_wheel,
            output_directory=args.output_directory,
            executable=args.executable,
            resume=args.resume,
        )
        write_json(args.output, plan)
        print(
            json.dumps(
                {
                    "mode": plan["mode"],
                    "oracle_input_fingerprint": plan["oracle"][
                        "input_fingerprint"
                    ],
                    "resume": plan["resume"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "register-oracle":
        record = register_oracle_capture(args.plan)
        write_json(args.output, record)
        print(
            json.dumps(
                {
                    "mode": record["mode"],
                    "oracle_input_fingerprint": record["input_fingerprint"],
                    "registration_status": record["registration_status"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "seal-oracle":
        record = build_oracle_capture_record(args.plan)
        write_json(args.output, record)
        print(
            json.dumps(
                {
                    "mode": record["mode"],
                    "oracle_input_fingerprint": record["input_fingerprint"],
                    "tree_sha256": record["tree_sha256"],
                },
                sort_keys=True,
            )
        )
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
