"""Static ownership and transitive mode reachability for changed targets."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Final

from .changed_target_models import MODES

_FUTURES_TERMS: Final = frozenset(
    {"funding", "leverage", "liquidation", "short", "shorting"}
)


def ownership_index(
    registry: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Flatten grouped registry ownership while retaining duplicate identities."""
    records: list[dict[str, Any]] = []
    identifiers: list[str] = []
    preimages: list[str] = []
    for group in registry.get("obligation_groups", []):
        if not isinstance(group, Mapping):
            continue
        for record in group.get("obligations", []):
            if not isinstance(record, Mapping):
                continue
            identifier = str(record.get("obligation_id", ""))
            identifiers.append(identifier)
            preimage = record.get("preimage")
            canonical_preimage = json.dumps(
                preimage,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            preimages.append(canonical_preimage)
            records.append(
                {
                    "id": identifier,
                    "owner": str(group.get("semantic_owner", "")),
                    "mapping": str(group.get("mapping", "")),
                    "reachability": str(group.get("reachability", "")),
                    "preimage": preimage,
                    "canonical_preimage": canonical_preimage,
                }
            )
    duplicate_ids = {
        identifier for identifier, count in Counter(identifiers).items() if count > 1
    }
    duplicate_preimages = {
        preimage for preimage, count in Counter(preimages).items() if count > 1
    }
    duplicates = duplicate_ids | duplicate_preimages
    return records, duplicates


def target_ownership(
    target: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    duplicate_identities: set[str],
) -> dict[str, Any]:
    """Derive one target's owner summary from source spans and semantic subjects."""
    spans = _target_spans(target)
    tokens = {
        str(target.get("value", "")),
        *(str(value) for value in target.get("methods", [])),
    }
    matched = [record for record in records if _owned(record, spans, tokens)]
    ids = sorted({str(record["id"]) for record in matched})
    mappings = sorted({str(record["mapping"]) for record in matched})
    owners = sorted({str(record["owner"]) for record in matched})
    return {
        "mapping": mappings[0] if len(mappings) == 1 else None,
        "semantic_owners": owners,
        "obligation_count": len(ids),
        "obligation_ids_sha256": _sha256_json(ids),
        "duplicate_obligation_count": sum(
            str(record["id"]) in duplicate_identities
            or str(record["canonical_preimage"]) in duplicate_identities
            for record in matched
        ),
        "reachable": bool(matched)
        and all(record["reachability"] == "reachable" for record in matched),
    }


def affected_modes(target: Mapping[str, Any]) -> list[str]:
    """Return every runtime mode that can reach one semantic target."""
    semantics = " ".join(
        [
            str(target.get("value", "")),
            *(str(value) for value in target.get("methods", [])),
            *(str(value) for value in target.get("semantic_callers", [])),
            *(str(value) for value in target.get("tags", [])),
        ]
    ).lower()
    return ["futures"] if any(term in semantics for term in _FUTURES_TERMS) else list(MODES)


def _target_spans(target: Mapping[str, Any]) -> list[tuple[int, int]]:
    proof = target.get("proof")
    raw = []
    if isinstance(proof, Mapping):
        raw = proof.get("changed_source_spans") or proof.get("new_source_spans", [])
    return [
        (int(span["line"]), int(span["end_line"]))
        for span in raw
        if isinstance(span, Mapping)
        and isinstance(span.get("line"), int)
        and isinstance(span.get("end_line"), int)
    ]


def _owned(
    record: Mapping[str, Any],
    spans: Sequence[tuple[int, int]],
    tokens: set[str],
) -> bool:
    preimage = record.get("preimage")
    if not isinstance(preimage, Mapping):
        return False
    source = preimage.get("source")
    normalized = preimage.get("normalized_semantics")
    subject = str(normalized[0]) if isinstance(normalized, list) and normalized else ""
    source_span = source[2] if isinstance(source, list) and len(source) == 3 else None
    span_match = isinstance(source_span, list) and len(source_span) == 4 and any(
        start <= int(source_span[0]) <= end for start, end in spans
    )
    return span_match or any(token and token in subject for token in tokens)


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
