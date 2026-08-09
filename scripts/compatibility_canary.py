#!/usr/bin/env python3
"""Validate one atomic Spot/Futures compatibility check before ledger advancement."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CANARY_SCHEMA_VERSION = "compatibility-hosted-canary-v1"
_MODES = ("spot", "futures")
_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def build_hosted_canary(
    identity: Mapping[str, Any],
    strategy_diff: Mapping[str, Any],
    reports: Mapping[str, Mapping[str, Any]],
    targeted_reports: Mapping[str, Mapping[str, Any]],
    qualifications: Mapping[str, Mapping[str, Any]],
    *,
    semantic_profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a deterministic four-identity, dual-mode completion proof."""

    watcher_identity = _watcher_identity(identity)
    source_sha256 = _sha256(identity.get("source_sha256"), "strategy source")
    profile_sha256 = _sha256(
        semantic_profile.get("fingerprint"),
        "committed semantic profile",
    )
    if watcher_identity["semantic_profile_sha256"] != profile_sha256:
        raise ValueError("compatibility identity differs from the committed semantic profile")
    new_source = strategy_diff.get("new")
    diff_source_sha256 = (
        new_source.get("sha256") if isinstance(new_source, Mapping) else None
    )
    if diff_source_sha256 != source_sha256:
        raise ValueError("strategy difference and compatibility identity source differ")

    mode_results = []
    for mode in _MODES:
        compatibility = _mode_document(reports, mode, "compatibility")
        targeted = _mode_document(targeted_reports, mode, "targeted")
        qualification = _mode_document(qualifications, mode, "qualification")
        _validate_compatibility(compatibility, mode, source_sha256)
        _validate_targeted(targeted, mode, source_sha256, qualification)
        _validate_qualification(qualification, source_sha256)
        mode_results.append(
            {
                "trading_mode": mode,
                "static_check_complete": True,
                "native_compatible": compatibility["native_compatible"],
                "targeted_check_complete": True,
                "verification_state": qualification["verification_state"],
                "quick_verified": qualification["verification_state"] == "quick_verified",
            }
        )

    canary: dict[str, Any] = {
        "schema_version": CANARY_SCHEMA_VERSION,
        "identity": watcher_identity,
        "identity_fingerprint": _canonical_sha256(watcher_identity),
        "modes": mode_results,
        "complete": True,
        "identity_advancement_allowed": True,
    }
    canary["fingerprint"] = _canonical_sha256(canary)
    return canary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--strategy-diff", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--targeted-reports", type=Path, required=True)
    parser.add_argument("--qualifications", type=Path, required=True)
    parser.add_argument("--semantic-profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    canary = build_hosted_canary(
        _read_object(args.identity),
        _read_object(args.strategy_diff),
        {
            mode: _read_object(args.reports / f"report-{mode}.json")
            for mode in _MODES
        },
        {
            mode: _read_object(args.targeted_reports / f"targeted-report-{mode}.json")
            for mode in _MODES
        },
        {
            mode: _read_object(args.qualifications / f"qualification-{mode}.json")
            for mode in _MODES
        },
        semantic_profile=_read_object(args.semantic_profile),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(canary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


def _watcher_identity(identity: Mapping[str, Any]) -> dict[str, str]:
    if identity.get("schema_version") != "1.1.0":
        raise ValueError("compatibility identity schema must be 1.1.0")
    return {
        "nfi_upstream_sha": _sha(identity.get("upstream_sha"), "NFI upstream"),
        "engine_sha": _sha(identity.get("engine_sha"), "engine"),
        "freqtrade_digest": _digest(identity.get("freqtrade_digest"), "Freqtrade"),
        "semantic_profile_sha256": _sha256(
            identity.get("semantic_profile_sha256"),
            "semantic profile",
        ),
    }


def _validate_compatibility(
    report: Mapping[str, Any],
    mode: str,
    source_sha256: str,
) -> None:
    source = report.get("source")
    if report.get("schema_version") != "1.0.0":
        raise ValueError(f"{mode} compatibility report schema is unsupported")
    if report.get("trading_mode") != mode:
        raise ValueError(f"{mode} compatibility report has the wrong trading mode")
    if not isinstance(source, Mapping) or source.get("sha256") != source_sha256:
        raise ValueError(f"{mode} compatibility report has a different source")
    if not isinstance(report.get("native_compatible"), bool):
        raise ValueError(f"{mode} compatibility report has no completed outcome")
    if not isinstance(report.get("blockers"), list):
        raise ValueError(f"{mode} compatibility report blockers must be an array")


def _validate_targeted(
    report: Mapping[str, Any],
    mode: str,
    source_sha256: str,
    qualification: Mapping[str, Any],
) -> None:
    if report.get("schema_version") != "1.0.0":
        raise ValueError(f"{mode} targeted report schema is unsupported")
    if report.get("trading_mode") != mode:
        raise ValueError(f"{mode} targeted report has the wrong trading mode")
    if report.get("source_sha256") != source_sha256:
        raise ValueError(f"{mode} targeted report has a different source")
    if report.get("qualification") != qualification:
        raise ValueError(f"{mode} targeted report and qualification differ")
    if report.get("verification_state") != qualification.get("verification_state"):
        raise ValueError(f"{mode} targeted verification state is inconsistent")
    if not isinstance(report.get("runs"), list):
        raise ValueError(f"{mode} targeted runs must be an array")
    if not isinstance(report.get("blockers"), list):
        raise ValueError(f"{mode} targeted blockers must be an array")
    if not isinstance(report.get("complete"), bool):
        raise ValueError(f"{mode} targeted report has no completed outcome")
    expected_complete = qualification.get("verification_state") == "quick_verified"
    if report["complete"] is not expected_complete:
        raise ValueError(f"{mode} targeted completion flag is inconsistent")


def _validate_qualification(report: Mapping[str, Any], source_sha256: str) -> None:
    if report.get("schema_version") != "1.0.0":
        raise ValueError("qualification schema is unsupported")
    if report.get("strategy_sha256") != source_sha256:
        raise ValueError("qualification has a different strategy source")
    if report.get("verification_state") not in {"latest_checked", "quick_verified"}:
        raise ValueError("qualification has an invalid verification state")
    if not isinstance(report.get("blockers"), list):
        raise ValueError("qualification blockers must be an array")


def _mode_document(
    documents: Mapping[str, Mapping[str, Any]],
    mode: str,
    label: str,
) -> Mapping[str, Any]:
    document = documents.get(mode)
    if not isinstance(document, Mapping):
        raise ValueError(f"{mode} {label} report is missing")
    return document


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document is not an object: {path}")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} SHA must be 40 lowercase hexadecimal characters")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} SHA-256 must be 64 lowercase hexadecimal characters")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} digest must be one canonical sha256 token")
    return value


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
