"""Read-only tracing hooks for the pinned Freqtrade 2026.5.1 reference."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import math
import numbers
import os
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any

PINNED_FREQTRADE_VERSION = "2026.5.1"
PINNED_METHOD_HASHES = {
    "backtest": "c65dbdd3427a758d2b7b3b9e5c113970cc39c94affadfde70d8e6a2ab9452fbf",
    "backtest_loop": "e6db4c1d0a841366eafd78d3ec2cc4d71d7a50b9331d45a5ebb9d3fd6558bc77",
    "_enter_trade": "e04325821a53f56ac22e1ee6a1ee13355f2eca7b88c193837364febf6c8bd952",
    "_exit_trade": "ab4bfae7ee432cfc740b9a71a301ce16fe261ba5545a0d58676184a5f3c1cbb7",
    "_check_trade_exit": "397f4c7be265c58fd9d596ae7421c8493379b9ef1ac5e20e175f05cd4643d1f8",
    "_check_adjust_trade_for_candle": (
        "900e3ec7f067fb67b56f190babed637712e26f5c08cb9e90ee5fd0df9849af8d"
    ),
    "_get_ohlcv_as_lists": "b48e1e054762ddd55bdde5c8f7b4e27b906cd8299640fe94b25eec1adbf51a01",
    "manage_open_orders": "31ae85853ad0eed91ef53d8fb5b2e39c5b5cec0f2a0566a7931e51c6eccfeac0",
    "_set_strategy": "1c0e8070fcd84ab19e05abe9a2f94c53355bb59a36e4f73ef32b778e9856efbd",
}
PINNED_EXCHANGE_METHOD_HASHES = {
    "_api_reload_markets": ("5ff713afa253ccb9538a2e3a5e03b101767c3e6dbf509246a34288197584f07e"),
}
PROFILE_PHASES = ("indicators", "callbacks", "trade_scans", "event_simulation")
_INSTALLED = False


def install_reference_tracer() -> None:
    """Patch only known methods and refuse a drifting Freqtrade implementation."""
    global _INSTALLED
    if _INSTALLED:
        return

    import ccxt
    import freqtrade
    from freqtrade.exchange.exchange import Exchange
    from freqtrade.optimize.backtesting import Backtesting

    version = getattr(freqtrade, "__version__", "")
    if version != PINNED_FREQTRADE_VERSION:
        raise RuntimeError(
            f"NFI tracer requires Freqtrade {PINNED_FREQTRADE_VERSION}, found {version!r}"
        )
    for name, expected_hash in PINNED_METHOD_HASHES.items():
        actual_hash = hashlib.sha256(
            inspect.getsource(getattr(Backtesting, name)).encode()
        ).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"NFI tracer source drift in Backtesting.{name}: "
                f"expected {expected_hash}, actual {actual_hash}"
            )
    for name, expected_hash in PINNED_EXCHANGE_METHOD_HASHES.items():
        actual_hash = hashlib.sha256(
            inspect.getsource(getattr(Exchange, name)).encode()
        ).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"NFI tracer source drift in Exchange.{name}: "
                f"expected {expected_hash}, actual {actual_hash}"
            )

    _patch_market_loader(Exchange, ccxt.__version__)
    _patch_backtest(Backtesting)
    _patch_backtest_loop(Backtesting)
    _patch_enter_trade(Backtesting)
    _patch_exit_trade(Backtesting)
    _patch_trade_exit_check(Backtesting)
    _patch_adjustment_check(Backtesting)
    _patch_open_order_management(Backtesting)
    _patch_ohlcv_lists(Backtesting)
    _patch_set_strategy(Backtesting)
    _INSTALLED = True


def _patch_market_loader(cls: type, ccxt_version: str) -> None:
    original = cls._api_reload_markets

    @wraps(original)
    async def traced(self: Any, reload: bool = False) -> Any:
        snapshot_path = os.environ.get("NFI_MARKET_SNAPSHOT_PATH")
        if snapshot_path:
            snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
            _validate_market_snapshot(snapshot, self, ccxt_version)
            self._api_async.set_markets(
                snapshot["markets"],
                snapshot["currencies"],
            )
            self._api_async.options = snapshot["options"]
            return None

        result = await original(self, reload=reload)
        capture_path = os.environ.get("NFI_MARKET_CAPTURE_PATH")
        if capture_path:
            destination = Path(capture_path)
            if not destination.exists():
                whitelist = self._config["exchange"]["pair_whitelist"]
                missing = [pair for pair in whitelist if pair not in self._api_async.markets]
                if missing:
                    raise RuntimeError(
                        f"cannot capture missing whitelisted markets: {', '.join(missing)}"
                    )
                markets = {pair: self._api_async.markets[pair] for pair in sorted(whitelist)}
                currency_codes = {
                    code
                    for market in markets.values()
                    for code in (
                        market.get("base"),
                        market.get("quote"),
                        market.get("settle"),
                    )
                    if code
                }
                currencies = {
                    code: self._api_async.currencies[code]
                    for code in sorted(currency_codes)
                    if code in self._api_async.currencies
                }
                snapshot = {
                    "schema_version": "1.0.0",
                    "freqtrade_version": PINNED_FREQTRADE_VERSION,
                    "ccxt_version": ccxt_version,
                    "exchange": str(self._config["exchange"]["name"]).lower(),
                    "trading_mode": _enum_value(self.trading_mode),
                    "markets": markets,
                    "currencies": currencies,
                    "options": self._api_async.options,
                }
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        return result

    cls._api_reload_markets = traced


def _validate_market_snapshot(snapshot: Any, exchange: Any, ccxt_version: str) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != "1.0.0":
        raise RuntimeError("invalid NFI market snapshot schema")
    expected = {
        "freqtrade_version": PINNED_FREQTRADE_VERSION,
        "ccxt_version": ccxt_version,
        "exchange": str(exchange._config["exchange"]["name"]).lower(),
        "trading_mode": _enum_value(exchange.trading_mode),
    }
    for field, value in expected.items():
        if snapshot.get(field) != value:
            raise RuntimeError(
                f"NFI market snapshot {field} mismatch: "
                f"expected {value!r}, actual {snapshot.get(field)!r}"
            )
    for field in ("markets", "currencies", "options"):
        if not isinstance(snapshot.get(field), dict):
            raise RuntimeError(f"NFI market snapshot {field} must be an object")


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _patch_backtest(cls: type) -> None:
    original = cls.backtest

    @wraps(original)
    def traced(self: Any, *args: Any, **kwargs: Any) -> Any:
        _initialize_profile(self)
        if os.environ.get("NFI_TRACE_PATH"):
            _writer(self)
        try:
            return original(self, *args, **kwargs)
        finally:
            writer = getattr(self, "_nfi_state_trace_writer", None)
            if writer is not None:
                writer.close()
            _flush_callback_audit(self)
            _flush_profile(self)

    cls.backtest = traced


def _patch_backtest_loop(cls: type) -> None:
    original = cls.backtest_loop

    @wraps(original)
    def traced(
        self: Any,
        row: tuple,
        pair: str,
        current_time: datetime,
        trade_dir: str | None,
        can_enter: bool,
    ) -> Any:
        started_ns = time.perf_counter_ns()
        try:
            result = original(self, row, pair, current_time, trade_dir, can_enter)
        finally:
            _record_profile(self, "event_simulation", time.perf_counter_ns() - started_ns)
        _append(self, current_time, "candle.after", pair=pair)
        return result

    cls.backtest_loop = traced


def _patch_ohlcv_lists(cls: type) -> None:
    """Audit the exact shifted signals consumed by Freqtrade's hot loop.

    The wrapped method has already called ``ft_advise_signals``, trimmed the
    timerange, shifted decisions by one candle, and converted the result to
    immutable row lists. Reading those rows avoids invoking strategy code a
    second time, which could both distort the profile and mutate stateful
    strategies.
    """

    original = cls._get_ohlcv_as_lists

    @wraps(original)
    def traced(self: Any, processed: dict[str, Any]) -> dict[str, tuple]:
        data = original(self, processed)
        _write_signal_audit(self, data, processed)
        return data

    cls._get_ohlcv_as_lists = traced


def _write_signal_audit(
    backtesting: Any,
    data: dict[str, tuple],
    processed: dict[str, Any],
) -> None:
    destination_text = os.environ.get("NFI_SIGNAL_AUDIT_PATH")
    if not destination_text:
        return

    # Importing HEADERS only inside the active reference container keeps this
    # tracer module importable by the lightweight host-side unit tests.
    from freqtrade.optimize.backtesting import HEADERS

    audit = _build_signal_audit(backtesting, data, HEADERS)
    _add_signal_feature_samples(audit, processed)
    destination = Path(destination_text)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    temporary.write_text(
        json.dumps(
            audit,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _build_signal_audit(
    backtesting: Any,
    data: dict[str, tuple],
    headers: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Return a compact, canonical inventory of executable signal rows."""

    indices = {name: headers.index(name) for name in headers}
    required = (
        "date",
        "enter_long",
        "exit_long",
        "enter_short",
        "exit_short",
        "enter_tag",
        "exit_tag",
    )
    missing = [name for name in required if name not in indices]
    if missing:
        raise RuntimeError(f"NFI signal audit missing Freqtrade headers: {', '.join(missing)}")

    pairs: dict[str, Any] = {}
    for pair in sorted(data):
        signals = []
        rows = data[pair]
        for row in rows:
            event = {
                "timestamp_ms": _timestamp_ms(row[indices["date"]]),
                "enter_long": _signal_enabled(row[indices["enter_long"]]),
                "exit_long": _signal_enabled(row[indices["exit_long"]]),
                "enter_short": _signal_enabled(row[indices["enter_short"]]),
                "exit_short": _signal_enabled(row[indices["exit_short"]]),
                "enter_tag": _optional_signal_tag(row[indices["enter_tag"]]),
                "exit_tag": _optional_signal_tag(row[indices["exit_tag"]]),
            }
            if any(
                event[name]
                for name in ("enter_long", "exit_long", "enter_short", "exit_short")
            ):
                signals.append(event)
        encoded = json.dumps(
            signals,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        pairs[pair] = {
            "rows": len(rows),
            "signal_rows": len(signals),
            "signals_sha256": hashlib.sha256(encoded).hexdigest(),
            "signals": signals,
        }

    strategy = getattr(backtesting, "strategy", None)
    return {
        "schema_version": "1.0.0",
        "freqtrade_version": PINNED_FREQTRADE_VERSION,
        "strategy": type(strategy).__name__ if strategy is not None else None,
        "trading_mode": str(backtesting.config.get("trading_mode", "")),
        "timeframe": str(backtesting.config.get("timeframe", "")),
        "pairs": pairs,
    }


def _add_signal_feature_samples(
    audit: dict[str, Any],
    processed: dict[str, Any],
) -> None:
    """Optionally attach selected unshifted strategy-frame values.

    This debug surface is deliberately opt-in: ordinary parity runs retain a
    tiny audit artifact, while a divergence investigation can request either a
    comma-separated column list or ``*``. Samples include requested timestamps
    and every source signal row, making the one-candle shift explicit.
    """

    feature_text = os.environ.get("NFI_SIGNAL_AUDIT_FEATURES", "").strip()
    if not feature_text:
        return
    configured_features = [item.strip() for item in feature_text.split(",") if item.strip()]
    requested_timestamps = {
        int(item.strip())
        for item in os.environ.get("NFI_SIGNAL_AUDIT_TIMESTAMPS_MS", "").split(",")
        if item.strip()
    }
    signal_columns = ("enter_long", "exit_long", "enter_short", "exit_short")
    tag_columns = ("enter_tag", "exit_tag")

    for pair in sorted(processed):
        frame = processed[pair]
        if "date" not in frame:
            raise RuntimeError(f"NFI signal feature audit missing date column for {pair}")
        features = (
            [str(column) for column in frame.columns]
            if configured_features == ["*"]
            else configured_features
        )
        missing = [name for name in features if name not in frame]
        if missing:
            raise RuntimeError(
                f"NFI signal feature audit missing columns for {pair}: {', '.join(missing)}"
            )

        row_timestamps = frame["date"].map(_timestamp_ms)
        selected = row_timestamps.isin(requested_timestamps)
        present_signal_columns = [name for name in signal_columns if name in frame]
        if present_signal_columns:
            source_signals = frame[present_signal_columns].fillna(0).astype(bool).any(axis=1)
            selected = selected | source_signals

        sample_columns = list(
            dict.fromkeys(("date", *signal_columns, *tag_columns, *features))
        )
        sample_columns = [name for name in sample_columns if name in frame]
        samples = []
        for index, row in frame.loc[selected, sample_columns].iterrows():
            values = {
                name: _canonical_feature_value(row[name])
                for name in sample_columns
                if name != "date"
            }
            samples.append(
                {
                    "timestamp_ms": _timestamp_ms(row["date"]),
                    "row_index": int(index) if isinstance(index, numbers.Integral) else str(index),
                    "values": values,
                }
            )
        audit["pairs"][pair]["feature_columns"] = features
        audit["pairs"][pair]["feature_samples"] = samples


def _canonical_feature_value(value: Any) -> Any:
    if type(value).__name__ == "NAType":
        return None
    if type(value).__module__ == "numpy" and hasattr(value, "item"):
        value = value.item()
    return _canonicalize(value)


def _signal_enabled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, numbers.Real) and math.isnan(float(value)):
        return False
    return bool(value)


