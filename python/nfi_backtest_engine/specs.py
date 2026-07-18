"""Versioned JSON Schema loading and semantic validation."""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .canonical import canonical_decimal
from .errors import NormalizationError, SpecValidationError

TRADE_SURFACE_SCHEMA = "trade-surface.schema.json"
TRADE_SURFACE_V2_SCHEMA = "trade-surface-v2.schema.json"
BENCHMARK_FIXTURE_SCHEMA = "benchmark-fixture.schema.json"
BENCHMARK_FIXTURE_V2_SCHEMA = "benchmark-fixture-v2.schema.json"

_TRADE_DECIMAL_FIELDS = (
    "open_rate",
    "close_rate",
    "amount",
    "stake_amount",
    "max_stake_amount",
    "leverage",
    "liquidation_price",
    "initial_stop_loss",
    "stop_loss",
)
_FEE_DECIMAL_FIELDS = ("open_rate", "open_cost", "close_rate", "close_cost", "funding")
_PROFIT_DECIMAL_FIELDS = ("absolute", "ratio")
_ORDER_DECIMAL_FIELDS = ("amount", "price", "cost")
_SUMMARY_DECIMAL_FIELDS = (
    "starting_balance",
    "final_balance",
    "profit_total_abs",
    "total_volume",
)


@lru_cache(maxsize=4)
def _validator(schema_name: str) -> Draft202012Validator:
    schema_resource = files("nfi_backtest_engine.schemas").joinpath(schema_name)
    schema = __import__("json").loads(schema_resource.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_schema(document: Any, schema_name: str) -> None:
    """Raise on the first deterministic JSON Schema error."""
    errors = sorted(
        _validator(schema_name).iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    path = _json_path(error.absolute_path)
    raise SpecValidationError(f"{schema_name} {path}: {error.message}")


def validate_trade_surface(document: Any) -> None:
    """Validate the schema plus canonical decimals and stable sequence fields."""
    if not isinstance(document, dict):
        validate_schema(document, TRADE_SURFACE_SCHEMA)
        return
    version = document.get("schema_version")
    if version == "1.0.0":
        schema = TRADE_SURFACE_SCHEMA
    elif version == "2.0.0":
        schema = TRADE_SURFACE_V2_SCHEMA
    else:
        raise SpecValidationError(f"$.schema_version: unsupported trade surface {version!r}")
    validate_schema(document, schema)
    if version == "2.0.0":
        _check_decimal_fields(document["summary"], _SUMMARY_DECIMAL_FIELDS, "$.summary")
    for trade_index, trade in enumerate(document["trades"]):
        path = f"$.trades[{trade_index}]"
        if trade["sequence"] != trade_index:
            raise SpecValidationError(
                f"{path}.sequence: expected {trade_index}, got {trade['sequence']}"
            )
        _check_decimal_fields(trade, _TRADE_DECIMAL_FIELDS, path)
        _check_decimal_fields(trade["fees"], _FEE_DECIMAL_FIELDS, f"{path}.fees")
        _check_decimal_fields(trade["profit"], _PROFIT_DECIMAL_FIELDS, f"{path}.profit")
        if version == "2.0.0":
            _check_decimal_fields(
                trade,
                (
                    "minimum_rate",
                    "maximum_rate",
                    "initial_stop_loss_ratio",
                    "stop_loss_ratio",
                ),
                path,
            )
        for order_index, order in enumerate(trade["orders"]):
            order_path = f"{path}.orders[{order_index}]"
            if order["sequence"] != order_index:
                raise SpecValidationError(
                    f"{order_path}.sequence: expected {order_index}, got {order['sequence']}"
                )
            _check_decimal_fields(order, _ORDER_DECIMAL_FIELDS, order_path)


def validate_fixture_manifest(document: Any) -> None:
    if not isinstance(document, dict):
        validate_schema(document, BENCHMARK_FIXTURE_SCHEMA)
        return
    version = document.get("schema_version")
    if version == "1.0.0":
        schema = BENCHMARK_FIXTURE_SCHEMA
    elif version == "2.0.0":
        schema = BENCHMARK_FIXTURE_V2_SCHEMA
    else:
        raise SpecValidationError(f"$.schema_version: unsupported fixture version {version!r}")
    validate_schema(document, schema)
    required_phases = set(document["measurement"]["required_profile_phases"])
    expected_phases = {"indicators", "callbacks", "trade_scans", "event_simulation"}
    if required_phases != expected_phases:
        raise SpecValidationError(
            "$.measurement.required_profile_phases: must contain each required phase exactly once"
        )

    if document["evidence_status"] == "captured":
        roles = {item["role"] for item in document["inputs"]}
        missing = {"strategy", "config", "candles"} - roles
        if document["freqtrade"]["trading_mode"] == "futures":
            missing |= {"funding_candles", "mark_candles"} - roles
        if missing:
            joined = ", ".join(sorted(missing))
            raise SpecValidationError(
                f"$.inputs: captured fixture is missing required roles: {joined}"
            )


def _check_decimal_fields(record: dict[str, Any], field_names: Iterable[str], path: str) -> None:
    for field_name in field_names:
        value = record[field_name]
        if value is None:
            continue
        try:
            canonical = canonical_decimal(value, path=f"{path}.{field_name}")
        except NormalizationError as exc:
            raise SpecValidationError(str(exc)) from exc
        if canonical != value:
            raise SpecValidationError(
                f"{path}.{field_name}: decimal is not canonical; expected {canonical!r}"
            )


def _json_path(parts: Iterable[Any]) -> str:
    result = "$"
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result
