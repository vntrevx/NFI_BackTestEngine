"""Carry a completed Native run's resource caps into its official comparison."""

from pathlib import Path

from .canonical import read_json
from .errors import BenchmarkError


def reference_resource_limits(run_directory: Path) -> dict[str, int | None]:
    """Older runs without a profile retain the managed daemon resource policy."""
    path = run_directory / "execution-profile.json"
    if not path.exists():
        return {}
    profile = read_json(path)
    limits = profile.get("limits") if isinstance(profile, dict) else None
    if (
        not isinstance(limits, dict)
        or not {"memory_cap_bytes", "cpu_process_limit"} <= limits.keys()
    ):
        raise BenchmarkError("reference execution profile must contain memory and CPU limits")
    memory = limits["memory_cap_bytes"]
    cpu = limits["cpu_process_limit"]
    if memory is not None and (
        isinstance(memory, bool) or not isinstance(memory, int) or memory < 1024**3
    ):
        raise BenchmarkError("reference memory cap must be null or an integer of at least 1 GiB")
    if isinstance(cpu, bool) or not isinstance(cpu, int) or cpu <= 0:
        raise BenchmarkError("reference CPU limit must be a positive integer")
    return {"memory_cap_bytes": memory, "cpu_limit": cpu}
