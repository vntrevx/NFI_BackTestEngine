"""Local consistency checks for one exact compatibility decision."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

_AUTOMATION_VERSION: Final = "1.0.0"


def is_valid_exact_decision(
    decision: Mapping[str, Any],
    mode: str,
    identity: Mapping[str, str],
) -> bool:
    """Reject an exact decision whose fields or fingerprints contradict."""
    verification = decision.get("verification")
    action = decision.get("action")
    targets = decision.get("behavior_targets")
    if (
        decision.get("schema_version") != _AUTOMATION_VERSION
        or decision.get("identity") != dict(identity)
        or decision.get("trading_mode") != mode
        or decision.get("automation_route") != "native_exact"
        or decision.get("execution_route") != "native"
        or decision.get("blockers") != []
        or not isinstance(verification, Mapping)
        or action
        != {
            "native_promotion_allowed": True,
            "bounded_discovery_required": False,
            "draft_pr_allowed": False,
            "draft_pr_kind": None,
            "issue_required": False,
            "official_fallback_available": True,
            "automatic_semantic_merge_allowed": False,
            "external_data_deferred_is_exact": False,
        }
        or not isinstance(targets, list)
        or not all(isinstance(target, Mapping) for target in targets)
    ):
        return False
    if (
        verification.get("state") != "quick_verified"
        or verification.get("changed_branch_reached") is not True
        or verification.get("trade_surface_exact") is not True
        or verification.get("full_state_exact") is not True
        or verification.get("exact") is not True
    ):
        return False
    action_fingerprint = _canonical_sha256(
        {
            "trading_mode": mode,
            "route": "native_exact",
            "review_kind": decision.get("review_kind"),
            "classification": decision.get("strategy_classification"),
            "added_opcodes": decision.get("added_opcodes"),
            "target_ids": [target.get("id") for target in targets],
            "blockers": [],
            "discovery_fingerprint": None,
        }
    )
    return (
        decision.get("action_fingerprint") == action_fingerprint
        and decision.get("decision_fingerprint")
        == _canonical_sha256(
            {
                "identity": dict(identity),
                "action_fingerprint": action_fingerprint,
                "verification_state": "quick_verified",
                "discovery_status": decision.get("discovery_status"),
            }
        )
    )


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
