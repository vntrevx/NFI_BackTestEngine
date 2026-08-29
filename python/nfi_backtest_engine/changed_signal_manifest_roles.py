"""Replay-manifest role resolution for changed-signal producers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, assert_never

from .changed_signal_filesystem_trust import FileIdentity, read_stable_file
from .errors import SpecValidationError

Mode = Literal["spot", "futures"]


@dataclass(frozen=True, slots=True)
class ManifestRoleSpec:
    """One expected role and repository-relative suffix in a replay manifest."""

    proof_role: str
    manifest_role: str
    suffix: str


def resolve_manifest_bindings(
    mode: Mode,
    manifest: dict[str, Any],
    replay_root: Path,
) -> list[tuple[str, Path, str, int, FileIdentity]]:
    """Resolve exact manifest inputs and validate their command destinations."""
    records = [
        _manifest_binding(spec, manifest, replay_root)
        for spec in _manifest_role_specs(mode)
    ]
    _validate_manifest_command(mode, manifest, records)
    return records


def _manifest_role_specs(mode: Mode) -> tuple[ManifestRoleSpec, ...]:
    match mode:
        case "spot":
            return (
                ManifestRoleSpec("strategy_input", "strategy", "inputs/strategy.py"),
                ManifestRoleSpec("config_input", "config", "inputs/config.json"),
                ManifestRoleSpec("candle_input", "candles", "BTC_USDT-5m.feather"),
                ManifestRoleSpec("market_input", "market_metadata", "markets.json"),
            )
        case "futures":
            return (
                ManifestRoleSpec("strategy_input", "strategy", "inputs/strategy.py"),
                ManifestRoleSpec("config_input", "config", "inputs/config.json"),
                ManifestRoleSpec(
                    "candle_input", "candles", "BTC_USDT_USDT-5m-futures.feather"
                ),
                ManifestRoleSpec(
                    "funding_input",
                    "funding_candles",
                    "BTC_USDT_USDT-1h-funding_rate.feather",
                ),
                ManifestRoleSpec(
                    "mark_input", "mark_candles", "BTC_USDT_USDT-1h-mark.feather"
                ),
                ManifestRoleSpec(
                    "market_input",
                    "reference_market_metadata",
                    "reference-markets.json",
                ),
                ManifestRoleSpec(
                    "leverage_input",
                    "market_metadata",
                    "full-x7-futures-20210101-markets.json",
                ),
            )
        case unreachable:
            assert_never(unreachable)


def _manifest_binding(
    spec: ManifestRoleSpec,
    manifest: dict[str, Any],
    replay_root: Path,
) -> tuple[str, Path, str, int, FileIdentity]:
    inputs = manifest["inputs"]
    if not isinstance(inputs, list):
        raise SpecValidationError("changed signal replay input inventory differs")
    candidates = [
        item
        for item in inputs
        if isinstance(item, dict)
        and item.get("role") == spec.manifest_role
        and isinstance(item.get("path"), str)
        and item["path"].endswith(spec.suffix)
    ]
    if len(candidates) != 1:
        raise SpecValidationError("changed signal replay role inventory differs")
    record = candidates[0]
    path = (replay_root / record["path"]).absolute()
    if not path.is_relative_to(replay_root.absolute()):
        raise SpecValidationError("changed signal replay role path differs")
    snapshot = read_stable_file(path, path)
    digest = hashlib.sha256(snapshot.payload).hexdigest()
    if record.get("sha256") != digest or record.get("bytes") != len(snapshot.payload):
        raise SpecValidationError("changed signal replay role content differs")
    return (
        spec.proof_role,
        path,
        digest,
        len(snapshot.payload),
        snapshot.metadata.identity,
    )


def _validate_manifest_command(
    mode: Mode,
    manifest: dict[str, Any],
    records: list[tuple[str, Path, str, int, FileIdentity]],
) -> None:
    freqtrade = manifest["freqtrade"]
    if not isinstance(freqtrade, dict) or not isinstance(freqtrade.get("command"), list):
        raise SpecValidationError("changed signal replay command inventory differs")
    command = freqtrade["command"]
    by_role = {role: path for role, path, _digest, _size, _identity in records}
    config = by_role["config_input"]
    strategy = by_role["strategy_input"]
    candle = by_role["candle_input"]
    replay_root = config.parents[1]
    match mode:
        case "spot":
            data_directory = candle.parent
        case "futures":
            data_directory = candle.parents[1]
        case unreachable:
            assert_never(unreachable)
    expected = {
        "--config": config.relative_to(replay_root).as_posix(),
        "--strategy-path": strategy.parent.relative_to(replay_root).as_posix(),
        "--datadir": data_directory.relative_to(replay_root).as_posix(),
    }
    for option, relative in expected.items():
        try:
            index = command.index(option)
        except ValueError as exc:
            raise SpecValidationError("changed signal replay command role differs") from exc
        if index + 1 >= len(command) or command[index + 1] != relative:
            raise SpecValidationError("changed signal replay command role differs")
