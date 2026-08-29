from __future__ import annotations

import os
import subprocess
from pathlib import Path
from threading import Event, Thread

import pytest
from nfi_backtest_engine import changed_signal_git_trust
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.changed_signal_filesystem_trust import trusted_file_operation
from nfi_backtest_engine.changed_signal_git_config import (
    active_private_git_configuration,
    private_git_configuration,
)
from nfi_backtest_engine.changed_signal_git_trust import resolve_upstream_source
from nfi_backtest_engine.changed_signal_proof import (
    ChangedSignalIdentity,
    validate_changed_signal_proof,
)
from nfi_backtest_engine.errors import SpecValidationError
from task8_trust_support import PROOF, isolated_git_attack_root


def _validate() -> None:
    document = read_json(PROOF)
    validate_changed_signal_proof(document, ChangedSignalIdentity(**document["identity"]))


def _git_config(git_root: Path) -> Path:
    return git_root / ".git/config"


def _append_config(git_root: Path, value: str) -> None:
    with _git_config(git_root).open("a", encoding="utf-8") as stream:
        stream.write(value)


def test_promotion_rejects_external_local_config_include(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: direct local config imports an external file that changes Git resolution.
    root = isolated_git_attack_root(tmp_path, monkeypatch)
    git_root = root / ".nfi/upstream-nfi"
    external = tmp_path / "attacker.config"
    external.write_text("[attacker]\n\tselected = true\n", encoding="utf-8")
    subprocess.run(
        (
            "git",
            "-C",
            git_root.as_posix(),
            "config",
            "--local",
            "include.path",
            external.as_posix(),
        ),
        check=True,
    )
    effective = subprocess.run(
        ("git", "-C", git_root.as_posix(), "config", "--get", "attacker.selected"),
        check=True,
        capture_output=True,
        text=True,
    )
    assert effective.stdout.strip() == "true"

    # When / Then: promotion rejects before the external config can become authority.
    with pytest.raises(SpecValidationError, match="include|config|Git"):
        _validate()


@pytest.mark.parametrize(
    "selector",
    [
        '[include]\n\tpath = "{absolute}"\n',
        '[include]\n\tpath = ../../../attacker.config\n',
        '[InClUdE]\n\tPaTh = "{absolute}"\n',
        '[includeIf "gitdir:{git_root}/.git/"]\n\tpath = "{absolute}"\n',
        '[includeIf "gitdir/i:{git_root}/.git/"]\n\tPaTh = "{absolute}"\n',
        '[includeIf "onbranch:**"]\n\tpath = "{absolute}"\n',
    ],
)
def test_git_authority_rejects_direct_include_selector_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
) -> None:
    root = isolated_git_attack_root(tmp_path, monkeypatch)
    git_root = root / ".nfi/upstream-nfi"
    external = tmp_path / "attacker.config"
    external.write_text("[attacker]\nselected = true\n", encoding="utf-8")
    _append_config(
        git_root,
        selector.format(absolute=external.as_posix(), git_root=git_root.as_posix()),
    )

    with pytest.raises(SpecValidationError, match="include|config"):
        resolve_upstream_source(root)


@pytest.mark.parametrize("variant", ["multiple", "nested"])
def test_git_authority_rejects_multiple_and_nested_includes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    root = isolated_git_attack_root(tmp_path, monkeypatch)
    git_root = root / ".nfi/upstream-nfi"
    external = tmp_path / "external.config"
    external.write_text("[attacker]\nselected = true\n", encoding="utf-8")
    if variant == "multiple":
        _append_config(
            git_root,
            f"[include]\npath = {external}\npath = {external}\n",
        )
    else:
        nested = tmp_path / "nested.config"
        nested.write_text(f"[include]\npath = {external}\n", encoding="utf-8")
        _append_config(git_root, f"[include]\npath = {nested}\n")

    with pytest.raises(SpecValidationError, match="include|config"):
        resolve_upstream_source(root)


def test_git_authority_accepts_clean_ordinary_local_config_and_cleans_private_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = isolated_git_attack_root(tmp_path, monkeypatch)
    git_root = root / ".nfi/upstream-nfi"

    with private_git_configuration(git_root) as entries:
        private = active_private_git_configuration()
        assert private is not None and private.read_bytes() == b""
        private_root = private.parent
        assert entries["remote.origin.url"] == [
            changed_signal_git_trust.UPSTREAM_SOURCE_CONTRACT.repository_url
        ]
    assert active_private_git_configuration() is None
    assert not private_root.exists()
    assert resolve_upstream_source(root)


@pytest.mark.parametrize("rotation", ["path", "content", "metadata"])
def test_promotion_rejects_event_synchronized_local_config_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rotation: str,
) -> None:
    root = isolated_git_attack_root(tmp_path, monkeypatch)
    git_root = root / ".nfi/upstream-nfi"
    config = _git_config(git_root)
    payload = config.read_bytes()
    rotate = Event()
    replaced = Event()
    failures: list[OSError] = []

    def replace_config() -> None:
        assert rotate.wait(timeout=10)
        try:
            if rotation == "path":
                candidate = config.with_name("config.r9-rotation")
                candidate.write_bytes(payload)
                os.replace(candidate, config)
            elif rotation == "content":
                config.write_bytes(payload + b"\n[attacker]\nselected = true\n")
            else:
                config.chmod(0o600)
        except OSError as exc:
            failures.append(exc)
        finally:
            replaced.set()

    original = changed_signal_git_trust._validate_object_overrides

    def synchronized_validation(
        selected_root: Path,
        git_directory: Path,
        local_config: dict[str, list[str]],
    ) -> None:
        rotate.set()
        assert replaced.wait(timeout=10)
        original(selected_root, git_directory, local_config)

    monkeypatch.setattr(
        changed_signal_git_trust,
        "_validate_object_overrides",
        synchronized_validation,
    )
    worker = Thread(target=replace_config, name="task8-r9-config-rotation")
    worker.start()
    try:
        with (
            pytest.raises(SpecValidationError, match="snapshot|changed|config"),
            trusted_file_operation(),
        ):
            resolve_upstream_source(root)
    finally:
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert not failures
