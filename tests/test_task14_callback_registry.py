from __future__ import annotations

import copy
import gzip
import hashlib
import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.semantic_registry import validate_semantic_obligation_registry
from nfi_backtest_engine.semantic_registry_callback_contract import (
    CALLBACK_INTERACTIONS,
    REQUIRED_CALLBACKS,
    callback_semantic_contract_rows,
    validate_callback_semantic_contract_rows,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = (
    ROOT
    / "python/nfi_backtest_engine/contracts/"
    "freqtrade-nfi-semantic-obligation-registry.json.gz"
)
MANIFEST = PACKAGED.with_name(
    "freqtrade-nfi-semantic-obligation-registry.manifest.json"
)


@cache
def _registry() -> dict[str, Any]:
    with gzip.open(PACKAGED, "rb") as payload:
        return json.load(payload)

def test_complete_callback_interaction_inventory_is_exact_and_non_duplicate() -> None:
    registry = _registry()
    rows = callback_semantic_contract_rows(registry["freqtrade"])

    validate_callback_semantic_contract_rows(rows, registry["freqtrade"])
    assert {row["callback"] for row in rows} == set(REQUIRED_CALLBACKS)
    assert {row["interaction"] for row in rows} == set(CALLBACK_INTERACTIONS)
    assert len(rows) == len({row["boundary_row"] for row in rows})
    assert len(rows) == len(
        {
            (row["callback"], row["interaction"], row["rule"])
            for row in rows
        }
    )
    assert all(row["fixture_requirement"] for row in rows)
    assert all(row["source_sha256"] for row in rows)


@pytest.mark.parametrize(
    ("interaction", "field", "replacement"),
    [
        ("order", "rule", "mutated-order-edge"),
        ("predicate", "rule", "mutated-source-predicate"),
        ("return", "rule", "mutated-return-class"),
        ("rollback", "rule", "mutated-rollback-action"),
        ("state-delta", "rule", "mutated-custom-state-delta"),
        ("visibility", "rule", "mutated-visibility-edge"),
        ("predicate", "source_sha256", "0" * 64),
    ],
)
def test_single_dimension_mutants_fail_closed_and_remain_boundary_mapped(
    interaction: str,
    field: str,
    replacement: str,
) -> None:
    registry = _registry()
    rows = [dict(row) for row in callback_semantic_contract_rows(registry["freqtrade"])]
    index = next(i for i, row in enumerate(rows) if row["interaction"] == interaction)
    original = rows[index]
    rows[index][field] = replacement

    assert original["boundary_row"]
    assert original["fixture_requirement"]
    with pytest.raises(SpecValidationError, match="CALLBACK_SEMANTIC_CONTRACT"):
        validate_callback_semantic_contract_rows(rows, registry["freqtrade"])


def test_unknown_row_shape_and_duplicate_boundary_fail_closed() -> None:
    registry = _registry()
    rows = [dict(row) for row in callback_semantic_contract_rows(registry["freqtrade"])]
    malformed: list[dict[str, Any]] = copy.deepcopy(rows)
    malformed[0]["unknown"] = True
    with pytest.raises(SpecValidationError, match="CALLBACK_SEMANTIC_CONTRACT"):
        validate_callback_semantic_contract_rows(malformed, registry["freqtrade"])

    duplicate = copy.deepcopy(rows)
    duplicate[1]["boundary_row"] = duplicate[0]["boundary_row"]
    with pytest.raises(SpecValidationError, match="CALLBACK_SEMANTIC_CONTRACT"):
        validate_callback_semantic_contract_rows(duplicate, registry["freqtrade"])


def test_registry_validation_detects_each_contract_obligation_mutation() -> None:
    registry = _registry()
    contract_records = [
        record
        for group in registry["obligation_groups"]
        for record in group["obligations"]
        if record["preimage"]["normalized_semantics"][0].startswith(
            "callback-contract:"
        )
    ]
    assert contract_records

    for record in contract_records[:6]:
        mutant = copy.deepcopy(registry)
        target = next(
            candidate
            for group in mutant["obligation_groups"]
            for candidate in group["obligations"]
            if candidate["obligation_id"] == record["obligation_id"]
        )
        target["preimage"]["normalized_semantics"][1] = "f" * 64
        with pytest.raises(SpecValidationError):
            validate_semantic_obligation_registry(mutant)


def test_packaged_registry_is_deterministic_and_self_authenticating() -> None:
    compressed = PACKAGED.read_bytes()
    tracked = gzip.decompress(compressed)
    manifest = json.loads(MANIFEST.read_bytes())

    assert manifest["compressed_sha256"] == hashlib.sha256(compressed).hexdigest()
    assert manifest["uncompressed_sha256"] == hashlib.sha256(tracked).hexdigest()
    assert manifest["uncompressed_bytes"] == len(tracked)
    assert manifest["registry_fingerprint"] == _registry()["fingerprint"]
