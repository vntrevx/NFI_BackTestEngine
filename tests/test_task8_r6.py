from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.changed_signal_proof import (
    ChangedSignalIdentity,
    validate_changed_signal_proof,
)
from nfi_backtest_engine.errors import SpecValidationError
from task8_trust_support import (
    OFFICIAL_CAPTURE,
    PROOF,
    SOURCE,
    artifact,
    attack_root,
    isolated_git_attack_root,
    reseal,
)


def _validate() -> None:
    document = read_json(PROOF)
    validate_changed_signal_proof(
        document,
        ChangedSignalIdentity(**document["identity"]),
    )


@pytest.mark.parametrize("roles", [("source",), ("capture",), ("source", "capture")])
def test_promotion_rejects_trusted_hardlink_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    roles: tuple[str, ...],
) -> None:
    # Given: exact trusted bytes remain at each consumed path but an attacker owns
    # another name for the same inode.
    root = attack_root(tmp_path, monkeypatch)
    for role in roles:
        path = root / (SOURCE if role == "source" else OFFICIAL_CAPTURE)
        alias = path.with_suffix(f"{path.suffix}.attacker")
        path.replace(alias)
        os.link(alias, path)

    # When / Then: promotion must reject multi-link trusted files before hashing.
    with pytest.raises(SpecValidationError, match="link|alias|source|capture"):
        _validate()


def test_promotion_rejects_active_equivalent_git_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the immutable commit is replaced by an attacker commit with the same tree,
    # preserving the checked strategy blob while changing the object graph.
    root = isolated_git_attack_root(tmp_path, monkeypatch)
    git_root = root / ".nfi/upstream-nfi"
    commit = "eebaf97c1434bd8f208b7cd9c417606646e1e478"
    tree = subprocess.run(
        ("git", "-C", git_root.as_posix(), "rev-parse", f"{commit}^{{tree}}"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    environment = {
        "PATH": os.environ["PATH"],
        "GIT_AUTHOR_NAME": "attacker",
        "GIT_AUTHOR_EMAIL": "attacker@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_NAME": "attacker",
        "GIT_COMMITTER_EMAIL": "attacker@example.invalid",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    replacement = subprocess.run(
        ("git", "-C", git_root.as_posix(), "commit-tree", tree),
        input="attacker replacement\n",
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    subprocess.run(
        ("git", "-C", git_root.as_posix(), "replace", commit, replacement),
        check=True,
    )

    # When / Then: equivalent replacement graphs are not canonical authority.
    with pytest.raises(SpecValidationError, match="replace|Git|object"):
        _validate()


@pytest.mark.parametrize(
    "current_ref",
    ["refs/heads/main", "refs/remotes/origin/main"],
)
def test_promotion_rejects_configured_upstream_main_ref_movement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_ref: str,
) -> None:
    # Given: the pinned commit remains available while the configured current branch
    # moves to its parent entirely offline.
    root = isolated_git_attack_root(tmp_path, monkeypatch)
    git_root = root / ".nfi/upstream-nfi"
    parent = subprocess.run(
        (
            "git",
            "-C",
            git_root.as_posix(),
            "rev-parse",
            "eebaf97c1434bd8f208b7cd9c417606646e1e478^",
        ),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ("git", "-C", git_root.as_posix(), "update-ref", current_ref, parent),
        check=True,
    )

    # When / Then: current-HEAD movement fails typed even though pinned objects exist.
    with pytest.raises(SpecValidationError, match="ref|head|commit|Git"):
        _validate()


@pytest.mark.parametrize("override", ["graft", "alternate", "config"])
def test_promotion_rejects_git_object_graph_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str,
) -> None:
    # Given: local Git state redirects commit ancestry, object storage, or ref discovery.
    root = isolated_git_attack_root(tmp_path, monkeypatch)
    git_root = root / ".nfi/upstream-nfi"
    git_directory = git_root / ".git"
    if override == "graft":
        (git_directory / "info/grafts").write_text(
            "eebaf97c1434bd8f208b7cd9c417606646e1e478\n",
            encoding="ascii",
        )
    elif override == "alternate":
        (git_directory / "objects/info/alternates").write_text(
            (Path(__file__).resolve().parents[1] / ".git/objects").as_posix() + "\n",
            encoding="utf-8",
        )
    else:
        subprocess.run(
            (
                "git",
                "-C",
                git_root.as_posix(),
                "config",
                "core.alternateRefsCommand",
                "echo attacker",
            ),
            check=True,
        )

    # When / Then: no local graph or object-store override can become authority.
    with pytest.raises(SpecValidationError, match="override|config|Git|object"):
        _validate()


@pytest.mark.parametrize(
    "variable",
    ["GIT_OBJECT_DIRECTORY", "GIT_REPLACE_REF_BASE", "GIT_CONFIG_COUNT"],
)
def test_promotion_rejects_git_object_rewriting_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    # Given: the validator process carries an object/config rewriting Git variable.
    attack_root(tmp_path, monkeypatch)
    monkeypatch.setenv(variable, "attacker")

    # When / Then: production rejects rather than silently sanitizing an active attack.
    with pytest.raises(SpecValidationError, match="environment|Git|object"):
        _validate()


@pytest.mark.parametrize("attack", ["missing", "extra", "path"])
def test_promotion_rejects_resealed_published_artifact_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    # Given: the declared published set and every attacker-controlled outer digest
    # are resealed around a missing, extra, or renamed member.
    root = attack_root(tmp_path, monkeypatch)
    document = read_json(PROOF)
    provenance = document["modes"]["spot"]["native_provenance"]
    path = root / "benchmarks/evidence/m22/current-x7-raw/spot/replay-publication-native.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if attack == "missing":
        manifest["artifacts"].pop()
    elif attack == "extra":
        manifest["artifacts"].append(dict(manifest["artifacts"][0], role="extra"))
    else:
        manifest["artifacts"][0]["path"] = "renamed.json"
    payload = (
        json.dumps(manifest["artifacts"], sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest["artifact_set_sha256"] = hashlib.sha256(payload).hexdigest()
    path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    record = artifact(provenance, "published_manifest")
    record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    record["bytes"] = path.stat().st_size
    reseal(document)

    # When / Then: promotion binds the exact code-owned set, not a manifest-defined set.
    with pytest.raises(SpecValidationError, match="published|artifact set|contract"):
        validate_changed_signal_proof(
            document,
            ChangedSignalIdentity(**document["identity"]),
        )
