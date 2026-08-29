"""Deterministic AST/IR-oriented differences between strategy revisions."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import write_json
from .strategy_diff_features import _changed_line_spans
from .strategy_diff_inventory import (
    _boolean_mapping_changes,
    _inventory,
)
from .strategy_diff_targets import _behavior_targets

STRATEGY_DIFF_VERSION = "1.3.0"
_VECTOR_METHODS = {
    "populate_indicators",
    "populate_entry_trend",
    "populate_exit_trend",
}
_STATEFUL_CALLBACKS = {
    "order_filled",
    "adjust_trade_position",
    "custom_exit",
}


def diff_strategies(
    old_source: str | Path,
    new_source: str | Path,
    *,
    class_name: str | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Describe behavior-relevant source changes without executing either file."""
    old = _inventory(Path(old_source).resolve(), class_name=class_name)
    new = _inventory(Path(new_source).resolve(), class_name=class_name)
    new["changed_source_spans"] = _changed_line_spans(old["path"], new["path"])
    changed_callbacks = _changed_callbacks(old["callbacks"], new["callbacks"])
    changes = {
        "signals": _set_change(old["signals"], new["signals"]),
        "tags": _set_change(old["tags"], new["tags"]),
        "callbacks": {
            "added": sorted(set(new["callbacks"]) - set(old["callbacks"])),
            "removed": sorted(set(old["callbacks"]) - set(new["callbacks"])),
            "changed": changed_callbacks,
            "locations": {
                name: new["callbacks"][name]["location"]
                for name in changed_callbacks
                if name in new["callbacks"]
            },
        },
        "dataframe_columns": _set_change(old["columns"], new["columns"]),
        "custom_state_keys": _set_change(old["state_keys"], new["state_keys"]),
        "grind_levels": _set_change(old["grind_levels"], new["grind_levels"]),
        "opcodes": _set_change(old["opcodes"], new["opcodes"]),
        "boolean_mappings": _boolean_mapping_changes(
            old["boolean_mappings"],
            new["boolean_mappings"],
        ),
    }
    diagnostics = {"old": old["diagnostics"], "new": new["diagnostics"]}
    classification = _classify(changed_callbacks, changes, diagnostics)
    report = {
        "schema_version": STRATEGY_DIFF_VERSION,
        "selected_class": new["class_name"],
        "old": old["source"],
        "new": new["source"],
        "classification": classification,
        "changes": changes,
        "behavior_targets": _behavior_targets(
            changes,
            old_inventory=old,
            new_inventory=new,
        ),
        "diagnostics": diagnostics,
    }
    if output_path is not None:
        write_json(output_path, report)
    return report


def _changed_callbacks(
    old: Mapping[str, Mapping[str, Any]],
    new: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    return sorted(
        name
        for name in set(old) & set(new)
        if old[name].get("source_sha256") != new[name].get("source_sha256")
    )


def _set_change(old: set[Any], new: set[Any]) -> dict[str, list[Any]]:
    return {
        "added": sorted(new - old),
        "removed": sorted(old - new),
    }


def _classify(
    changed_callbacks: list[str],
    changes: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
) -> str:
    if any(
        item.get("severity") == "error"
        for side in ("old", "new")
        for item in diagnostics[side]
        if isinstance(item, Mapping)
    ):
        return "stateful-review"
    changed = set(changed_callbacks)
    if changed and changed <= _VECTOR_METHODS:
        return "vector-only"
    state_change = changes["custom_state_keys"]
    grind_change = changes["grind_levels"]
    if (
        changed & _STATEFUL_CALLBACKS
        or state_change["added"]
        or state_change["removed"]
        or grind_change["added"]
        or grind_change["removed"]
    ):
        return "stateful-review"
    return "ir-compatible"
