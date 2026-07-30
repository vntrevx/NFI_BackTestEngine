from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "resolve_upstream_source",
    ROOT / "scripts" / "resolve_upstream_source.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_resolver_selects_content_identity_without_commit_hardcoding() -> None:
    sources = {
        "a" * 40: b"latest",
        "b" * 40: b"previous",
        "c" * 40: b"older",
    }

    commit, source = MODULE.find_source_commit(
        sources,
        expected_sha256=hashlib.sha256(b"previous").hexdigest(),
        fetch_source=sources.__getitem__,
    )

    assert commit == "b" * 40
    assert source == b"previous"


def test_resolver_fails_closed_when_bounded_history_has_no_digest() -> None:
    with pytest.raises(ValueError, match="not found"):
        MODULE.find_source_commit(
            ["a" * 40],
            expected_sha256=hashlib.sha256(b"missing").hexdigest(),
            fetch_source=lambda _commit: b"latest",
        )


@pytest.mark.parametrize(
    ("repository", "source_path", "max_commits"),
    [
        ("invalid", "NostalgiaForInfinityX7.py", 100),
        ("owner/..", "NostalgiaForInfinityX7.py", 100),
        ("owner/repository", "../strategy.py", 100),
        ("owner/repository", "strategy.py", 0),
        ("owner/repository", "strategy.py", 501),
    ],
)
def test_resolver_rejects_unbounded_or_unsafe_requests(
    repository: str,
    source_path: str,
    max_commits: int,
) -> None:
    with pytest.raises(ValueError):
        MODULE._validate_request(
            repository=repository,
            head_sha="a" * 40,
            source_path=source_path,
            expected_sha256="b" * 64,
            max_commits=max_commits,
        )
