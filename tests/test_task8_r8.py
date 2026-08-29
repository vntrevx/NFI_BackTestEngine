from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.changed_signal_git_environment import (
    GIT_CONFIG_SELECTOR_ENVIRONMENT,
    GIT_REPOSITORY_DISCOVERY_ENVIRONMENT,
    GIT_REPOSITORY_LOCAL_ENVIRONMENT,
)
from nfi_backtest_engine.changed_signal_proof import (
    ChangedSignalIdentity,
    validate_changed_signal_proof,
)
from nfi_backtest_engine.errors import SpecValidationError
from task8_trust_support import PROOF, attack_root

_INSTALLED_LOCAL_ENVIRONMENT = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)
_PRODUCTION_ENVIRONMENT_MATRIX = (
    *_INSTALLED_LOCAL_ENVIRONMENT,
    *sorted(GIT_CONFIG_SELECTOR_ENVIRONMENT),
    *sorted(GIT_REPOSITORY_DISCOVERY_ENVIRONMENT),
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
)


def _validate() -> None:
    document = read_json(PROOF)
    validate_changed_signal_proof(
        document,
        ChangedSignalIdentity(**document["identity"]),
    )


def test_pinned_environment_inventory_matches_installed_git_contract() -> None:
    # Given / When / Then: code owns the exact Git 2.43 local environment output.
    assert frozenset(
        _INSTALLED_LOCAL_ENVIRONMENT
    ) == GIT_REPOSITORY_LOCAL_ENVIRONMENT


def test_promotion_rejects_nonempty_git_config_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the exact installed-Git repository-local config selector is active.
    attack_root(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_CONFIG", "/dev/null")

    # When / Then: promotion must reject caller policy before child sanitization.
    with pytest.raises(SpecValidationError, match="environment|config|Git"):
        _validate()


@pytest.mark.parametrize("variable", _PRODUCTION_ENVIRONMENT_MATRIX)
def test_promotion_rejects_pinned_git_environment_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    # Given: one code-owned repository-local/config-selector variable is nonempty.
    attack_root(tmp_path, monkeypatch)
    monkeypatch.setenv(variable, "attacker")

    # When / Then: every listed input rejects at the production proof boundary.
    with pytest.raises(SpecValidationError, match="environment|config|Git"):
        _validate()


def test_promotion_accepts_empty_git_matrix_and_unrelated_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: every forbidden name is explicitly empty and unrelated input is nonempty.
    attack_root(tmp_path, monkeypatch)
    for variable in _PRODUCTION_ENVIRONMENT_MATRIX:
        monkeypatch.setenv(variable, "")
    monkeypatch.setenv("TODO8_R8_BENIGN_ENVIRONMENT", "present")

    # When / Then: only nonempty Git policy inputs are rejected.
    _validate()