def _optional_signal_tag(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, numbers.Real) and math.isnan(float(value)):
        return None
    text = str(value)
    return text if text else None


def _patch_enter_trade(cls: type) -> None:
    original = cls._enter_trade

    @wraps(original)
    def traced(self: Any, pair: str, row: tuple, direction: str, *args: Any, **kwargs: Any) -> Any:
        result = original(self, pair, row, direction, *args, **kwargs)
        timestamp = row[0].to_pydatetime()
        _append(self, timestamp, "trade.entry", pair=pair, callback="confirm_trade_entry")
        return result

    cls._enter_trade = traced


def _patch_exit_trade(cls: type) -> None:
    original = cls._exit_trade

    @wraps(original)
    def traced(
        self: Any,
        trade: Any,
        sell_row: tuple,
        close_rate: float,
        amount: float,
        exit_reason: str | None,
    ) -> Any:
        result = original(self, trade, sell_row, close_rate, amount, exit_reason)
        timestamp = sell_row[0].to_pydatetime()
        _append(self, timestamp, "trade.exit_order", pair=trade.pair)
        return result

    cls._exit_trade = traced


def _patch_trade_exit_check(cls: type) -> None:
    original = cls._check_trade_exit

    @wraps(original)
    def traced(self: Any, trade: Any, row: tuple, current_time: datetime) -> Any:
        started_ns = time.perf_counter_ns()
        try:
            result = original(self, trade, row, current_time)
        finally:
            _record_profile(self, "callbacks", time.perf_counter_ns() - started_ns)
        _append(self, current_time, "trade.exit_check", pair=trade.pair, callback="custom_exit")
        return result

    cls._check_trade_exit = traced


