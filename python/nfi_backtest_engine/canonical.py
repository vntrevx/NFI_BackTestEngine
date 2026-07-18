"""Canonical JSON, decimal, and timestamp helpers."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn

from .errors import NormalizationError


def read_json(path: str | Path, *, decimals: bool = False) -> Any:
    """Read UTF-8 JSON, optionally preserving JSON float tokens as Decimal."""
    parse_float = Decimal if decimals else float
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle, parse_float=parse_float)


def write_json(path: str | Path, value: Any) -> None:
    """Write deterministic human-readable UTF-8 JSON."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)
    destination.write_text(f"{serialized}\n", encoding="utf-8")


def canonical_decimal(value: Any, *, path: str, nullable: bool = False) -> str | None:
    """Convert a numeric value to the v1 finite canonical decimal representation."""
    if value is None and nullable:
        return None
    if value is None or isinstance(value, bool):
        _normalization_error(path, "expected a finite decimal")

    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int):
            number = Decimal(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                _normalization_error(path, "non-finite float is not supported")
            number = Decimal(repr(value))
        elif isinstance(value, str):
            number = Decimal(value)
        else:
            _normalization_error(path, f"unsupported decimal type {type(value).__name__}")
    except InvalidOperation as exc:
        raise NormalizationError(f"{path}: invalid decimal {value!r}") from exc

    if not number.is_finite():
        _normalization_error(path, "non-finite decimal is not supported")
    if number == 0:
        return "0"

    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def canonical_timestamp_ms(
    record: dict[str, Any],
    *,
    timestamp_keys: tuple[str, ...],
    date_keys: tuple[str, ...],
    path: str,
    nullable: bool = False,
) -> int | None:
    """Read and cross-check integer timestamp/date aliases from a Freqtrade record."""
    timestamp = _first_present(record, timestamp_keys)
    date_value = _first_present(record, date_keys)

    timestamp_ms = _integer_timestamp(timestamp[1], f"{path}.{timestamp[0]}") if timestamp else None
    date_ms = _date_timestamp(date_value[1], f"{path}.{date_value[0]}") if date_value else None

    if timestamp_ms is not None and date_ms is not None and timestamp_ms != date_ms:
        raise NormalizationError(f"{path}: timestamp/date disagree ({timestamp_ms} != {date_ms})")
    result = timestamp_ms if timestamp_ms is not None else date_ms
    if result is None and not nullable:
        aliases = ", ".join((*timestamp_keys, *date_keys))
        raise NormalizationError(f"{path}: missing timestamp ({aliases})")
    return result


def _integer_timestamp(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        _normalization_error(path, "timestamp must be an integer")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise NormalizationError(f"{path}: invalid timestamp {value!r}") from exc
    if not number.is_finite() or number != number.to_integral_value():
        _normalization_error(path, "timestamp must be a finite integer")
    timestamp = int(number)
    if timestamp < 0:
        _normalization_error(path, "timestamp must be non-negative")
    return timestamp


def _date_timestamp(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _normalization_error(path, "date timestamp must be a string")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise NormalizationError(f"{path}: invalid ISO timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _first_present(record: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, Any] | None:
    for key in keys:
        if key in record and record[key] is not None:
            return key, record[key]
    return None


def _normalization_error(path: str, message: str) -> NoReturn:
    raise NormalizationError(f"{path}: {message}")
