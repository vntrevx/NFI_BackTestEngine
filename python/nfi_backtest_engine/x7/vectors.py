"""X7 vector-frame scope checks and candle serialization."""

from __future__ import annotations

import math
import numbers
from typing import Any

import numpy as np
import pandas as pd

from ..errors import StrategyAnalysisError
from ..vector_manifest import EMPTY_TAG_TRANSPORT_SENTINEL


def _validate_nfi_frame_scope(
    frame: pd.DataFrame,
    pair: str,
    manager: dict[str, Any],
    *,
    can_short: bool,
) -> None:
    managed = manager.get("managed_long_routes")
    if not isinstance(managed, list):
        raise StrategyAnalysisError("NFI managed-long routes are invalid")
    routes = [route for route in managed if isinstance(route, dict)]
    routes.extend(
        route
        for name in ("long_grind", "long_btc")
        if isinstance((route := manager.get(name)), dict)
    )
    supported_long: set[str] = set()
    for route in routes:
        entry_tags = route.get("entry_tags")
        if not isinstance(entry_tags, list) or not all(
            isinstance(tag, str) and tag for tag in entry_tags
        ):
            raise StrategyAnalysisError("NFI route tags are invalid")
        supported_long.update(entry_tags)
    if not supported_long:
        raise StrategyAnalysisError("NFI adapter has no executable entry-tag route")
    required = {"nfi_exec_enter_long", "nfi_exec_enter_tag"}
    # X7 stores long and short labels in one ``enter_tag`` column. A spot row
    # can therefore carry a valid long label plus a simultaneous short label,
    # even though Freqtrade will only open the long side. Compile the short
    # scope in spot mode too so this source-defined compound representation is
    # validated by meaning rather than by a side-specific string whitelist.
    managed_short = manager.get("managed_short_routes")
    if not isinstance(managed_short, list) or not managed_short:
        raise StrategyAnalysisError("NFI managed-short routes are invalid")
    supported_short: set[str] = set()
    for route in managed_short:
        entry_tags = route.get("entry_tags") if isinstance(route, dict) else None
        if not isinstance(entry_tags, list) or not all(
            isinstance(tag, str) and tag for tag in entry_tags
        ):
            raise StrategyAnalysisError("NFI short route tags are invalid")
        supported_short.update(entry_tags)
    if not supported_short:
        raise StrategyAnalysisError("NFI adapter has no executable short entry-tag route")
    if can_short:
        required.add("nfi_exec_enter_short")
    missing = required - set(frame.columns)
    if missing:
        raise StrategyAnalysisError(
            "NFI route scope check is missing: " + ", ".join(sorted(missing))
        )
    compiled_callback_tags = supported_long | supported_short
    _validate_signal_tags(
        _series_column(frame, "nfi_exec_enter_long"),
        _series_column(frame, "nfi_exec_enter_tag"),
        compiled_callback_tags,
        required_side_tags=supported_long,
        pair=pair,
        side="long",
    )
    if can_short:
        _validate_signal_tags(
            _series_column(frame, "nfi_exec_enter_short"),
            _series_column(frame, "nfi_exec_enter_tag"),
            compiled_callback_tags,
            required_side_tags=supported_short,
            pair=pair,
            side="short",
        )


def _series_column(frame: pd.DataFrame, name: str) -> pd.Series:
    column = frame[name]
    if not isinstance(column, pd.Series):
        raise StrategyAnalysisError(f"NFI vector column is duplicated: {name}")
    return column


def _validate_signal_tags(
    signals: pd.Series,
    tags: pd.Series,
    supported: set[str],
    *,
    required_side_tags: set[str],
    pair: str,
    side: str,
) -> None:
    for signal, raw_tag in zip(signals, tags, strict=True):
        if not _enabled(signal):
            continue
        entry_tag = _optional_text(raw_tag)
        words = entry_tag.split() if entry_tag is not None else []
        if (
            not words
            or any(word not in supported for word in words)
            or not any(word in required_side_tags for word in words)
        ):
            shown = entry_tag if entry_tag is not None else "<none>"
            side_label = "" if side == "long" else f"{side} "
            raise StrategyAnalysisError(
                f"NFI adapter does not support {side_label}entry tag {shown!r} for {pair}"
            )
        # Later signals may occur while the pair already has an open trade and
        # are therefore ignored by Freqtrade. The native chronological loop
        # performs the definitive route check only when a signal can open a
        # trade; rejecting every raw vector signal would reject valid fixtures.
        break


