from __future__ import annotations

from pathlib import Path

from nfi_backtest_engine.hash_index import FileHashIndex


def test_file_hash_index_reuses_and_invalidates_metadata(tmp_path: Path) -> None:
    source = tmp_path / "candles.feather"
    source.write_bytes(b"first")
    with FileHashIndex(tmp_path / "hashes.sqlite") as index:
        first = index.hash_file(source)
        assert index.hash_file(source) == first
        source.write_bytes(b"second")
        second = index.hash_file(source)

    assert second != first