def _patch_adjustment_check(cls: type) -> None:
    original = cls._check_adjust_trade_for_candle

    @wraps(original)
    def traced(self: Any, trade: Any, row: tuple, current_time: datetime) -> Any:
        started_ns = time.perf_counter_ns()
        try:
            result = original(self, trade, row, current_time)
        finally:
            _record_profile(self, "callbacks", time.perf_counter_ns() - started_ns)
        _append(
            self,
            current_time,
            "trade.adjustment_check",
            pair=trade.pair,
            callback="adjust_trade_position",
        )
        return result

    cls._check_adjust_trade_for_candle = traced


def _patch_open_order_management(cls: type) -> None:
    original = cls.manage_open_orders

    @wraps(original)
    def traced(self: Any, trade: Any, current_time: datetime, row: tuple) -> Any:
        started_ns = time.perf_counter_ns()
        try:
            result = original(self, trade, current_time, row)
        finally:
            _record_profile(self, "trade_scans", time.perf_counter_ns() - started_ns)
        _append(self, current_time, "order.manage", pair=trade.pair)
        return result

    cls.manage_open_orders = traced


def _patch_set_strategy(cls: type) -> None:
    original = cls._set_strategy

    @wraps(original)
    def traced(self: Any, strategy: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, strategy, *args, **kwargs)
        _initialize_profile(self)
        _install_callback_audit(self)
        advise = self.strategy.advise_all_indicators
        if getattr(advise, "_nfi_profile_wrapper", False):
            return result

        @wraps(advise)
        def timed_advise(*call_args: Any, **call_kwargs: Any) -> Any:
            started_ns = time.perf_counter_ns()
            try:
                return advise(*call_args, **call_kwargs)
            finally:
                _record_profile(self, "indicators", time.perf_counter_ns() - started_ns)

        timed_advise._nfi_profile_wrapper = True
        self.strategy.advise_all_indicators = timed_advise
        return result

    cls._set_strategy = traced


