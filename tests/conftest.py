from __future__ import annotations

import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
from nfi_backtest_engine import changed_signal_git_trust, changed_signal_role_binding

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_UPSTREAM = ROOT / "benchmarks/evidence/m22/historical-upstream-eebaf.git.tar.gz"
NON_LINUX_GIT_VERSION = (
    subprocess.run(
        ("git", "--version"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if sys.platform != "linux"
    else None
)


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
    if NON_LINUX_GIT_VERSION is not None:
        # The production path stays pinned and fails closed. Non-Linux hosted test
        # images exercise the remaining authority contract with their system Git.
        monkeypatch.setattr(
            changed_signal_git_trust,
            "PINNED_GIT_VERSION",
            NON_LINUX_GIT_VERSION,
        )
    monkeypatch.setattr(changed_signal_role_binding, "UPSTREAM_SOURCE_CONTRACT", contract)
