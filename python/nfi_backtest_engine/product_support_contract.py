"""Machine-readable product scope and 10/10 release-target validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import read_json
from .errors import SpecValidationError
from .product_contract import (
    CERTIFICATION_SPREAD_THRESHOLD,
    FULL_X7_RELEASE_TIMEFRAMES,
    MAX_CERTIFICATION_REPETITIONS,
    MIN_CERTIFICATION_REPETITIONS,
    MIN_RELEASE_BACKTEST_DAYS,
    MIN_RELEASE_PAIR_COUNT,
    TARGET_SCREENING_SPEEDUP,
)
from .specs import PRODUCT_SUPPORT_CONTRACT_SCHEMA, validate_schema

PRODUCT_SUPPORT_CONTRACT_VERSION = "1.0.0"
PRODUCT_SUPPORT_CONTRACT_PATH = Path("planning/product-support-contract.json")


def load_product_support_contract(
    path: str | Path = PRODUCT_SUPPORT_CONTRACT_PATH,
) -> dict[str, Any]:
    """Load and semantically validate the authoritative product support contract."""
    document = read_json(Path(path))
    if not isinstance(document, dict):
        raise SpecValidationError("product support contract must be an object")
    validate_schema(document, PRODUCT_SUPPORT_CONTRACT_SCHEMA)
    _validate_product_policy(document)
    return document


def _validate_product_policy(document: dict[str, Any]) -> None:
    native = document["strategies"]["native_supported"]
    if [item["family"] for item in native] != ["NostalgiaForInfinityX7"]:
        raise SpecValidationError("product Native strategy scope must contain X7 only")

    legacy = document["strategies"]["official_only_legacy"]
    if [(item["family"], item["generation"]) for item in legacy] != [
        ("NostalgiaForInfinityNext", "V8"),
        ("NostalgiaForInfinityNextGen", "V9"),
    ]:
        raise SpecValidationError("product legacy strategy scope differs")

    modes = {(item["slug"], item["contract"], item["margin_mode"]) for item in document["modes"]}
    if modes != {
        ("spot", "binance-spot", "none"),
        ("futures", "binance-usdtm-isolated", "isolated"),
    }:
        raise SpecValidationError("product trading-mode scope differs")

    expected_platforms = [
        "linux-x86_64",
        "linux-aarch64",
        "macos-arm64",
        "windows-wsl2-x86_64",
    ]
    if document["platforms"]["supported"] != expected_platforms:
        raise SpecValidationError("product platform scope differs")

    certification = document["certification"]
    expected_certification = {
        "pair_count_per_mode": MIN_RELEASE_PAIR_COUNT,
        "minimum_days": MIN_RELEASE_BACKTEST_DAYS,
        "timeframes": list(FULL_X7_RELEASE_TIMEFRAMES),
        "minimum_native_repetitions": MIN_CERTIFICATION_REPETITIONS,
        "maximum_native_repetitions": MAX_CERTIFICATION_REPETITIONS,
        "adaptive_spread_threshold": CERTIFICATION_SPREAD_THRESHOLD,
        "minimum_native_speedup": TARGET_SCREENING_SPEEDUP,
    }
    differing = [
        field
        for field, expected in expected_certification.items()
        if certification[field] != expected
    ]
    if differing:
        raise SpecValidationError(
            "product certification policy differs from executable defaults: "
            + ", ".join(differing)
        )

    releases = document["release_train"]
    if [item["version"] for item in releases] != [
        "v1.12.0",
        "v1.13.0",
        "v1.14.0",
        "v1.15.0",
    ]:
        raise SpecValidationError("product release train differs")
    combined = [item["version"] for item in releases if item["combined_full_x7_certified"]]
    if combined != ["v1.15.0"]:
        raise SpecValidationError("only the gated v1.15.0 target may claim combined certification")


__all__ = [
    "PRODUCT_SUPPORT_CONTRACT_PATH",
    "PRODUCT_SUPPORT_CONTRACT_VERSION",
    "load_product_support_contract",
]
