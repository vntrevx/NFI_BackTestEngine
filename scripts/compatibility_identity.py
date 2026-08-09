#!/usr/bin/env python3
"""Decide whether one complete Native compatibility identity needs checking."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def decide_run(
    *,
    upstream_sha: str,
    engine_sha: str,
    freqtrade_digest: str,
    semantic_profile_sha256: str,
    previous_upstream_sha: str = "",
    previous_engine_sha: str = "",
    previous_freqtrade_digest: str = "",
    previous_semantic_profile_sha256: str = "",
    force: bool = False,
) -> dict[str, str | bool]:
    _validate_sha(upstream_sha, "upstream")
    _validate_sha(engine_sha, "engine")
    _validate_digest(freqtrade_digest, "Freqtrade")
    _validate_sha256(semantic_profile_sha256, "semantic profile")
    if previous_upstream_sha:
        _validate_sha(previous_upstream_sha, "previous upstream")
    if previous_engine_sha:
        _validate_sha(previous_engine_sha, "previous engine")
    if previous_freqtrade_digest:
        _validate_digest(previous_freqtrade_digest, "previous Freqtrade")
    if previous_semantic_profile_sha256:
        _validate_sha256(
            previous_semantic_profile_sha256,
            "previous semantic profile",
        )
    if force:
        changed, reason = True, "manual-force"
    elif upstream_sha != previous_upstream_sha:
        changed, reason = True, "upstream-changed"
    elif engine_sha != previous_engine_sha:
        changed, reason = True, "engine-changed"
    elif freqtrade_digest != previous_freqtrade_digest:
        changed, reason = True, "freqtrade-changed"
    elif semantic_profile_sha256 != previous_semantic_profile_sha256:
        changed, reason = True, "semantic-profile-changed"
    else:
        changed, reason = False, "unchanged"
    return {
        "changed": changed,
        "reason": reason,
        "previous_sha": previous_upstream_sha,
        "previous_engine_sha": previous_engine_sha,
        "upstream_sha": upstream_sha,
        "engine_sha": engine_sha,
        "freqtrade_digest": freqtrade_digest,
        "previous_freqtrade_digest": previous_freqtrade_digest,
        "semantic_profile_sha256": semantic_profile_sha256,
        "previous_semantic_profile_sha256": previous_semantic_profile_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-sha", required=True)
    parser.add_argument("--engine-sha", required=True)
    parser.add_argument("--freqtrade-digest", required=True)
    parser.add_argument("--semantic-profile-sha256", required=True)
    parser.add_argument("--previous-upstream-sha", default="")
    parser.add_argument("--previous-engine-sha", default="")
    parser.add_argument("--previous-freqtrade-digest", default="")
    parser.add_argument("--previous-semantic-profile-sha256", default="")
    parser.add_argument("--previous-check-path", default="")
    parser.add_argument("--force", choices=("true", "false"), required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    parser.add_argument("--github-summary", type=Path, required=True)
    args = parser.parse_args()
    decision = decide_run(
        upstream_sha=args.upstream_sha,
        engine_sha=args.engine_sha,
        freqtrade_digest=args.freqtrade_digest,
        semantic_profile_sha256=args.semantic_profile_sha256,
        previous_upstream_sha=args.previous_upstream_sha,
        previous_engine_sha=args.previous_engine_sha,
        previous_freqtrade_digest=args.previous_freqtrade_digest,
        previous_semantic_profile_sha256=args.previous_semantic_profile_sha256,
        force=args.force == "true",
    )
    output = {
        **decision,
        "previous_check_path": args.previous_check_path,
    }
    lines = [
        f"{key}={str(value).lower() if isinstance(value, bool) else value}"
        for key, value in output.items()
    ]
    with args.github_output.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    with args.github_summary.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n".join(
                f"{key}={value}"
                for key, value in decision.items()
                if key
                in {
                    "changed",
                    "reason",
                    "upstream_sha",
                    "engine_sha",
                    "freqtrade_digest",
                    "semantic_profile_sha256",
                }
            )
            + "\n"
        )
    return 0


def _validate_sha(value: str, label: str) -> None:
    if _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} SHA must be 40 lowercase hexadecimal characters")


def _validate_digest(value: str, label: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} digest must be one canonical sha256 token")


def _validate_sha256(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} SHA-256 must be 64 lowercase hexadecimal characters")


if __name__ == "__main__":
    raise SystemExit(main())
