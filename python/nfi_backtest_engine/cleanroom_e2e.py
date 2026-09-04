"""Repository-independent installed-artifact user journey."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import __version__
from .canonical import write_json
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file, validate_fixture

CLEANROOM_REPORT_VERSION = "cleanroom-e2e-report-v1"


def run_cleanroom_e2e(
    fixture_manifest: str | Path,
    output_directory: str | Path,
    *,
    executable: str | Path | None = None,
    require_no_checkout: bool = True,
    timeout_seconds: int = 900,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Run the documented first-result journey from one installed CLI."""
    if timeout_seconds <= 0:
        raise BenchmarkError("clean-room command timeout must be positive")
    if require_no_checkout and (Path.cwd() / ".git").exists():
        raise BenchmarkError("clean-room audit refuses a repository checkout")
    manifest_path = Path(fixture_manifest).resolve()
    manifest = validate_fixture(manifest_path)
    inputs = _fixture_inputs(manifest, manifest_path.parent)
    output = Path(output_directory).resolve()
    if output.exists():
        raise BenchmarkError(f"clean-room output already exists: {output}")
    output.mkdir(parents=True)
    workspace = output / "workspace"
    workspace.mkdir()
    staged_source = workspace / f"{inputs['strategy_class']}.py"
    shutil.copyfile(inputs["strategy"], staged_source)
    if sha256_file(staged_source) != sha256_file(inputs["strategy"]):
        raise BenchmarkError("clean-room strategy staging changed sealed bytes")
    cli = str(executable or _installed_cli_path())
    if runner is subprocess.run and not Path(cli).is_file():
        raise BenchmarkError(f"installed nfi-bte executable is unavailable: {cli}")

    nfi_root = workspace / ".nfi"
    project = nfi_root / "project.json"
    run_directory = nfi_root / "run"
    commands = [
        ("doctor", [cli, "doctor", "--json"]),
        ("strategy-list", [cli, "strategy", "list", "--workspace", str(workspace), "--json"]),
        (
            "init",
            [
                cli,
                "init",
                str(staged_source),
                "--class",
                inputs["strategy_class"],
                "--config",
                str(inputs["config"]),
                "--datadir",
                str(inputs["data_directory"]),
                "--timerange",
                inputs["timerange"],
                "--output-dir",
                str(run_directory),
                "--project",
                str(project),
                "--pair",
                inputs["pair"],
                "--yes",
            ],
        ),
        (
            "run",
            [
                cli,
                "run",
                "--project",
                str(project),
                "--no-download",
                "--no-market-download",
                "--markets",
                str(inputs["reference_markets"]),
                "--no-verify",
                "--fallback",
                "disabled",
                "--yes",
            ],
        ),
        ("report", [cli, "report", str(run_directory), "--no-full-report"]),
        ("status", [cli, "status", "--json"]),
        (
            "clean-dry-run",
            [cli, "clean", "--dry-run", "--root", str(nfi_root), "--no-runtime-probes"],
        ),
        ("update-check", [cli, "update", "--check"]),
    ]
    records: list[dict[str, Any]] = []
    environment = {**os.environ, "PYTHONUNBUFFERED": "1"}
    for index, (name, command) in enumerate(commands, start=1):
        completed = runner(
            command,
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        stdout = output / f"{index:02d}-{name}.stdout.log"
        stderr = output / f"{index:02d}-{name}.stderr.log"
        stdout.write_text(completed.stdout or "", encoding="utf-8")
        stderr.write_text(completed.stderr or "", encoding="utf-8")
        records.append(
            {
                "name": name,
                "arguments": command[1:],
                "exit_code": completed.returncode,
                "stdout_sha256": sha256_file(stdout),
                "stderr_sha256": sha256_file(stderr),
            }
        )
        if completed.returncode != 0:
            report = _report(manifest, manifest_path, records, complete=False, artifacts=[])
            write_json(output / "cleanroom-report.json", report)
            raise BenchmarkError(
                f"CLEANROOM_{name.upper().replace('-', '_')}_FAILED: see {stderr}"
            )

    required = [
        project,
        run_directory / "run.json",
        run_directory / "report.md",
        nfi_root / "clean-audit.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise BenchmarkError(f"clean-room journey omitted required artifacts: {missing}")
    artifacts = [_artifact(path, workspace) for path in required]
    report = _report(manifest, manifest_path, records, complete=True, artifacts=artifacts)
    write_json(output / "cleanroom-report.json", report)
    return report


def _installed_cli_path() -> Path:
    entrypoint = Path(sys.argv[0])
    if entrypoint.name == "nfi-bte" and entrypoint.is_file():
        return entrypoint.absolute()
    discovered = shutil.which("nfi-bte")
    if discovered is None:
        raise BenchmarkError("installed nfi-bte executable is unavailable")
    return Path(discovered).absolute()


def _fixture_inputs(manifest: Mapping[str, Any], root: Path) -> dict[str, Any]:
    command = manifest.get("freqtrade", {}).get("command")
    descriptors = manifest.get("inputs")
    if not isinstance(command, list) or not isinstance(descriptors, list):
        raise SpecValidationError("clean-room fixture contract is incomplete")

    def option(name: str) -> str:
        try:
            index = command.index(name)
            value = command[index + 1]
        except (ValueError, IndexError) as exc:
            raise SpecValidationError(f"clean-room fixture omits {name}") from exc
        if not isinstance(value, str) or not value:
            raise SpecValidationError(f"clean-room fixture has invalid {name}")
        return value

    by_role = {item.get("role"): item.get("path") for item in descriptors if isinstance(item, dict)}
    strategy = _contained(root, by_role.get("strategy"), "strategy")
    config = _contained(root, by_role.get("config"), "config")
    reference_markets = _contained(
        root, by_role.get("reference_market_metadata"), "reference market metadata"
    )
    data_directory = _contained(root, option("--datadir"), "data directory", directory=True)
    pair = option("--pairs")
    if pair.startswith("-"):
        raise SpecValidationError("clean-room fixture pair is invalid")
    return {
        "strategy": strategy,
        "strategy_class": option("--strategy"),
        "config": config,
        "data_directory": data_directory,
        "reference_markets": reference_markets,
        "timerange": option("--timerange"),
        "pair": pair,
    }


def _contained(root: Path, value: Any, label: str, *, directory: bool = False) -> Path:
    if not isinstance(value, str) or not value:
        raise SpecValidationError(f"clean-room fixture omits {label}")
    path = (root / value).resolve()
    if not path.is_relative_to(root) or (not path.is_dir() if directory else not path.is_file()):
        raise SpecValidationError(f"clean-room fixture {label} is unavailable")
    return path


def _artifact(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _report(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    commands: list[dict[str, Any]],
    *,
    complete: bool,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": CLEANROOM_REPORT_VERSION,
        "package_version": __version__,
        "fixture_id": manifest["fixture_id"],
        "fixture_manifest_sha256": sha256_file(manifest_path),
        "repository_checkout_used": False,
        "commands": commands,
        "artifacts": artifacts,
        "complete": complete,
    }


__all__ = ["CLEANROOM_REPORT_VERSION", "run_cleanroom_e2e"]