_AUDITED_CALLBACKS = (
    "adjust_trade_position",
    "confirm_trade_entry",
    "confirm_trade_exit",
    "custom_exit",
    "custom_stake_amount",
    "order_filled",
)
_AUDITED_CUSTOM_DATA_KEYS = (
    "system_version",
    "derisk_level_1",
    "derisk_level_2",
    "derisk_level_3",
    "grind_1_cluster_max_profit_stake",
    "grind_1_cluster_max_profit_rate",
    "grind_2_cluster_max_profit_stake",
    "grind_2_cluster_max_profit_rate",
    "grind_3_cluster_max_profit_stake",
    "grind_3_cluster_max_profit_rate",
    "grind_4_cluster_max_profit_stake",
    "grind_4_cluster_max_profit_rate",
    "grind_5_cluster_max_profit_stake",
    "grind_5_cluster_max_profit_rate",
)


def _install_callback_audit(backtesting: Any) -> None:
    if not os.environ.get("NFI_CALLBACK_AUDIT_PATH"):
        return
    if getattr(backtesting, "_nfi_callback_audit", None) is None:
        backtesting._nfi_callback_audit = {
            "schema_version": "1.0.0",
            "callbacks": {},
        }
    for name in _AUDITED_CALLBACKS:
        callback = getattr(backtesting.strategy, name, None)
        if callback is None or getattr(callback, "_nfi_callback_audit_wrapper", False):
            continue
        signature = inspect.signature(callback)

        @wraps(callback)
        def audited(
            *args: Any,
            __callback: Any = callback,
            __name: str = name,
            __signature: Any = signature,
            **kwargs: Any,
        ) -> Any:
            bound = __signature.bind_partial(*args, **kwargs)
            before = _audit_trade_state(bound.arguments.get("trade"))
            try:
                result = __callback(*args, **kwargs)
            except Exception as exc:
                _record_callback_audit(
                    backtesting,
                    __name,
                    bound.arguments,
                    before,
                    _audit_trade_state(bound.arguments.get("trade")),
                    None,
                    f"{type(exc).__name__}: {exc}",
                )
                raise
            _record_callback_audit(
                backtesting,
                __name,
                bound.arguments,
                before,
                _audit_trade_state(bound.arguments.get("trade")),
                result,
                None,
            )
            return result

        audited._nfi_callback_audit_wrapper = True
        setattr(backtesting.strategy, name, audited)


