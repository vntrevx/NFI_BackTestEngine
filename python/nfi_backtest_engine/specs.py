"""Versioned JSON Schema loading and semantic validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from functools import cache
from importlib import import_module
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .canonical import canonical_decimal
from .errors import NormalizationError, SpecValidationError

TRADE_SURFACE_SCHEMA = "trade-surface.schema.json"
TRADE_SURFACE_V2_SCHEMA = "trade-surface-v2.schema.json"
BENCHMARK_FIXTURE_SCHEMA = "benchmark-fixture.schema.json"
BENCHMARK_FIXTURE_V2_SCHEMA = "benchmark-fixture-v2.schema.json"
BENCHMARK_FIXTURE_V3_SCHEMA = "benchmark-fixture-v3.schema.json"
CERTIFICATION_REPORT_SCHEMA = "certification-report-v1.schema.json"
FULL_X7_CERTIFICATION_SCHEMA = "full-x7-certification-v1.schema.json"
FULL_X7_CERTIFICATION_V2_SCHEMA = "full-x7-certification-v2.schema.json"
FULL_X7_COMBINED_RELEASE_SCHEMA = "full-x7-combined-release-v1.schema.json"
FULL_X7_COMBINED_RELEASE_GATE_SCHEMA = "full-x7-combined-release-gate-v1.schema.json"
REGRESSION_CONTRACT_SCHEMA = "regression-contract-v1.schema.json"
VERIFICATION_LEDGER_RECORD_SCHEMA = "verification-ledger-record-v1.schema.json"
CLEAN_AUDIT_SCHEMA = "clean-audit-v1.schema.json"
CLEAN_RESULT_SCHEMA = "clean-result-v1.schema.json"
RELEASE_GATE_SCHEMA = "release-gate-v1.schema.json"
RESULT_VERIFICATION_SCHEMA = "result-verification-v1.schema.json"
RESULT_EVIDENCE_INDEX_SCHEMA = "result-evidence-index-v1.schema.json"
STATE_MACHINE_PROGRAM_SCHEMA = "state-machine-program-v1.schema.json"
STATE_MACHINE_PROGRAM_V2_SCHEMA = "state-machine-program-v2.schema.json"
STATE_MACHINE_PROGRAM_V3_SCHEMA = "state-machine-program-v3.schema.json"
SEMANTIC_INVENTORY_SCHEMA = "semantic-inventory-v1.schema.json"
SEMANTIC_OBLIGATION_REGISTRY_SCHEMA = "semantic-obligation-registry-v1.schema.json"
CHANGED_TARGET_LEDGER_SCHEMA = "changed-target-ledger-v1.schema.json"
SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_ID = (
    "https://github.com/vntrevx/NFI_BackTestEngine/schemas/"
    "semantic-obligation-registry-v1.schema.json"
)
SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_VERSION = "semantic-obligation-registry-v1"
SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_BYTES = 20_881
SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_SHA256 = (
    "b68588db19867595d626f674c3abbd85ef8678ceef5fe214999d53e462406083"
)
_SEMANTIC_REGISTRY_SCHEMA_IDENTITY_CODE = "SEMANTIC_REGISTRY_SCHEMA_IDENTITY"
INDICATOR_INVENTORY_SCHEMA = "indicator-operation-inventory-v1.schema.json"
INDICATOR_PROGRAM_SCHEMA = "indicator-program-v1.schema.json"
SIGNAL_PROGRAM_SCHEMA = "signal-program-v1.schema.json"
TAG_PROGRAM_SCHEMA = "tag-program-v1.schema.json"
FULL_NATIVE_VECTOR_MANIFEST_SCHEMA = "full-native-vector-manifest-v1.schema.json"
STATEFUL_COVERAGE_SCHEMA = "stateful-coverage-v1.schema.json"
FREQTRADE_SEMANTIC_PROFILE_SCHEMA = "freqtrade-semantic-profile-v1.schema.json"
SEMANTIC_OBSERVER_REPORT_SCHEMA = "semantic-observer-report-v1.schema.json"
SCHEDULER_CONTRACT_SCHEMA = "scheduler-contract-v1.schema.json"
SCHEDULER_VERIFICATION_SCHEMA = "scheduler-verification-v1.schema.json"
PORTFOLIO_TRACE_SCHEMA = "portfolio-semantic-trace-v1.schema.json"
PORTFOLIO_VERIFICATION_SCHEMA = "portfolio-semantic-verification-v1.schema.json"
EXECUTION_TRACE_SCHEMA = "execution-semantic-trace-v1.schema.json"
NATIVE_EXECUTION_EVENTS_SCHEMA = "native-execution-events-v1.schema.json"
EXECUTION_BOUNDARY_EVENT_SCHEMA = "execution-boundary-event-v1.schema.json"
EXECUTION_VERIFICATION_SCHEMA = "execution-semantic-verification-v1.schema.json"
EXECUTION_CONTRACT_SCHEMA = "execution-contract-v1.schema.json"
FUTURES_CONTRACT_SCHEMA = "futures-contract-v1.schema.json"
CALLBACK_SOURCE_IR_SCHEMA = "callback-source-ir-v1.schema.json"
NATIVE_SCORECARD_SCHEMA = "native-10-scorecard-v1.schema.json"
NATIVE_SCORE_RAW_EVIDENCE_SCHEMA = "native-score-raw-evidence-v1.schema.json"
NATIVE_SCORE_MACHINE_RECORD_SCHEMA = "native-score-machine-record-v1.schema.json"
NATIVE_SCORE_DOMAIN_EVIDENCE_SCHEMA = "native-score-domain-evidence-v1.schema.json"
RELEASE_PROVENANCE_ENVELOPE_SCHEMA = "release-provenance-envelope-v2.schema.json"
PRODUCT_SUPPORT_CONTRACT_SCHEMA = "product-support-contract-v1.schema.json"

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


def semantic_obligation_registry_schema_identity() -> dict[str, str | int]:
    """Return the registry schema identity sealed independently in executable code."""
    return {
        "$id": SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_ID,
        "schema_version": SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_VERSION,
        "bytes": SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_BYTES,
        "sha256": SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_SHA256,
    }


def _schema_identity_error(reason: str) -> SpecValidationError:
    return SpecValidationError(f"{_SEMANTIC_REGISTRY_SCHEMA_IDENTITY_CODE}: {reason}")


def _semantic_registry_schema_package_locations() -> tuple[Path, ...]:
    package = import_module("nfi_backtest_engine.schemas")
    return tuple(Path(location).absolute() for location in package.__path__)


def _verify_semantic_registry_schema_resource_uniqueness(package: Path) -> None:
    try:
        if package.is_symlink() or not package.is_dir():
            raise _schema_identity_error("trusted schema package is not a regular directory")
        candidates = [
            entry.name
            for entry in package.iterdir()
            if "semantic-obligation-registry-v1.schema" in entry.name
        ]
    except OSError as exc:
        raise _schema_identity_error("trusted schema package contents are unavailable") from exc
    if candidates != [SEMANTIC_OBLIGATION_REGISTRY_SCHEMA]:
        raise _schema_identity_error("trusted schema resource is absent or duplicated")


def _trusted_semantic_registry_schema_bytes() -> bytes:
    try:
        package_locations = _semantic_registry_schema_package_locations()
    except (ImportError, ModuleNotFoundError, OSError) as exc:
        raise _schema_identity_error("trusted schema package is unavailable") from exc
    if len(package_locations) != 1:
        raise _schema_identity_error("trusted schema package location is duplicated")
    _verify_semantic_registry_schema_resource_uniqueness(package_locations[0])
    try:
        schema_resource = files("nfi_backtest_engine.schemas").joinpath(
            SEMANTIC_OBLIGATION_REGISTRY_SCHEMA
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise _schema_identity_error("trusted schema package is unavailable") from exc

    resource_path = schema_resource if isinstance(schema_resource, Path) else None
    if resource_path is not None:
        try:
            if resource_path.is_symlink():
                raise _schema_identity_error("trusted schema must not be a symlink")
            if not resource_path.is_file():
                raise _schema_identity_error("trusted schema is absent or not a regular file")
        except OSError as exc:
            raise _schema_identity_error("trusted schema file identity is unavailable") from exc
    try:
        payload = schema_resource.read_bytes()
    except (FileNotFoundError, IsADirectoryError, OSError) as exc:
        raise _schema_identity_error("trusted schema bytes are unavailable") from exc
    if len(payload) != SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_BYTES:
        raise _schema_identity_error("trusted schema length differs from compiled identity")
    if hashlib.sha256(payload).hexdigest() != SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_SHA256:
        raise _schema_identity_error("trusted schema digest differs from compiled identity")
    return payload


def verify_semantic_obligation_registry_schema_identity() -> dict[str, str | int]:
    """Verify exact packaged schema bytes before trusting a semantic registry."""
    payload = _trusted_semantic_registry_schema_bytes()
    try:
        schema = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover - hash owns it
        raise _schema_identity_error("trusted schema is not canonical JSON") from exc
    if not isinstance(schema, dict):  # pragma: no cover - hash owns it
        raise _schema_identity_error("trusted schema is not a JSON object")
    version = schema.get("properties", {}).get("schema_version", {}).get("const")
    if schema.get("$id") != SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_ID or (
        version != SEMANTIC_OBLIGATION_REGISTRY_SCHEMA_VERSION
    ):
        raise _schema_identity_error("trusted schema ID or version differs from compiled identity")
    return semantic_obligation_registry_schema_identity()


@cache
def _semantic_registry_validator(payload: bytes) -> Draft202012Validator:
    schema = json.loads(payload)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@cache
def _validator(schema_name: str) -> Draft202012Validator:
    schema_resource = files("nfi_backtest_engine.schemas").joinpath(schema_name)
    schema = json.loads(schema_resource.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_schema(document: Any, schema_name: str) -> None:
    """Raise on the first deterministic JSON Schema error."""
    validator = (
        _semantic_registry_validator(_trusted_semantic_registry_schema_bytes())
        if schema_name == SEMANTIC_OBLIGATION_REGISTRY_SCHEMA
        else _validator(schema_name)
    )
    errors = sorted(
        validator.iter_errors(document),
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
    elif version == "3.0.0":
        schema = BENCHMARK_FIXTURE_V3_SCHEMA
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
    if version == "3.0.0":
        strategy = next(item for item in document["inputs"] if item["role"] == "strategy")
        if document["strategy_provenance"]["effective_source_sha256"] != strategy["sha256"]:
            raise SpecValidationError(
                "$.strategy_provenance.effective_source_sha256: "
                "must match the sealed strategy input"
            )


def validate_regression_contract(document: Any) -> None:
    """Validate the immutable regression manifest before touching referenced files."""
    validate_schema(document, REGRESSION_CONTRACT_SCHEMA)


def validate_verification_ledger_record(document: Any) -> None:
    """Validate one append-only verification event."""
    validate_schema(document, VERIFICATION_LEDGER_RECORD_SCHEMA)


def validate_clean_audit(document: Any) -> None:
    """Validate the evidence-aware clean dry-run report."""
    validate_schema(document, CLEAN_AUDIT_SCHEMA)


def validate_clean_result(document: Any) -> None:
    """Validate a durable cleanup application receipt."""
    validate_schema(document, CLEAN_RESULT_SCHEMA)


def validate_release_gate(document: Any) -> None:
    """Validate a candidate/certificate identity gate."""
    validate_schema(document, RELEASE_GATE_SCHEMA)


def validate_combined_release_gate(document: Any) -> None:
    """Validate the final Spot+Futures build-once release gate."""
    validate_schema(document, FULL_X7_COMBINED_RELEASE_GATE_SCHEMA)


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
