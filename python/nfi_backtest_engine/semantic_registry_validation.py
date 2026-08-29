"""Fast exact validation for the semantic registry's linear record family."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import SpecValidationError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBLIGATION_ID = re.compile(r"^obl-[a-z0-9-]+-[0-9a-f]{64}$")
_SOURCE_PATH = re.compile(r"^(@strategy|@semantic-contract|@source/.+)$")


def semantic_registry_schema_projection(document: Any) -> Any:
    """Retain one schema-checked record per group; the fast pass checks every record."""
    if not isinstance(document, Mapping):
        return document
    groups = document.get("obligation_groups")
    if not isinstance(groups, list) or not groups:
        return document
    projected_groups: list[Any] = []
    for group in groups:
        if not isinstance(group, Mapping):
            return document
        obligations = group.get("obligations")
        if not isinstance(obligations, list) or not obligations:
            return document
        projected_groups.append({**group, "obligations": obligations[:1]})
    return {**document, "obligation_groups": projected_groups}


def validate_semantic_registry_records(groups: Sequence[Mapping[str, Any]]) -> None:
    """Apply the trusted schema's exact record constraints in one linear pass."""
    for group in groups:
        obligations = group["obligations"]
        for record in obligations:
            _validate_record(record)


def _validate_record(record: Any) -> None:
    if not isinstance(record, Mapping) or set(record) != {"obligation_id", "preimage"}:
        raise SpecValidationError("semantic obligation record fields differ")
    obligation_id = record["obligation_id"]
    if not isinstance(obligation_id, str) or _OBLIGATION_ID.fullmatch(obligation_id) is None:
        raise SpecValidationError("semantic obligation ID is invalid")
    preimage = record["preimage"]
    if not isinstance(preimage, Mapping) or set(preimage) != {
        "source",
        "normalized_semantics",
    }:
        raise SpecValidationError("semantic obligation preimage fields differ")
    _validate_source(preimage["source"])
    normalized = preimage["normalized_semantics"]
    if not isinstance(normalized, list) or len(normalized) != 2:
        raise SpecValidationError("semantic obligation normalized preimage is invalid")
    subject, semantic_sha256 = normalized
    if not isinstance(subject, str) or not subject:
        raise SpecValidationError("semantic obligation subject is empty")
    if not isinstance(semantic_sha256, str) or _SHA256.fullmatch(semantic_sha256) is None:
        raise SpecValidationError("semantic obligation semantic hash is invalid")


def _validate_source(source: Any) -> None:
    if not isinstance(source, list) or len(source) != 3:
        raise SpecValidationError("semantic obligation preimage source is invalid")
    identity_sha256, path, span = source
    if not isinstance(identity_sha256, str) or _SHA256.fullmatch(identity_sha256) is None:
        raise SpecValidationError("semantic obligation preimage source identity is invalid")
    if not isinstance(path, str) or _SOURCE_PATH.fullmatch(path) is None:
        raise SpecValidationError("semantic obligation preimage source path is invalid")
    if span is None:
        return
    if not isinstance(span, list) or len(span) != 4:
        raise SpecValidationError("semantic obligation preimage source span is invalid")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()))
        for value in span
    ):
        raise SpecValidationError("semantic obligation preimage source span is invalid")
    line, column, end_line, end_column = span
    if line < 1 or column < 0 or end_line < 1 or end_column < 0:
        raise SpecValidationError("semantic obligation preimage source span is invalid")