def _record_callback_audit(
    backtesting: Any,
    name: str,
    arguments: dict[str, Any],
    before: Any,
    after: Any,
    result: Any,
    error: str | None,
) -> None:
    audit = getattr(backtesting, "_nfi_callback_audit", None)
    if not isinstance(audit, dict):
        return
    callbacks = audit["callbacks"]
    callback = callbacks.setdefault(name, {"calls": 0, "outcomes": {}})
    callback["calls"] += 1
    outcome = {
        "result": _audit_result_class(result),
        "entry_tag": _audit_entry_tag(arguments),
        "order_tag": _audit_order_tag(arguments),
        "side": _audit_side(arguments),
        "state_changed": before != after,
        "error": error,
    }
    key = json.dumps(
        outcome,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    bucket = callback["outcomes"].setdefault(
        key,
        {
            "signature": outcome,
            "count": 0,
            "samples": [],
        },
    )
    bucket["count"] += 1
    timestamp = _audit_timestamp(arguments)
    requested_timestamps = {
        int(item.strip())
        for item in os.environ.get("NFI_CALLBACK_AUDIT_TIMESTAMPS_MS", "").split(",")
        if item.strip()
    }
    # Ordinary audits retain at most three examples for each outcome so the
    # artifact remains compact. A parity investigation may name exact callback
    # timestamps that must survive that cap; duplicate callbacks at the same
    # timestamp are still recorded because their before/after state can differ.
    if len(bucket["samples"]) < 3 or timestamp in requested_timestamps:
        bucket["samples"].append(
            {
                "pair": _audit_pair(arguments),
                "timestamp": timestamp,
                "current_rate": _audit_number(arguments.get("current_rate")),
                "current_profit": _audit_number(arguments.get("current_profit")),
                "min_stake": _audit_number(arguments.get("min_stake")),
                "max_stake": _audit_number(arguments.get("max_stake")),
                "strategy_grind_entry_tag": _audit_strategy_grind_entry_tag(backtesting),
                "result": _canonicalize(result),
                "before": before,
                "after": after,
                "feature_window": _audit_feature_window(backtesting, arguments),
            }
        )


def _audit_result_class(result: Any) -> dict[str, Any]:
    if result is None:
        return {"kind": "none"}
    if isinstance(result, bool):
        return {"kind": "bool", "value": result}
    if isinstance(result, str):
        return {"kind": "text", "value": result}
    if isinstance(result, tuple):
        tag = result[1] if len(result) > 1 and isinstance(result[1], str) else None
        return {"kind": "tuple", "length": len(result), "tag": tag}
    if isinstance(result, numbers.Real):
        return {"kind": "number"}
    return {"kind": type(result).__name__}


def _audit_trade_state(trade: Any) -> Any:
    if trade is None:
        return None
    orders = list(getattr(trade, "orders", []) or [])
    last_order = orders[-1] if orders else None
    return _canonicalize(
        {
            "id": getattr(trade, "id", None),
            "amount": getattr(trade, "amount", None),
            "open_rate": getattr(trade, "open_rate", None),
            "stake_amount": getattr(trade, "stake_amount", None),
            "max_stake_amount": getattr(trade, "max_stake_amount", None),
            "liquidation_price": getattr(trade, "liquidation_price", None),
            "stop_loss": getattr(trade, "stop_loss", None),
            "successful_entries": getattr(trade, "nr_of_successful_entries", None),
            "successful_exits": getattr(trade, "nr_of_successful_exits", None),
            "order_count": len(orders),
            "last_order_tag": getattr(last_order, "ft_order_tag", None),
            "custom_data": _audit_custom_data(trade),
        }
    )


def _audit_custom_data(trade: Any) -> dict[str, Any] | None:
    getter = getattr(trade, "get_custom_data", None)
    if callable(getter):
        return {key: getter(key, None) for key in _AUDITED_CUSTOM_DATA_KEYS}
    custom_data = getattr(trade, "custom_data", None)
    if not isinstance(custom_data, dict):
        return None
    return {key: custom_data.get(key) for key in _AUDITED_CUSTOM_DATA_KEYS}


def _audit_entry_tag(arguments: dict[str, Any]) -> str | None:
    value = arguments.get("entry_tag")
    if value is None:
        trade = arguments.get("trade")
        value = getattr(trade, "enter_tag", None)
    return value if isinstance(value, str) else None


def _audit_order_tag(arguments: dict[str, Any]) -> str | None:
    value = getattr(arguments.get("order"), "ft_order_tag", None)
    return value if isinstance(value, str) else None


def _audit_side(arguments: dict[str, Any]) -> str | None:
    value = arguments.get("side")
    if value is not None:
        return _enum_value(value)
    trade = arguments.get("trade")
    if trade is None:
        return None
    return "short" if bool(getattr(trade, "is_short", False)) else "long"


def _audit_pair(arguments: dict[str, Any]) -> str | None:
    value = arguments.get("pair")
    if value is None:
        value = getattr(arguments.get("trade"), "pair", None)
    return value if isinstance(value, str) else None


def _audit_timestamp(arguments: dict[str, Any]) -> int | None:
    for name in ("current_time", "date"):
        value = arguments.get(name)
        if isinstance(value, datetime):
            return int(value.timestamp() * 1000)
    return None


def _audit_strategy_grind_entry_tag(backtesting: Any) -> str | None:
    """Expose the NFI branch label set by `long_grind_entry_v3`, when present."""
    value = getattr(getattr(backtesting, "strategy", None), "_grind_entry_tag", None)
    return value if isinstance(value, str) else None


def _audit_number(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    return _canonicalize(value)


def _audit_feature_window(
    backtesting: Any,
    arguments: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Capture the exact dataframe rows visible to a callback when requested.

    This is opt-in because materializing dataframe rows in every callback audit
    would distort the reference runtime. Outcome sampling limits this helper to
    at most three windows per distinct callback result signature.
    """
    requested = os.environ.get("NFI_CALLBACK_FEATURES")
    if not requested:
        return None
    pair = _audit_pair(arguments)
    if pair is None:
        return None
    columns = [name.strip() for name in requested.split(",") if name.strip()]
    dataframe, _ = backtesting.strategy.dp.get_analyzed_dataframe(
        pair,
        backtesting.strategy.timeframe,
    )
    available = ["date", *(name for name in columns if name in dataframe.columns)]
    if not available or dataframe.empty:
        return []
    return [
        {
            name: (
                int(value.timestamp() * 1000)
                if name == "date" and hasattr(value, "timestamp")
                else _canonicalize(value)
            )
            for name, value in row.items()
        }
        for row in dataframe.loc[:, available].tail(2).to_dict(orient="records")
    ]


def _flush_callback_audit(backtesting: Any) -> None:
    destination_text = os.environ.get("NFI_CALLBACK_AUDIT_PATH")
    audit = getattr(backtesting, "_nfi_callback_audit", None)
    if not destination_text or not isinstance(audit, dict):
        return
    callbacks = audit.get("callbacks", {})
    for callback in callbacks.values():
        outcomes = callback.get("outcomes", {})
        callback["outcomes"] = sorted(
            outcomes.values(),
            key=lambda item: json.dumps(
                item["signature"],
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    destination = Path(destination_text)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            audit,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _writer(backtesting: Any) -> Any:
    writer = getattr(backtesting, "_nfi_state_trace_writer", None)
    if writer is not None:
        return writer

    from nfi_backtest_engine.state_trace import StateTraceWriter

    required = (
        "NFI_TRACE_PATH",
        "NFI_TRACE_RUN_ID",
        "NFI_TRACE_INPUT_SHA256",
        "NFI_TRACE_STRATEGY_SHA256",
        "NFI_TRACE_PROFILE_SHA256",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing NFI trace environment: {', '.join(missing)}")
    writer = StateTraceWriter(
        Path(os.environ["NFI_TRACE_PATH"]),
        source="freqtrade-reference",
        run_id=os.environ["NFI_TRACE_RUN_ID"],
        input_sha256=os.environ["NFI_TRACE_INPUT_SHA256"],
        strategy_sha256=os.environ["NFI_TRACE_STRATEGY_SHA256"],
        profile_sha256=os.environ["NFI_TRACE_PROFILE_SHA256"],
        trading_mode=str(backtesting.config["trading_mode"]),
        include_state=os.environ.get("NFI_TRACE_INCLUDE_STATE") == "1",
    )
    backtesting._nfi_state_trace_writer = writer
    return writer


def _append(
    backtesting: Any,
    timestamp: datetime,
    phase: str,
    *,
    pair: str | None = None,
    callback: str | None = None,
) -> None:
    if not os.environ.get("NFI_TRACE_PATH"):
        return
    writer = _writer(backtesting)
    writer.append(
        timestamp_ms=_timestamp_ms(timestamp),
        phase=phase,
        pair=pair,
        callback=callback,
        state=_snapshot(backtesting),
    )


def _initialize_profile(backtesting: Any) -> None:
    if not os.environ.get("NFI_BTE_PROFILE_EVENTS"):
        return
    if isinstance(getattr(backtesting, "_nfi_profile_totals", None), dict):
        return
    backtesting._nfi_profile_totals = {
        phase: {"calls": 0, "duration_ns": 0, "max_duration_ns": 0} for phase in PROFILE_PHASES
    }


def _record_profile(backtesting: Any, phase: str, duration_ns: int) -> None:
    totals = getattr(backtesting, "_nfi_profile_totals", None)
    if totals is None:
        return
    record = totals[phase]
    record["calls"] += 1
    record["duration_ns"] += duration_ns
    record["max_duration_ns"] = max(record["max_duration_ns"], duration_ns)


def _flush_profile(backtesting: Any) -> None:
    destination = os.environ.get("NFI_BTE_PROFILE_EVENTS")
    totals = getattr(backtesting, "_nfi_profile_totals", None)
    if not destination or totals is None:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for phase in PROFILE_PHASES:
            record = {
                "schema_version": "1.0.0",
                "phase": phase,
                **totals[phase],
                "process_id": os.getpid(),
            }
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    backtesting._nfi_profile_totals = None


def _snapshot(backtesting: Any) -> dict[str, Any]:
    from freqtrade.persistence import LocalTrade, PairLocks

    balances = backtesting.wallets.get_all_balances()
    trades = sorted(LocalTrade.bt_trades, key=lambda trade: trade.id or 0)
    locks = sorted(
        PairLocks.get_all_locks(),
        key=lambda lock: (
            lock.lock_time,
            lock.pair,
            lock.side,
            lock.id if lock.id is not None else -1,
        ),
    )
    return _canonicalize(
        {
            "wallets": balances,
            "open_trade_count": LocalTrade.bt_open_open_trade_count,
            "total_profit": LocalTrade.bt_total_profit,
            "trades": [trade.to_json(minified=False) for trade in trades],
            "locks": [lock.to_json() for lock in locks],
            "counters": {
                "trade_id": backtesting.trade_id_counter,
                "order_id": backtesting.order_id_counter,
                "rejected_signals": backtesting.rejected_trades,
                "timedout_entry_orders": backtesting.timedout_entry_orders,
                "timedout_exit_orders": backtesting.timedout_exit_orders,
                "canceled_trade_entries": backtesting.canceled_trade_entries,
                "canceled_entry_orders": backtesting.canceled_entry_orders,
                "replaced_entry_orders": backtesting.replaced_entry_orders,
            },
        }
    )


def _canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        if math.isnan(number):
            return {"$float": "nan"}
        if math.isinf(number):
            return {"$float": "infinity" if number > 0 else "-infinity"}
        return _decimal_string(Decimal(repr(number)))
    if isinstance(value, Decimal):
        if value.is_nan():
            return {"$decimal": "nan"}
        if value.is_infinite():
            return {"$decimal": "infinity" if value > 0 else "-infinity"}
        return _decimal_string(value)
    if isinstance(value, datetime):
        return _timestamp_ms(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _canonicalize(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        items = [_canonicalize(item) for item in value]
        return sorted(items, key=repr)
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if hasattr(value, "_asdict"):
        return _canonicalize(value._asdict())
    if hasattr(value, "to_json"):
        return _canonicalize(value.to_json())
    raise TypeError(f"unsupported reference state value {type(value).__name__}")


def _decimal_string(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite reference state decimal")
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _timestamp_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp() * 1000)
