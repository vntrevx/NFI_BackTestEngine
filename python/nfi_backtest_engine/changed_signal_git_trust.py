"""Replacement-immune Git object authority for changed-signal source."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .changed_signal_git_config import (
    active_private_git_configuration,
    private_git_configuration,
)
from .changed_signal_git_environment import (
    PINNED_GIT_VERSION,
    active_git_rewrite_environment,
)
from .errors import SpecValidationError
from .windows_path_security import windows_root_identity


@dataclass(frozen=True, slots=True)
class UpstreamSourceContract:
    """Immutable repository, ref, commit, tree, path, and blob identity."""

    repository_url: str
    git_directory: Path
    current_refs: tuple[str, ...]
    commit: str
    tree_oid: str
    source_path: str
    blob_oid: str


UPSTREAM_SOURCE_CONTRACT: Final = UpstreamSourceContract(
    repository_url="https://github.com/iterativv/NostalgiaForInfinity.git",
    git_directory=Path(".nfi/upstream-nfi"),
    current_refs=("refs/remotes/origin/main", "refs/heads/main"),
    commit="eebaf97c1434bd8f208b7cd9c417606646e1e478",
    tree_oid="75f1d10af297266e27bec6052ed913a389dc7458",
    source_path="NostalgiaForInfinityX7.py",
    blob_oid="9f1af9f07886738e888925e348ca353e83e3b59e",
)


def resolve_upstream_source(repository_root: Path) -> bytes:
    """Return canonical blob bytes only from the configured unmodified object graph."""
    contract = UPSTREAM_SOURCE_CONTRACT
    git_root = (
        contract.git_directory
        if contract.git_directory.is_absolute()
        else repository_root / contract.git_directory
    )
    _validate_repository_layout(git_root)
    _validate_process_environment()
    if _git(git_root, ("--version",)).decode().strip() != PINNED_GIT_VERSION:
        raise SpecValidationError("changed signal Git executable version differs")
    git_directory = git_root / ".git"
    with private_git_configuration(git_root) as local_config:
        _validate_object_overrides(git_root, git_directory, local_config)
        if _git(git_root, ("rev-parse", "--is-shallow-repository")) != b"false\n":
            raise SpecValidationError("changed signal upstream Git repository is shallow")
        if _git(git_root, ("rev-parse", "--show-object-format")) != b"sha1\n":
            raise SpecValidationError("changed signal upstream Git object format differs")
        if local_config.get("remote.origin.url") != [contract.repository_url]:
            raise SpecValidationError("changed signal upstream Git repository identity differs")
        for current_ref in contract.current_refs:
            resolved_ref = _git(git_root, ("rev-parse", current_ref)).decode().strip()
            if resolved_ref != contract.commit:
                raise SpecValidationError("changed signal configured upstream ref moved")
        resolved_commit = (
            _git(
                git_root,
                ("rev-parse", f"{contract.commit}^{{commit}}"),
            )
            .decode()
            .strip()
        )
        if resolved_commit != contract.commit:
            raise SpecValidationError("changed signal upstream Git commit differs")
        tree_oid = (
            _git(
                git_root,
                ("rev-parse", f"{contract.commit}^{{tree}}"),
            )
            .decode()
            .strip()
        )
        if tree_oid != contract.tree_oid:
            raise SpecValidationError("changed signal upstream Git tree differs")
        blob_oid = (
            _git(
                git_root,
                ("rev-parse", f"{contract.commit}:{contract.source_path}"),
            )
            .decode()
            .strip()
        )
        if blob_oid != contract.blob_oid:
            raise SpecValidationError("changed signal upstream Git path or blob differs")
        _verify_object(git_root, contract.commit, "commit")
        _verify_object(git_root, tree_oid, "tree")
        return _verify_object(git_root, blob_oid, "blob")


def _validate_repository_layout(git_root: Path) -> None:
    lexical = git_root.absolute()
    git_directory = lexical / ".git"
    if os.name == "nt":
        try:
            windows_root_identity(lexical)
            windows_root_identity(git_directory)
        except SpecValidationError as exc:
            raise SpecValidationError(
                "changed signal upstream Git repository is missing"
            ) from exc
        return
    if (
        lexical.is_symlink()
        or lexical.resolve() != lexical
        or not lexical.is_dir()
        or git_directory.is_symlink()
        or git_directory.resolve() != git_directory
        or not git_directory.is_dir()
    ):
        raise SpecValidationError("changed signal upstream Git repository is missing")


def _validate_process_environment() -> None:
    if active_git_rewrite_environment(os.environ):
        raise SpecValidationError("changed signal upstream Git environment rewrites objects")


def _validate_object_overrides(
    git_root: Path,
    git_directory: Path,
    local_config: dict[str, list[str]],
) -> None:
    replacements = _git(git_root, ("for-each-ref", "--format=%(refname)", "refs/replace"))
    if replacements:
        raise SpecValidationError("changed signal upstream Git replacement ref is active")
    grafts = git_directory / "info/grafts"
    alternates = git_directory / "objects/info/alternates"
    if (grafts.is_file() and grafts.stat().st_size) or (
        alternates.is_file() and alternates.stat().st_size
    ):
        raise SpecValidationError("changed signal upstream Git object override is active")
    forbidden_fragments = ("replace", "graft", "alternate", "objectdirectory")
    if any(fragment in name for name in local_config for fragment in forbidden_fragments):
        raise SpecValidationError("changed signal upstream Git config rewrites objects")


def _verify_object(git_root: Path, oid: str, expected_type: str) -> bytes:
    actual_type = _git(git_root, ("cat-file", "-t", oid)).decode().strip()
    if actual_type != expected_type:
        raise SpecValidationError("changed signal upstream Git object type differs")
    payload = _git(git_root, ("cat-file", expected_type, oid))
    verified_oid = (
        _git(
            git_root,
            ("hash-object", "-t", expected_type, "--stdin"),
            input_bytes=payload,
        )
        .decode()
        .strip()
    )
    if verified_oid != oid:
        raise SpecValidationError("changed signal upstream Git object is unverifiable")
    return payload


def _git(
    git_root: Path,
    arguments: tuple[str, ...],
    input_bytes: bytes | None = None,
) -> bytes:
    environment = {
        "GIT_CONFIG_COUNT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", os.defpath),
    }
    private_config = active_private_git_configuration()
    if private_config is not None:
        environment["GIT_CONFIG"] = private_config.as_posix()
    try:
        completed = subprocess.run(
            ("git", "--no-replace-objects", "-C", git_root.as_posix(), *arguments),
            input=input_bytes,
            capture_output=True,
            check=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SpecValidationError("changed signal upstream Git object is unavailable") from exc
    return completed.stdout
