"""Presentation-only parsing for NFI signal and adjustment tags.

Raw tags in the sealed trade surface remain authoritative.  These helpers only
add searchable classifications to derived reports and CSV exports.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_STRUCTURED_ACTION_TAG = re.compile(
    r"^(?P<family>[A-Za-z][A-Za-z0-9]*)_"
    r"(?P<level>[0-9]+)_(?P<action>entry|exit|derisk)$"
)
_DERISK_LEVEL_TAG = re.compile(r"^derisk_level_(?P<level>[0-9]+)$")
_LEGACY_GRIND_TAG = re.compile(r"^(?:s?g|gd|gm|gmd)(?P<level>[0-9]+)$")
_LEGACY_DERISK_TAG = re.compile(r"^(?:d|dd|ddl)(?P<level>[0-9]+)$")


@dataclass(frozen=True)
class ParsedOrderTag:
    """A non-authoritative view of one raw filled-order tag."""

    raw: str
    token: str
    family: str
    level: int | None
    action: str
    reference_order_ids: tuple[int, ...]


def signal_tag_tokens(value: Any) -> tuple[str, ...]:
    """Return distinct whitespace-delimited entry signals in source order."""

    if not isinstance(value, str):
        return ()
    return tuple(dict.fromkeys(value.split()))


def parse_order_tag(
    value: Any,
    *,
    is_entry: bool,
    entry_tag: Any = None,
    is_initial_entry: bool = False,
    fallback_action: str,
) -> ParsedOrderTag:
    """Classify a raw order tag while retaining every unrecognized value."""

    raw = value if isinstance(value, str) else ""
    parts = raw.split()
    token = parts[0] if parts else ""
    references = tuple(int(part) for part in parts[1:] if part.isdigit())
    if (
        is_initial_entry
        and token
        and signal_tag_tokens(raw) == signal_tag_tokens(entry_tag)
    ):
        return ParsedOrderTag(raw, token, "signal", None, "entry", ())

    structured = _STRUCTURED_ACTION_TAG.fullmatch(token)
    if structured is not None:
        return ParsedOrderTag(
            raw,
            token,
            structured.group("family").lower(),
            int(structured.group("level")),
            structured.group("action"),
            references,
        )

    derisk = _DERISK_LEVEL_TAG.fullmatch(token)
    if derisk is not None:
        return ParsedOrderTag(
            raw,
            token,
            "derisk",
            int(derisk.group("level")),
            "derisk",
            references,
        )

    legacy_grind = _LEGACY_GRIND_TAG.fullmatch(token)
    if legacy_grind is not None:
        return ParsedOrderTag(
            raw,
            token,
            "grind",
            int(legacy_grind.group("level")),
            "entry" if is_entry else "exit",
            references,
        )

    legacy_derisk = _LEGACY_DERISK_TAG.fullmatch(token)
    if legacy_derisk is not None:
        return ParsedOrderTag(
            raw,
            token,
            "derisk",
            int(legacy_derisk.group("level")),
            "derisk",
            references,
        )

    return ParsedOrderTag(
        raw,
        token,
        "other" if token else "untagged",
        None,
        fallback_action,
        (),
    )


def trade_tag_details(trade: Mapping[str, Any]) -> dict[str, Any]:
    """Build compact per-trade Signal and Grind fields for ``trades.csv``."""

    grind_levels: set[int] = set()
    counts = {"entry": 0, "exit": 0, "derisk": 0}
    grind_orders = 0
    orders = trade.get("orders")
    raw_orders = orders if isinstance(orders, list) else []
    exit_indexes = [
        index
        for index, order in enumerate(raw_orders)
        if isinstance(order, Mapping) and order.get("is_entry") is False
    ]
    final_exit_index = (
        exit_indexes[-1] if exit_indexes and not bool(trade.get("is_open")) else None
    )
    for index, order in enumerate(raw_orders):
        if not isinstance(order, Mapping):
            continue
        is_entry = order.get("is_entry") is True
        fallback_action = (
            "entry"
            if is_entry
            else "partial_exit"
            if index != final_exit_index
            else "exit"
        )
        parsed = parse_order_tag(
            order.get("tag"),
            is_entry=is_entry,
            entry_tag=trade.get("entry_tag"),
            is_initial_entry=index == 0 and is_entry,
            fallback_action=fallback_action,
        )
        if parsed.family != "grind" or parsed.level is None:
            continue
        grind_orders += 1
        grind_levels.add(parsed.level)
        if parsed.action in counts:
            counts[parsed.action] += 1
    return {
        "signal_tags": signal_tag_tokens(trade.get("entry_tag")),
        "grind_levels": tuple(sorted(grind_levels)),
        "grind_order_count": grind_orders,
        "grind_entry_count": counts["entry"],
        "grind_exit_count": counts["exit"],
        "grind_derisk_count": counts["derisk"],
    }


def summarize_grind_tags(
    trades: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate dynamically discovered Grind levels across filled orders."""

    levels: dict[int, dict[str, Any]] = {}
    grind_trade_indexes: set[int] = set()
    order_count = 0
    for trade_index, trade in enumerate(trades):
        raw_orders = trade.get("orders")
        orders = raw_orders if isinstance(raw_orders, list) else []
        for order_index, order in enumerate(orders):
            if not isinstance(order, Mapping):
                continue
            is_entry = order.get("is_entry") is True
            parsed = parse_order_tag(
                order.get("tag"),
                is_entry=is_entry,
                entry_tag=trade.get("entry_tag"),
                is_initial_entry=order_index == 0 and is_entry,
                fallback_action="entry" if is_entry else "exit",
            )
            if parsed.family != "grind" or parsed.level is None:
                continue
            grind_trade_indexes.add(trade_index)
            order_count += 1
            row = levels.setdefault(
                parsed.level,
                {
                    "level": parsed.level,
                    "trade_indexes": set(),
                    "orders": 0,
                    "entries": 0,
                    "exits": 0,
                    "derisks": 0,
                    "tag_forms": set(),
                },
            )
            row["trade_indexes"].add(trade_index)
            row["orders"] += 1
            count_key = {
                "entry": "entries",
                "exit": "exits",
                "derisk": "derisks",
            }.get(parsed.action)
            if count_key is not None:
                row[count_key] += 1
            row["tag_forms"].add(parsed.token)

    exported = [
        {
            "level": level,
            "trades": len(row["trade_indexes"]),
            "orders": row["orders"],
            "entries": row["entries"],
            "exits": row["exits"],
            "derisks": row["derisks"],
            "tag_forms": sorted(row["tag_forms"]),
        }
        for level, row in sorted(levels.items())
    ]
    return {
        "source": "orders[].tag",
        "trades": len(grind_trade_indexes),
        "orders": order_count,
        "levels": exported,
    }
