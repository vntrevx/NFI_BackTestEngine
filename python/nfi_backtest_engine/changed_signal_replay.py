"""Clean-room replay of the tracked current Signal 562 proof lanes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .canonical import read_json
from .changed_signal_replay_publication import (
    ReplayPublication,
    publication_manifest_path,
    publish_replay_artifacts,
)
from .changed_signal_role_binding import (
    resolve_replay_role_bindings,
    role_bindings_sha256,
)
from .docker_environment import docker_subprocess_environment
from .docker_runtime import managed_docker_run
from .fixture_engine import run_fixture_engine
from .normalize import normalize_freqtrade_result, read_freqtrade_export
from .reference.execution import (
    build_reference_docker_command,
    ensure_docker_config,
    ensure_reference_dependencies,
    reference_runtime_volume,
)
from .reference.storage import _reference_market_input

_REPOSITORY: Final = Path(__file__).resolve().parents[2]
_RESULT_PREFIX: Final = re.compile(
    r"^backtest-result-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}"
)
Mode = Literal["spot", "futures"]
Lane = Literal["official", "native"]


@dataclass(frozen=True, slots=True)
class ReplayRun:
    """Private execution context for one clean-room replay lane."""

    mode: Mode
    fixture: Path
    manifest_path: Path
    private_root: Path


def replay_changed_signal(mode: Mode, lane: Lane) -> dict[str, str | bool]:
    """Execute privately and expose only the canonical deterministic artifact set."""
    fixture = _REPOSITORY / f"benchmarks/evidence/m22/current-x7-raw/{mode}/replay"
    manifest_path = fixture / "manifest.json"
    bindings = resolve_replay_role_bindings(mode, lane, manifest_path, _REPOSITORY)
    bindings_digest = role_bindings_sha256(bindings)
    published_root = Path(f"/tmp/task8-r6-published-{mode}-{lane}")
    with tempfile.TemporaryDirectory(prefix=f"task8-r6-private-{mode}-{lane}-") as name:
        run = ReplayRun(mode, fixture, manifest_path, Path(name))
        exact = _run_native(run) if lane == "native" else _run_official(run)
        publish_replay_artifacts(
            run.private_root,
            ReplayPublication(
                mode=mode,
                lane=lane,
                role_bindings_sha256=bindings_digest,
                destination=published_root,
                expected_manifest=publication_manifest_path(_REPOSITORY, mode, lane),
            ),
        )
    return {
        "mode": mode,
        "lane": lane,
        "exact": exact,
        "output": published_root.as_posix(),
        "role_bindings_sha256": bindings_digest,
    }


def _run_native(run: ReplayRun) -> bool:
    result = run_fixture_engine(
        run.manifest_path,
        run.private_root,
        timeout_seconds=600,
        verification_level="full",
    )
    tracked_root = run.fixture.parent
    produced = (
        (
            run.private_root / "research/engine-events.jsonl",
            tracked_root / "native-events.jsonl",
        ),
        (
            run.private_root / "research/simulation-result.json",
            tracked_root / "native-execution.json",
        ),
        (
            run.private_root / "engine-state-projected.trace",
            tracked_root / "native-state.nfitrace",
        ),
    )
    if any(
        hashlib.sha256(actual.read_bytes()).digest()
        != hashlib.sha256(expected.read_bytes()).digest()
        for actual, expected in produced
    ):
        raise SystemExit("Native clean-room replay raw output differs from tracked bytes")
    return bool(result["parity"]["equal"])


def _run_official(run: ReplayRun) -> bool:
    manifest = read_json(run.manifest_path)
    run.private_root.mkdir(parents=True, exist_ok=True)
    (run.private_root / "user_data").mkdir()
    docker = ensure_docker_config()
    dependencies = ensure_reference_dependencies(
        project_root=_REPOSITORY,
        docker_config=docker,
    )
    market = _reference_market_input(manifest)
    with reference_runtime_volume(docker) as volume, managed_docker_run(
        docker_config=docker,
        role=f"task8-r6-replay-{run.mode}",
    ) as lease:
        command = build_reference_docker_command(
            manifest,
            fixture_root=run.fixture,
            output_directory=run.private_root,
            dependency_directory=dependencies,
            trace_mode="full",
            profile=False,
            docker_config=docker,
            market_snapshot=market,
            run_prefix=lease["command_prefix"],
            runtime_volume=volume,
        )
        completed = subprocess.run(
            command,
            cwd=_REPOSITORY,
            env=docker_subprocess_environment(),
            check=False,
            timeout=600,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    archive = next(run.private_root.glob("backtest-result-*.zip"))
    normalized = normalize_freqtrade_result(
        read_freqtrade_export(archive),
        strategy="CurrentChangedPredicateContract",
        surface_version="2",
    )
    expected = read_json(run.fixture / "artifacts/trade-surface.json")
    if normalized != expected:
        raise SystemExit("official clean-room replay differs from tracked trade surface")
    canonical_archive = run.private_root / "official-execution.zip"
    canonicalize_freqtrade_archive(archive, canonical_archive)
    tracked = run.fixture / "artifacts/freqtrade-result.zip"
    if hashlib.sha256(canonical_archive.read_bytes()).digest() != hashlib.sha256(
        tracked.read_bytes()
    ).digest():
        raise SystemExit("official clean-room replay archive differs from tracked bytes")
    return True


def canonicalize_freqtrade_archive(source: Path, destination: Path) -> None:
    """Seal nondeterministic run labels while preserving execution content."""
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as output:
        for member in sorted(archive.namelist()):
            payload = archive.read(member)
            if member.endswith(".json") and not member.endswith("_config.json"):
                document = json.loads(payload)
                for strategy in document.get("strategy", {}).values():
                    strategy["backtest_run_start_ts"] = 0
                    strategy["backtest_run_end_ts"] = 0
                payload = json.dumps(
                    document,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            name = _RESULT_PREFIX.sub("backtest-result", member)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            output.writestr(info, payload, compresslevel=9)


def main() -> int:
    """Execute the exact mode and lane named on the command line."""
    if len(sys.argv) != 3 or sys.argv[1] not in {"spot", "futures"}:
        raise SystemExit("usage: changed_signal_replay MODE (--official|--native)")
    mode: Literal["spot", "futures"] = "spot" if sys.argv[1] == "spot" else "futures"
    if sys.argv[2] not in {"--official", "--native"}:
        raise SystemExit(f"unsupported replay lane: {sys.argv[2]}")
    lane: Literal["official", "native"] = (
        "official" if sys.argv[2] == "--official" else "native"
    )
    print(json.dumps(replay_changed_signal(mode, lane), sort_keys=True, separators=(",", ":")))
    return os.EX_OK


if __name__ == "__main__":
    raise SystemExit(main())
