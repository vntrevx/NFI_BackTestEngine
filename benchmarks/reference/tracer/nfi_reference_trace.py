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
from datetime import date, datetime, timezone
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
    "manage_open_orders": "31ae85853ad0eed91ef53d8fb5b2e39c5b5cec0f2a0566a7931e51c6eccfeac0",
    "_set_strategy": "1c0e8070fcd84ab19e05abe9a2f94c53355bb59a36e4f73ef32b778e9856efbd",
}
PINNED_EXCHANGE_METHOD_HASHES = {
    "_api_reload_markets": (
        "5ff713afa253ccb9538a2e3a5e03b101767c3e6dbf509246a34288197584f07e"
    ),
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
                markets = {
                    pair: self._api_async.markets[pair]
                    for pair in sorted(whitelist)
                }
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
        phase: {"calls": 0, "duration_ns": 0, "max_duration_ns": 0}
        for phase in PROFILE_PHASES
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
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)
