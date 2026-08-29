"""Shared fresh-root and resealing support for Todo 8 trust tests."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine import (
    changed_signal_git_trust,
    changed_signal_mutation_validation,
    changed_signal_provenance,
    changed_signal_role_binding,
    changed_signal_validation,
)
from nfi_backtest_engine.changed_signal_role_binding import (
    ReplayRoleBinding,
    resolve_replay_role_bindings,
    role_bindings_sha256,
)

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "benchmarks/evidence/m22/current-x7-changed-signal-boundary.json"
SOURCE = Path("benchmarks/evidence/m22/current-x7-raw/upstream-NostalgiaForInfinityX7.source")
OFFICIAL_CAPTURE = Path("benchmarks/reference/capture/current_changed_signal.py")
NATIVE_CAPTURE = Path("python/nfi_backtest_engine/changed_signal_native_capture.py")
CONTRACT = Path("benchmarks/reference/strategies/CurrentChangedPredicateContract.py")
PROFILE = Path("planning/freqtrade-semantic-profile.json")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def artifact(provenance: dict[str, Any], role: str) -> dict[str, Any]:
    return next(item for item in provenance["artifacts"] if item["role"] == role)


def attack_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy evidence while retaining the canonical repository object authority."""
    return _attack_root(tmp_path, monkeypatch, isolated_git=False)


def isolated_git_attack_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Copy evidence and an independently mutable complete Git object database."""
    return _attack_root(tmp_path, monkeypatch, isolated_git=True)


def _attack_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: bool,
) -> Path:
    shutil.copytree(
        ROOT / "benchmarks/evidence/m22/current-x7-raw",
        tmp_path / "benchmarks/evidence/m22/current-x7-raw",
    )
    for relative in (OFFICIAL_CAPTURE, NATIVE_CAPTURE, CONTRACT, PROFILE):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    contract = changed_signal_git_trust.UPSTREAM_SOURCE_CONTRACT
    if isolated_git:
        upstream = tmp_path / ".nfi/upstream-nfi"
        upstream.parent.mkdir(parents=True)
        source_repository = (
            contract.git_directory
            if contract.git_directory.is_absolute()
            else ROOT / contract.git_directory
        )
        shutil.copytree(source_repository, upstream)
        subprocess.run(
            (
                "git",
                "-C",
                upstream.as_posix(),
                "remote",
                "set-url",
                "origin",
                contract.repository_url,
            ),
            check=True,
        )
        for current_ref in contract.current_refs:
            subprocess.run(
                (
                    "git",
                    "-C",
                    upstream.as_posix(),
                    "update-ref",
                    current_ref,
                    contract.commit,
                ),
                check=True,
            )
        git_directory = upstream
    else:
        git_directory = (ROOT / contract.git_directory).resolve()
    isolated_contract = replace(contract, git_directory=git_directory)
    monkeypatch.setattr(
        changed_signal_git_trust,
        "UPSTREAM_SOURCE_CONTRACT",
        isolated_contract,
    )
    monkeypatch.setattr(
        changed_signal_role_binding,
        "UPSTREAM_SOURCE_CONTRACT",
        isolated_contract,
    )
    monkeypatch.setattr(changed_signal_mutation_validation, "_REPOSITORY", tmp_path)
    monkeypatch.setattr(changed_signal_provenance, "_REPOSITORY", tmp_path)
    monkeypatch.setattr(changed_signal_validation, "_REPOSITORY", tmp_path)
    return tmp_path


def baseline_bindings(
    root: Path,
) -> dict[tuple[str, str], tuple[ReplayRoleBinding, ...]]:
    return {
        (mode, lane): resolve_replay_role_bindings(
            mode,
            lane,
            root / f"benchmarks/evidence/m22/current-x7-raw/{mode}/replay/manifest.json",
            root,
        )
        for mode in ("spot", "futures")
        for lane in ("official", "native")
    }


def reseal_role(
    document: dict[str, Any],
    root: Path,
    role: str,
    baseline: dict[tuple[str, str], tuple[ReplayRoleBinding, ...]],
) -> None:
    for mode_name, mode in document["modes"].items():
        for lane_name in ("official", "native"):
            if role == "capture_input" and lane_name == "native":
                continue
            provenance = mode[f"{lane_name}_provenance"]
            record = artifact(provenance, role)
            path = root / record["path"]
            record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            record["bytes"] = path.stat().st_size
            bindings = tuple(
                replace(
                    binding,
                    sha256=record["sha256"],
                    bytes=record["bytes"],
                )
                if binding.role == role
                else binding
                for binding in baseline[(mode_name, lane_name)]
            )
            provenance["role_bindings_sha256"] = role_bindings_sha256(bindings)


def reseal_publication(
    document: dict[str, Any],
    root: Path,
    target: tuple[str, str],
) -> None:
    """Refresh one attacker-controlled published manifest and its proof record."""
    mode_name, lane_name = target
    provenance = document["modes"][mode_name][f"{lane_name}_provenance"]
    path = root / (
        f"benchmarks/evidence/m22/current-x7-raw/{mode_name}/replay-publication-{lane_name}.json"
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["role_bindings_sha256"] = provenance["role_bindings_sha256"]
    by_role = {item["role"]: item for item in provenance["artifacts"]}
    for record in manifest["artifacts"]:
        proof = by_role[record["role"]]
        record["sha256"] = proof["sha256"]
        record["bytes"] = proof["bytes"]
    manifest["artifact_set_sha256"] = hashlib.sha256(
        (json.dumps(manifest["artifacts"], sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    proof_manifest = artifact(provenance, "published_manifest")
    proof_manifest["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    proof_manifest["bytes"] = path.stat().st_size


def reseal(document: dict[str, Any]) -> None:
    for mode in document["modes"].values():
        for lane_name in ("official", "native"):
            provenance = mode[f"{lane_name}_provenance"]
            provenance["raw_output_sha256"] = canonical_sha256(
                [item["sha256"] for item in provenance["artifacts"]]
            )
            provenance["normalized_sha256"] = canonical_sha256(mode[lane_name])
    unsigned = {key: value for key, value in document.items() if key != "fingerprint"}
    document["fingerprint"] = canonical_sha256(unsigned)
