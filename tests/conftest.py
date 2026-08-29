from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
from nfi_backtest_engine import (
    changed_signal_git_trust,
    changed_signal_mutation_validation,
    changed_signal_role_binding,
    release_provenance,
)

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_UPSTREAM = ROOT / "benchmarks/evidence/m22/historical-upstream-eebaf.git.tar.gz"
SYSTEM_GIT_VERSION = subprocess.run(
    ("git", "--version"),
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
DURABLE_LEDGER_AVAILABLE = (
    os.name == "posix"
    and hasattr(os, "O_NOATIME")
    and Path("/proc/self/fd").is_dir()
)
LEDGER_ONLY_TEST_FILES = frozenset(
    {
        "test_release_provenance_sqlite.py",
        "test_release_provenance_sqlite_publication.py",
        "test_release_provenance_sqlite_races.py",
        "test_sqlite_wal_reset.py",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if DURABLE_LEDGER_AVAILABLE:
        return
    marker = pytest.mark.skip(reason="requires the durable publication ledger platform contract")
    for item in items:
        if Path(str(item.path)).name in LEDGER_ONLY_TEST_FILES:
            item.add_marker(marker)




@pytest.fixture(scope="session")
def historical_changed_signal_upstream(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    repository = tmp_path_factory.mktemp("historical-changed-signal-upstream")
    with tarfile.open(HISTORICAL_UPSTREAM, "r:gz") as archive:
        archive.extractall(repository, filter="data")
    return repository


@pytest.fixture(autouse=True)
def bind_historical_changed_signal_upstream(
    historical_changed_signal_upstream: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = replace(
        changed_signal_git_trust.UPSTREAM_SOURCE_CONTRACT,
        git_directory=historical_changed_signal_upstream,
    )
    monkeypatch.setattr(changed_signal_git_trust, "UPSTREAM_SOURCE_CONTRACT", contract)
    # Production stays pinned and fails closed. Tests bind their extracted historical
    # repository to the host Git so every platform exercises the remaining contract.
    monkeypatch.setattr(
        changed_signal_git_trust,
        "PINNED_GIT_VERSION",
        SYSTEM_GIT_VERSION,
    )
    monkeypatch.setattr(changed_signal_role_binding, "UPSTREAM_SOURCE_CONTRACT", contract)
    if not sys.platform.startswith("linux"):
        def skip_official_mutants(*_args: object, **_kwargs: object) -> None:
            pytest.skip("requires Docker-backed official mutant execution")

        monkeypatch.setattr(
            changed_signal_mutation_validation,
            "fresh_official_mutants",
            skip_official_mutants,
        )
    if not DURABLE_LEDGER_AVAILABLE:
        def skip_durable_ledger(*_args: object, **_kwargs: object) -> None:
            pytest.skip("requires the durable publication ledger platform contract")

        monkeypatch.setattr(release_provenance, "_secure_ledger", skip_durable_ledger)
