"""Physical disk accounting for cleanup candidates.

Logical usage belongs to path names, while allocated usage belongs to inodes.
Keeping that distinction here prevents hard-linked vector artifacts from being
reported once per link and prevents links outside a deletion set from being
claimed as reclaimable disk space.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InodeUsage:
    """One observed filesystem name and the inode storage behind it."""

    unit_path: str
    device: int
    inode: int
    link_count: int
    allocated_bytes: int


def apply_physical_accounting(
    entries: Sequence[MutableMapping[str, Any]],
    usages: Sequence[InodeUsage],
) -> None:
    """Assign each inode once and count it reclaimable only when all links vanish."""
    by_path = {str(entry["path"]): entry for entry in entries}
    for entry in entries:
        entry["allocated_bytes"] = 0
        entry["reclaimable_allocated_bytes"] = 0

    grouped: dict[tuple[int, int], list[InodeUsage]] = defaultdict(list)
    for usage in usages:
        grouped[(usage.device, usage.inode)].append(usage)

    for occurrences in grouped.values():
        unit_paths = sorted({usage.unit_path for usage in occurrences})
        unit_entries = [by_path[path] for path in unit_paths]
        allocated_bytes = max(usage.allocated_bytes for usage in occurrences)

        protected = sorted(
            (entry for entry in unit_entries if not bool(entry["deletable"])),
            key=_entry_path,
        )
        allocation_owner = protected[0] if protected else min(unit_entries, key=_entry_path)
        allocation_owner["allocated_bytes"] += allocated_bytes

        observed_names = len(occurrences)
        filesystem_links = max(usage.link_count for usage in occurrences)
        all_observed_links_are_deletable = all(
            bool(entry["deletable"]) for entry in unit_entries
        )
        if all_observed_links_are_deletable and observed_names == filesystem_links:
            reclaim_owner = min(unit_entries, key=_entry_path)
            reclaim_owner["reclaimable_allocated_bytes"] += allocated_bytes


def _entry_path(entry: Mapping[str, Any]) -> str:
    return str(entry["path"])
