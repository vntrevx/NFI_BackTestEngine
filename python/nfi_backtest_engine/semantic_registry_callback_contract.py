"""Generate and validate the closed callback-interaction obligation matrix."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .errors import SpecValidationError
from .semantic_registry_callback_data import (
    CALLBACK_INTERACTIONS,
    CALLBACK_SPECS,
    FREQTRADE_COMMIT,
    FREQTRADE_METHOD_MERKLE,
    FREQTRADE_VERSION,
    REQUIRED_CALLBACKS,
    ROW_FIELDS,
    SOURCE_METHODS,
)

__all__ = [
    "CALLBACK_INTERACTIONS",
    "REQUIRED_CALLBACKS",
    "callback_semantic_contract_rows",
    "validate_callback_semantic_contract_rows",
]


def _source_methods(freqtrade: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    try:
        reference = freqtrade["reference"]
        source = freqtrade["source"]
    except (KeyError, TypeError):
        raise SpecValidationError(
            "CALLBACK_SEMANTIC_CONTRACT: Freqtrade identity shape differs"
        ) from None
    if (
        not isinstance(reference, Mapping)
        or not isinstance(source, Mapping)
        or reference.get("version") != FREQTRADE_VERSION
        or source.get("commit") != FREQTRADE_COMMIT
        or source.get("observed_method_count") != 15
        or source.get("observed_method_merkle_root") != FREQTRADE_METHOD_MERKLE
    ):
        raise SpecValidationError(
            "CALLBACK_SEMANTIC_CONTRACT: Freqtrade source identity differs"
        )
    return dict(SOURCE_METHODS)


def _expected_rows(freqtrade: Mapping[str, Any]) -> list[dict[str, str]]:
    methods = _source_methods(freqtrade)
    expected: list[dict[str, str]] = []
    for callback, owner, method, rules in CALLBACK_SPECS:
        digest = methods.get((owner, method))
        if digest is None:
            raise SpecValidationError(
                f"CALLBACK_SEMANTIC_CONTRACT: source method {owner}.{method} is absent"
            )
        for interaction, rule in zip(CALLBACK_INTERACTIONS, rules, strict=True):
            boundary = f"CB-{callback}-{interaction}"
            expected.append(
                {
                    "callback": callback,
                    "interaction": interaction,
                    "rule": rule,
                    "source_owner": owner,
                    "source_method": method,
                    "source_sha256": digest,
                    "boundary_row": boundary,
                    "fixture_requirement": f"official-callback-trace:{boundary}",
                }
            )
    return expected


def callback_semantic_contract_rows(freqtrade: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Return the canonical exhaustive callback matrix for one authenticated source."""
    result = tuple(_expected_rows(freqtrade))
    validate_callback_semantic_contract_rows(result, freqtrade)
    return result


def validate_callback_semantic_contract_rows(
    rows: Sequence[Mapping[str, Any]],
    freqtrade: Mapping[str, Any],
) -> None:
    """Reject missing, changed, duplicated, reordered, or open-world contract rows."""
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != ROW_FIELDS:
            raise SpecValidationError(
                "CALLBACK_SEMANTIC_CONTRACT: callback row fields differ"
            )
        normalized.append(dict(row))
    boundaries = [row["boundary_row"] for row in normalized]
    identities = [
        (row["callback"], row["interaction"], row["rule"])
        for row in normalized
    ]
    if len(boundaries) != len(set(boundaries)) or len(identities) != len(set(identities)):
        raise SpecValidationError(
            "CALLBACK_SEMANTIC_CONTRACT: duplicate callback boundary row"
        )
    if normalized != _expected_rows(freqtrade):
        raise SpecValidationError(
            "CALLBACK_SEMANTIC_CONTRACT: callback obligations differ from canonical matrix"
        )
