from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest
from nfi_backtest_engine import cache as cache_module
from nfi_backtest_engine.cache import ContentCache, cache_key
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.vector_runtime import _cacheable_vector_record


def _publish_same_bytes(root: str, key: str) -> bytes:
    return ContentCache(root).put_bytes(key, b"shared").read_bytes()


def test_content_cache_is_immutable_and_prunes_oldest(tmp_path: Path) -> None:
    cache = ContentCache(tmp_path / "cache", max_bytes=5)
    first_key = cache_key("data", {"pair": "BTC/USDT"})
    second_key = cache_key("data", {"pair": "ETH/USDT"})

    cache.put_bytes(first_key, b"123")
    cache.put_bytes(second_key, b"456")

    assert cache.get(first_key) is None
    assert cache.get(second_key).read_bytes() == b"456"


def test_same_key_publication_is_process_safe(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    key = cache_key("vectors", {"pair": "APE/USDT"})
    with ProcessPoolExecutor(
        max_workers=4,
        mp_context=get_context("spawn"),
        max_tasks_per_child=1,
    ) as executor:
        results = list(executor.map(_publish_same_bytes, [str(root)] * 4, [key] * 4))

    assert results == [b"shared"] * 4
    assert ContentCache(root).get(key).read_bytes() == b"shared"


def test_vector_cache_metadata_excludes_process_telemetry() -> None:
    record = {
        "pair": "APE/USDT:USDT",
        "sha256": "a" * 64,
        "wall_time_seconds": 2.5,
        "resident_bytes_at_completion": 512 * 1024**2,
    }

    assert _cacheable_vector_record(record) == {
        "pair": "APE/USDT:USDT",
        "sha256": "a" * 64,
    }


def test_file_publication_reuses_the_worker_hash_and_same_volume_inode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "vector.feather"
    source.write_bytes(b"immutable vector")
    digest = sha256_file(source)

    def unexpected_hash(_path: Path) -> str:
        raise AssertionError("a worker-hashed same-volume vector must not be rehashed")

    monkeypatch.setattr(cache_module, "sha256_file", unexpected_hash)
    cache = ContentCache(tmp_path / "cache")
    payload = cache.put_file(
        cache_key("vectors", {"pair": "APE/USDT"}),
        source,
        expected_sha256=digest,
    )

    assert payload.read_bytes() == source.read_bytes()
    assert payload.stat().st_ino == source.stat().st_ino


def test_cache_pruning_can_be_deferred_to_the_end_of_a_batch(tmp_path: Path) -> None:
    cache = ContentCache(tmp_path / "cache", max_bytes=5)
    first_key = cache_key("data", {"batch": 1})
    second_key = cache_key("data", {"batch": 2})

    cache.put_bytes(first_key, b"1234", prune=False)
    cache.put_bytes(second_key, b"5678", prune=False)

    assert cache.get(first_key) is not None
    assert cache.get(second_key) is not None
    cache.prune()
    assert sum(cache.get(key) is not None for key in (first_key, second_key)) == 1


def test_cache_rejects_a_noncanonical_worker_digest(tmp_path: Path) -> None:
    source = tmp_path / "vector.feather"
    source.write_bytes(b"vector")

    with pytest.raises(SpecValidationError, match="64 lowercase hexadecimal"):
        ContentCache(tmp_path / "cache").put_file(
            cache_key("vectors", {"pair": "APE/USDT"}),
            source,
            expected_sha256="not-a-digest",
        )
