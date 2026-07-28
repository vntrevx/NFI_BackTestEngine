"""Shared presentation-only value formatting."""

from __future__ import annotations

import html
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


def _summary_currency(summary: Mapping[str, Any]) -> str | None:
    breakdowns = _mapping(summary, "breakdowns")
    rows = breakdowns.get("by_pair")
    if not isinstance(rows, list):
        return None
    currencies = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        pair = str(row.get("pair", ""))
        if "/" not in pair:
            continue
        quote = pair.split("/", maxsplit=1)[1].split(":", maxsplit=1)[0]
        if quote:
            currencies.add(quote)
    return next(iter(currencies)) if len(currencies) == 1 else None


def _terminal_row(label: str, value: Any) -> str:
    return f"{label:<21} {value}"


def _status_label(status: str) -> str:
    return {
        "complete": "COMPLETE ✓",
        "prepared": "PREPARED",
        "blocked_unsupported_semantics": "BLOCKED — SAFE STOP",
    }.get(status, status.upper())


def _verification_label(verification: Mapping[str, Any]) -> str:
    status = verification.get("status")
    if status == "exact_match":
        return "EXACT MATCH ✓"
    if status == "mismatch":
        return f"MISMATCH — {_difference_text(verification.get('difference'))}"
    return "NOT RUN — confirmation required"


def _difference_text(value: Any) -> str:
    if isinstance(value, Mapping):
        path = value.get("path")
        reason = value.get("reason")
        if path or reason:
            return f"{path or 'unknown path'}: {reason or 'values differ'}"
    return "official and native surfaces differ"


def _memory_label(execution: Mapping[str, Any]) -> str:
    peak = _float(execution.get("peak_rss_bytes"))
    if peak is not None:
        return f"{_bytes(peak)} peak RSS"
    budget = _float(execution.get("memory_budget_bytes"))
    return f"{_bytes(budget)} budget" if budget is not None else "not measured"


def _mode_label(context: Mapping[str, Any]) -> str:
    mode = str(context.get("trading_mode") or "unknown")
    margin = context.get("margin_mode")
    timeframe = context.get("timeframe")
    details = [mode]
    if margin:
        details.append(str(margin))
    if timeframe:
        details.append(str(timeframe))
    return " · ".join(details)


def _format_timerange(value: Any) -> str:
    text = str(value or "unknown")
    parts = text.split("-", maxsplit=1)
    if len(parts) != 2:
        return text
    return f"{_date_token(parts[0])} → {_date_token(parts[1])}"


def _date_token(value: str) -> str:
    if len(value) >= 8 and value[:8].isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value or "open"


def _duration(value: Any) -> str:
    seconds = _float(value)
    if seconds is None:
        return "not measured"
    rounded = max(0, round(seconds))
    hours, remainder = divmod(rounded, 3_600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _bytes(value: float) -> str:
    size = max(0.0, value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024 or candidate == units[-1]:
            break
        size /= 1024
    return f"{size:.1f} {unit}"


def _money(value: Any, currency: str | None) -> str:
    number = _float(value)
    if number is None:
        return "—"
    suffix = f" {currency}" if currency else ""
    return f"{number:,.2f}{suffix}"


def _signed_money(value: Any, currency: str | None) -> str:
    number = _float(value)
    if number is None:
        return "—"
    suffix = f" {currency}" if currency else ""
    return f"{number:+,.2f}{suffix}"


def _percent(value: Any) -> str:
    number = _float(value)
    return f"{number * 100:.2f}%" if number is not None else "—"


def _signed_percent(value: Any) -> str:
    number = _float(value)
    return f"{number * 100:+.2f}%" if number is not None else "—"


def _decimal_text(value: Any) -> str:
    number = _float(value)
    return f"{number:,.2f}" if number is not None else "—"


def _minutes(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "—"
    if number >= 1_440:
        return f"{number / 1_440:.1f}d"
    if number >= 60:
        return f"{number / 60:.1f}h"
    return f"{number:.0f}m"


def _value_class(value: Any) -> str:
    number = _float(value)
    if number is None or number == 0:
        return ""
    return "positive" if number > 0 else "negative"


def _integer_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError, OverflowError):
        return "—"


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _iso_timestamp(value: Any) -> str | None:
    timestamp = _float(value)
    if timestamp is None:
        return None
    return (
        datetime.fromtimestamp(timestamp / 1_000, tz=UTC)
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )


def _date_label(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC).strftime("%Y-%m-%d")


def _compact_number(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:.2f}"


def _short_timestamp(value: Any) -> str:
    text = str(value or "")
    return text.replace("T", " ")[:19] if text else "unknown"


def _compact_path(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "unknown"
    # Proofs can move between Windows and POSIX hosts.  Normalize only for display;
    # the complete source path remains untouched in summary.json.
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    if len(parts) <= 2:
        return text
    return f"…/{'/'.join(parts[-2:])}"


def _truncate(value: str, width: int) -> str:
    return value if len(value) <= width else f"{value[: max(0, width - 1)]}…"


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    candidate = value.get(key)
    return candidate if isinstance(candidate, Mapping) else {}
