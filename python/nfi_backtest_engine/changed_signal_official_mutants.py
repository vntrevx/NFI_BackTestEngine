"""Fresh pinned official captures for changed-signal mutant promotion."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final, Literal

from .changed_signal_trust import validate_official_capture
from .docker_environment import docker_subprocess_environment
from .docker_runtime import managed_docker_run
from .errors import SpecValidationError
from .reference.contracts import REFERENCE_IMAGE_REF
from .reference.execution import ensure_docker_config

Mode = Literal["spot", "futures"]
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
_REPOSITORY: Final = Path(__file__).resolve().parents[2]
_CAPTURE_PATH: Final = Path("benchmarks/reference/capture/current_changed_signal.py")
_BATCH_SCRIPT: Final = """\
set -eu
mode="$1"
shift
for strategy in "$@"; do
  PYTHONPATH="/mutants/$strategy:/nfi-python" \
    python /capture/current_changed_signal.py "$mode"
done
"""


@dataclass(frozen=True, slots=True)
class OfficialMutantSource:
    """Identifier and stable source bytes for one official mutant execution."""

    identifier: str
    payload: bytes


def fresh_official_mutants(
    mode: Mode,
    sources: tuple[OfficialMutantSource, ...],
) -> tuple[dict[str, JsonValue], ...]:
    """Execute all mutant snapshots once through the pinned official boundary."""
    capture = validate_official_capture(_REPOSITORY, _REPOSITORY / _CAPTURE_PATH)
    return _fresh_official_mutants(mode, capture.snapshot.payload, sources)


@cache
def _fresh_official_mutants(
    mode: Mode,
    capture_payload: bytes,
    sources: tuple[OfficialMutantSource, ...],
) -> tuple[dict[str, JsonValue], ...]:
    with tempfile.TemporaryDirectory(prefix=f"task8-r7-mutants-{mode}-") as name:
        root = Path(name)
        capture_path = root / "capture/current_changed_signal.py"
        capture_path.parent.mkdir()
        capture_path.write_bytes(capture_payload)
        capture_path.chmod(0o444)
        mutants = root / "mutants"
        mutants.mkdir()
        for source in sources:
            if not source.identifier or "/" in source.identifier:
                raise SpecValidationError("changed signal mutant identifier is not portable")
            directory = mutants / source.identifier
            directory.mkdir()
            strategy = directory / "CurrentChangedPredicateContract.py"
            strategy.write_bytes(source.payload)
            strategy.chmod(0o444)
            directory.chmod(0o555)
        mutants.chmod(0o555)
        capture_path.parent.chmod(0o555)
        docker = ensure_docker_config()
        with managed_docker_run(
            docker_config=docker,
            role=f"task8-r7-mutants-{mode}",
        ) as lease:
            command = [
                *lease["command_prefix"],
                "--network",
                "none",
                "--volume",
                f"{capture_path}:/capture/current_changed_signal.py:ro",
                "--volume",
                f"{mutants}:/mutants:ro",
                "--volume",
                f"{_REPOSITORY / 'python/nfi_backtest_engine'}:/nfi-python/nfi_backtest_engine:ro",
                "--entrypoint",
                "/bin/sh",
                REFERENCE_IMAGE_REF,
                "-c",
                _BATCH_SCRIPT,
                "task8-r7-mutants",
                mode,
                *(source.identifier for source in sources),
            ]
            completed = subprocess.run(
                command,
                cwd=_REPOSITORY,
                env=docker_subprocess_environment(),
                capture_output=True,
                check=False,
                text=True,
                timeout=600,
            )
        if completed.returncode != os.EX_OK:
            raise SpecValidationError("changed signal official mutant recapture failed")
    lines = completed.stdout.splitlines()
    if len(lines) != len(sources):
        raise SpecValidationError("changed signal official mutant recapture inventory differs")
    captures = tuple(json.loads(line) for line in lines)
    if not all(isinstance(capture, dict) for capture in captures):
        raise SpecValidationError("changed signal official mutant recapture is malformed")
    return captures
