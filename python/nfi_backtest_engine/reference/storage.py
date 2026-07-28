"""Immutable reference input lookup and output resource records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import BenchmarkError


def _initialize_output_directory(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"reference output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "user_data").mkdir()


def _find_result_zip(output: Path) -> Path:
    candidates = sorted(output.glob("backtest-result-*.zip"))
    if len(candidates) != 1:
        names = ", ".join(path.name for path in candidates) or "none"
        raise BenchmarkError(f"expected exactly one official result ZIP in {output}; found {names}")
    return candidates[0]


def _one_input(inputs: list[dict[str, Any]], role: str) -> dict[str, Any]:
    matches = [item for item in inputs if item["role"] == role]
    if len(matches) != 1:
        raise BenchmarkError(f"fixture requires exactly one {role!r} input")
    return matches[0]


def _reference_market_input(manifest: dict[str, Any]) -> dict[str, Any]:
    preferred = [item for item in manifest["inputs"] if item["role"] == "reference_market_metadata"]
    if preferred:
        if len(preferred) != 1:
            raise BenchmarkError("fixture requires exactly one reference market metadata input")
        return preferred[0]
    return _one_input(manifest["inputs"], "market_metadata")


def _file_record(path: Path) -> dict[str, Any]:
    from ..fixture import sha256_file

    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _read_nonnegative_integer(path: Path) -> int | None:
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _read_cpu_stat(path: Path) -> dict[str, int] | None:
    return _read_integer_record(path)


def _read_integer_record(path: Path) -> dict[str, int] | None:
    if not path.is_file():
        return None
    result: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result or None


def _read_io_stat(path: Path) -> list[dict[str, Any]] | None:
    """Parse cgroup-v2 IO counters without depending on host device names."""
    if not path.is_file():
        return None
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        counters: dict[str, int] = {}
        for token in parts[1:]:
            name, separator, raw_value = token.partition("=")
            if separator and raw_value.isdigit():
                counters[name] = int(raw_value)
        records.append({"device": parts[0], "counters": counters})
    return records or None


def _container_memory_assessment(
    *,
    exit_code: int,
    peak_bytes: int | None,
    events: dict[str, int] | None,
    resources: dict[str, Any] | None,
) -> dict[str, Any]:
    policy = resources.get("policy") if isinstance(resources, dict) else None
    raw_limit = policy.get("container_memory_limit_bytes") if isinstance(policy, dict) else None
    limit = raw_limit if isinstance(raw_limit, int) and raw_limit > 0 else None
    peak_ratio = peak_bytes / limit if peak_bytes is not None and limit is not None else None
    oom_kills = events.get("oom_kill", 0) if events is not None else 0
    if oom_kills > 0:
        verdict = "oom_killed"
    elif exit_code in {137, -9}:
        verdict = "possible_oom"
    elif peak_ratio is not None and peak_ratio >= 0.9:
        verdict = "near_limit"
    elif peak_bytes is not None:
        verdict = "within_limit"
    else:
        verdict = "unmeasured"
    return {
        "verdict": verdict,
        "limit_bytes": limit,
        "peak_bytes": peak_bytes,
        "peak_ratio": peak_ratio,
        "oom_kill_count": oom_kills,
    }
