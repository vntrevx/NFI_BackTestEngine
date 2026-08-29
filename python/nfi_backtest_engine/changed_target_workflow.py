"""Fail-closed join between changed-target ledger and workflow decisions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .changed_target_ledger import _sha256_json
from .changed_target_proofs import unique_blockers
from .errors import SpecValidationError
from .specs import CHANGED_TARGET_LEDGER_SCHEMA, validate_schema


def validate_changed_target_promotion(
    ledger: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]],
) -> None:
    """Reject any mode whose Native decision outruns its complete target proof."""
    validate_schema(ledger, CHANGED_TARGET_LEDGER_SCHEMA)
    identity = ledger.get("identity")
    targets = ledger.get("targets")
    summary = ledger.get("summary")
    if not isinstance(identity, Mapping) or not isinstance(targets, list) or not isinstance(
        summary, Mapping
    ):
        raise SpecValidationError("changed-target ledger workflow contract is invalid")
    fingerprint_preimage = {key: value for key, value in ledger.items() if key != "fingerprint"}
    if ledger.get("fingerprint") != _sha256_json(fingerprint_preimage):
        raise SpecValidationError("changed-target ledger fingerprint differs")
    target_blocker_groups = [
        target.get("hard_blockers", [])
        for target in targets
        if isinstance(target, Mapping)
    ]
    target_blockers = unique_blockers(
        [item for group in target_blocker_groups for item in group]
    )
    ledger_blockers = ledger.get("hard_blockers", [])
    canonical_ledger_blockers = unique_blockers(ledger_blockers)
    blocked_target_count = sum(bool(group) for group in target_blocker_groups)
    internally_consistent = (
        all(group == unique_blockers(group) for group in target_blocker_groups)
        and ledger_blockers == canonical_ledger_blockers
        and target_blockers == canonical_ledger_blockers
        and all(
            target.get("native_promotion_allowed") is (not bool(target.get("hard_blockers")))
            for target in targets
            if isinstance(target, Mapping)
        )
        and summary.get("target_count") == len(targets)
        and summary.get("blocked_target_count") == blocked_target_count
        and summary.get("hard_blocker_count") == len(target_blockers)
        and summary.get("native_promotion_allowed")
        is (bool(targets) and not target_blockers)
    )
    if not internally_consistent:
        raise SpecValidationError("changed-target ledger is internally inconsistent")
    for mode in ("futures", "spot"):
        decision = decisions.get(mode)
        action = decision.get("action") if isinstance(decision, Mapping) else None
        decision_identity = decision.get("identity") if isinstance(decision, Mapping) else None
        if (
            not isinstance(action, Mapping)
            or not isinstance(decision_identity, Mapping)
            or decision_identity.get("upstream_sha") != identity.get("upstream_head")
            or decision_identity.get("strategy_sha256") != identity.get("new_source_sha256")
            or decision_identity.get("freqtrade_digest") != identity.get("freqtrade_digest")
            or decision_identity.get("semantic_profile_sha256")
            != identity.get("semantic_profile_sha256")
        ):
            raise SpecValidationError(f"{mode} decision and changed-target identities differ")
        unresolved = any(
            isinstance(target, Mapping)
            and mode in target.get("affected_modes", [])
            and target.get("native_promotion_allowed") is not True
            for target in targets
        )
        if action.get("native_promotion_allowed") is True and (
            unresolved or summary.get("native_promotion_allowed") is not True
        ):
            raise SpecValidationError(
                f"{mode} Native promotion contradicts changed-target hard blockers"
            )