def _x7_signal_candles(
    frame: pd.DataFrame,
    *,
    can_short: bool,
) -> list[dict[str, Any]]:
    required = {
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "nfi_exec_enter_long",
        "nfi_exec_exit_long",
    }
    missing = required - set(frame.columns)
    if can_short:
        missing.update(
            {
                "nfi_exec_enter_short",
                "nfi_exec_exit_short",
                "nfi_exec_funding_rate",
                "nfi_exec_funding_mark_price",
            }
            - set(frame.columns)
        )
    if missing:
        raise StrategyAnalysisError(
            f"vector artifact is missing execution columns: {', '.join(sorted(missing))}"
        )
    records = []
    previous_close: float | None = None
    for row in frame.to_dict(orient="records"):
        timestamp = pd.Timestamp(row["date"])
        timestamp = (
            timestamp.tz_localize("UTC")
            if timestamp.tzinfo is None
            else timestamp.tz_convert("UTC")
        )
        enter_tag = _optional_text(row.get("nfi_exec_enter_tag"))
        exit_tag = _optional_text(row.get("nfi_exec_exit_tag"))
        funding_rate = _optional_finite_number(row.get("nfi_exec_funding_rate"))
        funding_mark_price = _optional_finite_number(row.get("nfi_exec_funding_mark_price"))
        if (funding_rate is None) != (funding_mark_price is None):
            raise StrategyAnalysisError(
                "funding rate and mark price must be present on the same candle"
            )
        if funding_mark_price is not None and funding_mark_price <= 0.0:
            raise StrategyAnalysisError("funding mark price must be positive")
        records.append(
            {
                "timestamp_ms": timestamp.value // 1_000_000,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "previous_close": previous_close,
                "enter_long": (
                    {
                        "tag": enter_tag,
                        "leverage": None,
                        "liquidation_price": None,
                    }
                    if _enabled(row["nfi_exec_enter_long"])
                    else None
                ),
                "enter_short": (
                    {
                        "tag": enter_tag,
                        "leverage": None,
                        "liquidation_price": None,
                    }
                    if can_short and _enabled(row["nfi_exec_enter_short"])
                    else None
                ),
                "exit_long": (
                    {"reason": exit_tag or "exit_signal"}
                    if _enabled(row["nfi_exec_exit_long"])
                    else None
                ),
                "exit_short": (
                    {"reason": exit_tag or "exit_signal"}
                    if can_short and _enabled(row["nfi_exec_exit_short"])
                    else None
                ),
                "funding_rate": funding_rate,
                "funding_mark_price": funding_mark_price,
                "adjustment": None,
            }
        )
        previous_close = float(row["close"])
    return records


def _x7_feature_columns(
    frame: pd.DataFrame,
    required_features: list[str],
) -> dict[str, list[Any]]:
    missing = set(required_features) - set(frame.columns)
    if missing:
        raise StrategyAnalysisError(
            "vector artifact is missing trade-decision features: " + ", ".join(sorted(missing))
        )
    return {
        column: [_scalar_feature_value(value, column) for value in frame[column]]
        for column in required_features
    }


def _scalar_feature_value(value: Any, column: str) -> Any:
    missing = pd.isna(value)
    if isinstance(missing, bool) and missing:
        return {"$float": "nan"}
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if not isinstance(value, numbers.Real):
        raise StrategyAnalysisError(f"trade-decision feature {column} must contain numeric scalars")
    number = float(value)
    if math.isnan(number):
        return {"$float": "nan"}
    if math.isinf(number):
        return {"$float": "infinity" if number > 0 else "-infinity"}
    return number


def _enabled(value: Any) -> bool:
    return not pd.isna(value) and float(value) != 0.0


def _optional_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value)
    return text if text and text != EMPTY_TAG_TRANSPORT_SENTINEL else None


def _optional_finite_number(value: Any) -> float | None:
    """Decode a nullable funding scalar without accepting infinities."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise StrategyAnalysisError("funding event values must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise StrategyAnalysisError("funding event values must be finite")
    return result
