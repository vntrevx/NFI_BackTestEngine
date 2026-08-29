"""Pinned Git 2.43 repository-local environment contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

PINNED_GIT_VERSION: Final = "git version 2.43.0"
GIT_ENVIRONMENT_CONTRACT_VERSION: Final = "git-2.43.0-local-env-v1"

# Frozen from `git rev-parse --local-env-vars` under PINNED_GIT_VERSION. Runtime
# validation never trusts caller-influenced enumeration to define this boundary.
GIT_REPOSITORY_LOCAL_ENVIRONMENT: Final = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)
GIT_CONFIG_SELECTOR_ENVIRONMENT: Final = frozenset(
    {
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_SYSTEM",
    }
)
GIT_REPOSITORY_DISCOVERY_ENVIRONMENT: Final = frozenset(
    {
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH",
        "GIT_NAMESPACE",
    }
)
GIT_CONFIG_FAMILY_PREFIXES: Final = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
GIT_REWRITE_ENVIRONMENT: Final = (
    GIT_REPOSITORY_LOCAL_ENVIRONMENT
    | GIT_CONFIG_SELECTOR_ENVIRONMENT
    | GIT_REPOSITORY_DISCOVERY_ENVIRONMENT
)


def active_git_rewrite_environment(environment: Mapping[str, str]) -> frozenset[str]:
    """Return every nonempty caller variable forbidden by the pinned contract."""
    active = {
        name
        for name in GIT_REWRITE_ENVIRONMENT.intersection(environment)
        if environment[name]
    }
    active.update(
        name
        for name, value in environment.items()
        if value and name.startswith(GIT_CONFIG_FAMILY_PREFIXES)
    )
    return frozenset(active)
