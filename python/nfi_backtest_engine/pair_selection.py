"""Resolve NFI's live volume pairlist once, then freeze the selected order."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .canonical import loads_json_bytes, write_json
from .config_loader import sanitize_config, strip_service_only_settings
from .docker_runtime import (
    RUN_AS_BIND_OWNER_SCRIPT,
    docker_root_with_bind_owner_arguments,
    managed_docker_run,
)
from .errors import BenchmarkError, SpecValidationError
from .reference_runtime import (
    REFERENCE_IMAGE_REF,
    REFERENCE_PLATFORM,
    ensure_docker_config,
    ensure_reference_image,
)

PAIR_COUNT_PRESETS = (10, 20, 40, 80, 100)
_PAIRLIST_FILENAME = "pairlist-volume-binance-usdt.json"
_BLACKLIST_FILENAME = "blacklist-binance.json"
_TRANSIENT_ERRORS = (
    "connection refused",
    "connection reset",
    "ddosprotection",
    "exchangenotavailable",
    "name resolution",
    "networkerror",
    "ratelimitexceeded",
    "requesttimeout",
    "temporaryerror",
    "timeout while contacting dns servers",
)
_RETRY_DELAYS_SECONDS = (2, 5)


def nfi_volume_policy_available(workspace: str | Path) -> bool:
    """Return whether the checkout contains both NFI-owned ranking inputs."""
    root = Path(workspace).resolve() / "configs"
    return all(_regular_file(root / name) for name in (_PAIRLIST_FILENAME, _BLACKLIST_FILENAME))


def resolve_nfi_volume_pairs(
    config: dict[str, Any],
    workspace: str | Path,
    *,
    diagnostic_path: str | Path,
) -> list[str]:
    """Run NFI's current dynamic policy in pinned Freqtrade and return its frozen order."""
    root = Path(workspace).resolve()
    policy_root = root / "configs"
    pairlist_source = policy_root / _PAIRLIST_FILENAME
    blacklist_source = policy_root / _BLACKLIST_FILENAME
    if not nfi_volume_policy_available(root):
        raise SpecValidationError(
            "this NFI checkout has no complete Binance volume-pair policy; "
            "update NFI or choose custom pairs"
        )
    prepared = _selection_config(config)
    failure_log = Path(diagnostic_path).resolve()
    docker_config = ensure_docker_config()
    ensure_reference_image(docker_config=docker_config)

    completed: subprocess.CompletedProcess[str] | None = None
    command: list[str] = []
    attempt_count = 0
    with tempfile.TemporaryDirectory(prefix="nfi-pair-selection-") as temporary:
        temporary_root = Path(temporary)
        inputs = temporary_root / "input"
        output = temporary_root / "output"
        inputs.mkdir()
        output.mkdir()
        (output / "user_data").mkdir()
        write_json(inputs / "config.json", prepared)
        shutil.copyfile(pairlist_source, inputs / _PAIRLIST_FILENAME)
        shutil.copyfile(blacklist_source, inputs / _BLACKLIST_FILENAME)

        for attempt_count in range(1, 4):
            with managed_docker_run(
                docker_config=docker_config,
                role="pairlist-resolution",
            ) as lease:
                command = [
                    *lease["command_prefix"],
                    "--platform",
                    REFERENCE_PLATFORM,
                    *docker_root_with_bind_owner_arguments(output),
                    "--volume",
                    f"{inputs}:/input:ro",
                    "--volume",
                    f"{output}:/work",
                    "--entrypoint",
                    "/bin/sh",
                    REFERENCE_IMAGE_REF,
                    "-c",
                    RUN_AS_BIND_OWNER_SCRIPT,
                    "nfi-pairlist-resolution",
                    "freqtrade",
                    "test-pairlist",
                    "--config",
                    "/input/config.json",
                    "--userdir",
                    "/work/user_data",
                    "--print-json",
                ]
                completed = subprocess.run(
                    command,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                )
            if completed.returncode == 0:
                break
            detail = _process_detail(completed)
            if not _transient(detail) or attempt_count == 3:
                break
            time.sleep(_RETRY_DELAYS_SECONDS[attempt_count - 1])

    assert completed is not None
    if completed.returncode != 0:
        _write_failure_log(failure_log, completed, attempt_count)
        if _transient(_process_detail(completed)):
            raise BenchmarkError(
                "Binance market ranking was temporarily unavailable after "
                f"{attempt_count} attempts. Run the same command again. "
                f"Technical details: {failure_log}"
            )
        raise BenchmarkError(
            "NFI market ranking failed. "
            f"Technical details: {failure_log}"
        )
    pairs = _parse_pairlist_stdout(completed.stdout)
    failure_log.unlink(missing_ok=True)
    return pairs


def _selection_config(config: dict[str, Any]) -> dict[str, Any]:
    prepared = sanitize_config(strip_service_only_settings(config))
    if not isinstance(prepared, dict):
        raise SpecValidationError("pair-selection config must be an object")
    exchange = prepared.get("exchange")
    if not isinstance(exchange, dict) or exchange.get("name") != "binance":
        raise SpecValidationError("automatic volume ranking currently requires Binance")
    exchange.pop("pair_whitelist", None)
    exchange.pop("pair_blacklist", None)
    prepared.pop("pairlists", None)
    prepared["add_config_files"] = [_PAIRLIST_FILENAME, _BLACKLIST_FILENAME]
    prepared.setdefault("max_open_trades", 1)
    prepared.setdefault("stake_currency", "USDT")
    prepared.setdefault("stake_amount", "unlimited")
    prepared.setdefault("tradable_balance_ratio", 0.99)
    return prepared


def _parse_pairlist_stdout(stdout: str) -> list[str]:
    for raw in reversed(stdout.splitlines()):
        line = raw.strip()
        if not line.startswith("["):
            continue
        try:
            value = loads_json_bytes(line.encode())
        except SpecValidationError:
            continue
        if (
            isinstance(value, list)
            and value
            and all(isinstance(pair, str) and "/" in pair for pair in value)
        ):
            return list(dict.fromkeys(value))
    raise BenchmarkError("pinned Freqtrade returned no valid JSON pair list")


def _write_failure_log(
    path: Path,
    completed: subprocess.CompletedProcess[str],
    attempts: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"attempts: {attempts}\n"
        f"exit_code: {completed.returncode}\n\n"
        f"{_process_detail(completed)}\n",
        encoding="utf-8",
    )


def _process_detail(completed: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        value.strip()
        for value in (completed.stderr, completed.stdout)
        if value.strip()
    )


def _transient(detail: str) -> bool:
    normalized = detail.lower()
    return any(token in normalized for token in _TRANSIENT_ERRORS)


def _regular_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()
