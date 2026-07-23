"""Bounded-memory storage used by the pinned official Freqtrade reference.

The reference tracer keeps Freqtrade's strategy, callback, order, protection,
and trade-loop implementations intact.  This module changes only two container
representations which otherwise duplicate a multi-year analyzed dataset:

* hot-loop rows are exposed through a block-cached sequence instead of one
  Python ``list`` object per candle;
* analyzed DataProvider frames are written to Arrow IPC record batches and
  only the callback-visible window is read back.

The module is intentionally standalone.  It is mounted beside
``sitecustomize.py`` in the pinned Freqtrade container and has no dependency on
the native NFI engine.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from collections import OrderedDict
from collections.abc import Iterator, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DATASTORE_MODE_ENV = "NFI_REFERENCE_DATASTORE"
DATASTORE_SPOOLED = "spooled"
STORAGE_REPORT_ENV = "NFI_REFERENCE_STORAGE_REPORT"
SPOOL_DIRECTORY_ENV = "NFI_REFERENCE_SPOOL_DIRECTORY"
CACHE_BYTES_ENV = "NFI_REFERENCE_CACHE_BYTES"

# A callback cache is an acceleration layer, not another copy of the dataset.
# Five percent leaves the official trade loop most of the cgroup allocation
# while retaining several Arrow batches for active trades.
CALLBACK_CACHE_MEMORY_FRACTION = 0.05


class ColumnarRows(Sequence[list[Any]]):
    """Expose Freqtrade hot-loop rows without materializing every Python row.

    Columns are detached from the large strategy dataframe once.  A small
    pandas block is converted with the same ``values.tolist()`` operation used
    by pinned Freqtrade, preserving scalar types while bounding Python-object
    residency.
    """

    def __init__(
        self,
        columns: dict[str, Any],
        headers: Sequence[str],
        *,
        block_rows: int,
    ) -> None:
        if block_rows <= 0:
            raise ValueError("columnar row block size must be positive")
        self._headers = tuple(headers)
        self._columns = tuple(columns[name] for name in self._headers)
        lengths = {len(column) for column in self._columns}
        if len(lengths) > 1:
            raise ValueError("columnar row columns have different lengths")
        self._length = lengths.pop() if lengths else 0
        self._block_rows = block_rows
        self._cached_start = 0
        self._cached_stop = 0
        self._cached_rows: list[list[Any]] = []

    @classmethod
    def from_dataframe(
        cls,
        dataframe: Any,
        headers: Sequence[str],
        *,
        block_rows: int,
    ) -> ColumnarRows:
        """Detach only the official hot-loop columns from an analyzed frame."""
        columns = {
            name: dataframe.loc[:, name].array.copy()
            for name in headers
        }
        return cls(columns, headers, block_rows=block_rows)

    def __len__(self) -> int:
        return self._length

    def __getitem__(self, index: int | slice) -> list[Any] | list[list[Any]]:
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(self._length))]
        if index < 0:
            index += self._length
        if index < 0 or index >= self._length:
            raise IndexError("columnar row index out of range")
        if not self._cached_start <= index < self._cached_stop:
            self._load_block(index)
        return self._cached_rows[index - self._cached_start]

    def __iter__(self) -> Iterator[list[Any]]:
        for index in range(self._length):
            yield self[index]

    def _load_block(self, index: int) -> None:
        import pandas as pd

        start = (index // self._block_rows) * self._block_rows
        stop = min(self._length, start + self._block_rows)
        block = pd.DataFrame(
            {
                name: column[start:stop]
                for name, column in zip(self._headers, self._columns, strict=True)
            },
            copy=False,
        )
        # This is deliberately identical to the final scalar conversion in
        # Freqtrade 2026.5.1's Backtesting._get_ohlcv_as_lists.
        self._cached_rows = block.loc[:, list(self._headers)].values.tolist()
        self._cached_start = start
        self._cached_stop = stop


@dataclass(frozen=True)
class _SpoolRecord:
    path: Path
    row_count: int
    column_count: int
    refreshed_at: datetime
    range_index: dict[str, Any] | None


@dataclass
class _CachedWindow:
    start: int
    stop: int
    dataframe: Any
    bytes: int


class SpooledFrames(MutableMapping[str, Any]):
    """Mutable mapping whose full indicator frames live in Arrow files.

    The official strategy method still executes every pair in the same order.
    Only its result mapping changes representation.  Date columns stay in
    memory because Freqtrade needs them once to establish the timerange.
    """

    def __init__(self, storage: ReferenceStorage) -> None:
        self._storage = storage
        self._records: dict[str, _SpoolRecord] = {}
        self._date_frames: dict[str, Any] = {}

    def __getitem__(self, pair: str) -> Any:
        return self._storage.read_preprocessed(self._records[pair])

    def __setitem__(self, pair: str, dataframe: Any) -> None:
        prior = self._records.pop(pair, None)
        if prior is not None:
            self._storage.drop_preprocessed(prior)
        record = self._storage.spool_preprocessed(pair, dataframe)
        self._records[pair] = record
        self._date_frames[pair] = dataframe.loc[:, ["date"]].copy()

    def __delitem__(self, pair: str) -> None:
        record = self._records.pop(pair)
        self._date_frames.pop(pair, None)
        self._storage.drop_preprocessed(record)

    def __iter__(self) -> Iterator[str]:
        return iter(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def date_frames(self) -> dict[str, Any]:
        """Return compact frames used by official timerange calculation."""
        return self._date_frames


class ReferenceStorage:
    """Per-backtest Arrow datastore and bounded callback-window cache."""

    def __init__(
        self,
        *,
        base_directory: Path,
        report_path: Path,
        row_block_rows: int,
        cache_limit_bytes: int | None = None,
    ) -> None:
        if row_block_rows <= 0:
            raise ValueError("reference storage row block size must be positive")
        base_directory.mkdir(parents=True, exist_ok=True)
        self.root = Path(
            tempfile.mkdtemp(prefix="run-", dir=base_directory)
        )
        self.report_path = report_path
        self.row_block_rows = row_block_rows
        self.cache_limit_bytes = (
            cache_limit_bytes
            if cache_limit_bytes is not None
            else _default_cache_limit_bytes()
        )
        if self.cache_limit_bytes <= 0:
            raise ValueError("reference callback cache limit must be positive")
        self._records: dict[tuple[int, Any], _SpoolRecord] = {}
        self._cache: OrderedDict[tuple[int, Any], _CachedWindow] = OrderedDict()
        self._cache_bytes = 0
        self._metrics: dict[str, int | float | str | bool] = {
            "mode": DATASTORE_SPOOLED,
            "source_dataframe_shallow_bytes": 0,
            "spool_bytes": 0,
            "pair_count": 0,
            "row_count": 0,
            "column_count_maximum": 0,
            "indicator_spool_bytes_written": 0,
            "indicator_rows_written": 0,
            "indicator_write_seconds": 0.0,
            "indicator_read_seconds": 0.0,
            "page_cache_drop_calls": 0,
            "block_rows": row_block_rows,
            "block_loads": 0,
            "rows_loaded": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "peak_cached_bytes": 0,
            "cache_limit_bytes": self.cache_limit_bytes,
            "write_seconds": 0.0,
            "read_seconds": 0.0,
            "removed_on_exit": False,
        }

    def spool_dataframe(
        self,
        provider: Any,
        pair_key: Any,
        dataframe: Any,
    ) -> None:
        """Write one full analyzed frame as independently readable batches."""
        started = time.perf_counter()
        key = (id(provider), pair_key)
        prior = self._records.pop(key, None)
        if prior is not None:
            self._drop_record(prior)

        digest = hashlib.sha256(repr(key).encode()).hexdigest()
        destination = self.root / f"{digest}.arrow"
        record = self._write_dataframe(destination, dataframe)
        self._records[key] = record
        self._metrics["source_dataframe_shallow_bytes"] = int(
            self._metrics["source_dataframe_shallow_bytes"]
        ) + int(dataframe.memory_usage(index=True, deep=False).sum())
        self._metrics["spool_bytes"] = int(self._metrics["spool_bytes"]) + (
            destination.stat().st_size
        )
        self._metrics["pair_count"] = len(self._records)
        self._metrics["row_count"] = int(self._metrics["row_count"]) + len(dataframe)
        self._metrics["column_count_maximum"] = max(
            int(self._metrics["column_count_maximum"]),
            len(dataframe.columns),
        )
        self._metrics["write_seconds"] = float(self._metrics["write_seconds"]) + (
            time.perf_counter() - started
        )

    def new_preprocessed_frames(self) -> SpooledFrames:
        return SpooledFrames(self)

    def spool_preprocessed(self, pair: str, dataframe: Any) -> _SpoolRecord:
        """Persist one indicator frame before the next pair is analyzed."""
        started = time.perf_counter()
        token = f"indicator:{pair}:{time.perf_counter_ns()}"
        digest = hashlib.sha256(token.encode()).hexdigest()
        record = self._write_dataframe(
            self.root / f"indicator-{digest}.arrow",
            dataframe,
        )
        self._metrics["indicator_spool_bytes_written"] = int(
            self._metrics["indicator_spool_bytes_written"]
        ) + record.path.stat().st_size
        self._metrics["indicator_rows_written"] = int(
            self._metrics["indicator_rows_written"]
        ) + len(dataframe)
        self._metrics["indicator_write_seconds"] = float(
            self._metrics["indicator_write_seconds"]
        ) + (time.perf_counter() - started)
        return record

    def read_preprocessed(self, record: _SpoolRecord) -> Any:
        """Load one full pair frame for official signal generation."""
        import pyarrow as pa
        import pyarrow.ipc as ipc

        started = time.perf_counter()
        with pa.memory_map(str(record.path), "r") as source:
            dataframe = ipc.open_file(source).read_all().to_pandas()
        self._drop_clean_page_cache(record.path, flush=False)
        if record.range_index is not None:
            dataframe.index = _range_index_for_window(
                record.range_index,
                0,
                record.row_count,
            )
        self._metrics["indicator_read_seconds"] = float(
            self._metrics["indicator_read_seconds"]
        ) + (time.perf_counter() - started)
        return dataframe

    def drop_preprocessed(self, record: _SpoolRecord) -> None:
        self._drop_record(record)

    def has_dataframe(self, provider: Any, pair_key: Any) -> bool:
        return (id(provider), pair_key) in self._records

    def read_window(
        self,
        provider: Any,
        pair_key: Any,
        *,
        start: int,
        stop: int,
    ) -> tuple[Any, datetime]:
        """Read the exact iloc window exposed by DataProvider callbacks."""
        import pandas as pd
        import pyarrow as pa
        import pyarrow.ipc as ipc

        key = (id(provider), pair_key)
        record = self._records[key]
        bounded_start = min(max(0, start), record.row_count)
        bounded_stop = min(max(0, stop), record.row_count)
        if bounded_stop <= bounded_start:
            return pd.DataFrame(), record.refreshed_at

        cached = self._cache.get(key)
        if (
            cached is not None
            and cached.start <= bounded_start
            and cached.stop >= bounded_stop
        ):
            self._metrics["cache_hits"] = int(self._metrics["cache_hits"]) + 1
            self._cache.move_to_end(key)
            return (
                cached.dataframe.iloc[
                    bounded_start - cached.start : bounded_stop - cached.start
                ],
                record.refreshed_at,
            )

        self._metrics["cache_misses"] = int(self._metrics["cache_misses"]) + 1
        started = time.perf_counter()
        first_batch = bounded_start // self.row_block_rows
        last_batch = (bounded_stop - 1) // self.row_block_rows
        cache_start = first_batch * self.row_block_rows
        cache_stop = min(
            record.row_count,
            (last_batch + 1) * self.row_block_rows,
        )
        with pa.memory_map(str(record.path), "r") as source:
            reader = ipc.open_file(source)
            batches = [
                reader.get_batch(index)
                for index in range(first_batch, last_batch + 1)
            ]
            dataframe = pa.Table.from_batches(
                batches,
                schema=reader.schema,
            ).to_pandas()
        self._drop_clean_page_cache(record.path, flush=False)
        if record.range_index is not None:
            dataframe.index = _range_index_for_window(
                record.range_index,
                cache_start,
                cache_stop,
            )
        frame_bytes = int(dataframe.memory_usage(index=True, deep=False).sum())
        self._replace_cache(
            key,
            _CachedWindow(
                start=cache_start,
                stop=cache_stop,
                dataframe=dataframe,
                bytes=frame_bytes,
            ),
        )
        self._metrics["block_loads"] = int(self._metrics["block_loads"]) + len(
            batches
        )
        self._metrics["rows_loaded"] = int(self._metrics["rows_loaded"]) + len(
            dataframe
        )
        self._metrics["read_seconds"] = float(self._metrics["read_seconds"]) + (
            time.perf_counter() - started
        )
        return (
            dataframe.iloc[
                bounded_start - cache_start : bounded_stop - cache_start
            ],
            record.refreshed_at,
        )

    def clear_provider(self, provider: Any) -> None:
        provider_id = id(provider)
        for key in [key for key in self._cache if key[0] == provider_id]:
            self._remove_cache(key)
        for key in [key for key in self._records if key[0] == provider_id]:
            self._drop_record(self._records.pop(key))
        self._refresh_spool_metrics()

    def finish(self) -> dict[str, Any]:
        """Write metrics atomically, then remove the ephemeral Arrow payload."""
        self._refresh_spool_metrics()
        metrics = dict(self._metrics)
        shutil.rmtree(self.root)
        metrics["removed_on_exit"] = True
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_suffix(f"{self.report_path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                metrics,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.report_path)
        self._records.clear()
        self._cache.clear()
        self._cache_bytes = 0
        return metrics

    def _replace_cache(
        self,
        key: tuple[int, Any],
        window: _CachedWindow,
    ) -> None:
        self._remove_cache(key)
        self._cache[key] = window
        self._cache_bytes += window.bytes
        while (
            self._cache_bytes > self.cache_limit_bytes
            and len(self._cache) > 1
        ):
            oldest = next(iter(self._cache))
            self._remove_cache(oldest)
        self._metrics["peak_cached_bytes"] = max(
            int(self._metrics["peak_cached_bytes"]),
            self._cache_bytes,
        )

    def _remove_cache(self, key: tuple[int, Any]) -> None:
        cached = self._cache.pop(key, None)
        if cached is not None:
            self._cache_bytes -= cached.bytes

    def _drop_record(self, record: _SpoolRecord) -> None:
        record.path.unlink(missing_ok=True)

    def _write_dataframe(
        self,
        destination: Path,
        dataframe: Any,
    ) -> _SpoolRecord:
        import pyarrow as pa
        import pyarrow.ipc as ipc

        range_index = _range_index_record(dataframe.index)
        schema = None
        writer = None
        sink = pa.OSFile(str(destination), "wb")
        try:
            for start in range(0, len(dataframe), self.row_block_rows):
                stop = min(len(dataframe), start + self.row_block_rows)
                chunk = dataframe.iloc[start:stop]
                table = pa.Table.from_pandas(
                    chunk,
                    schema=schema,
                    preserve_index=True,
                    safe=True,
                ).combine_chunks()
                if schema is None:
                    schema = table.schema
                    writer = ipc.new_file(
                        sink,
                        schema,
                        options=ipc.IpcWriteOptions(compression="lz4"),
                    )
                batches = table.to_batches(max_chunksize=self.row_block_rows)
                if len(batches) != 1:
                    raise RuntimeError(
                        "reference Arrow chunk did not produce exactly one record batch"
                    )
                writer.write_batch(batches[0])
            if schema is None:
                empty = pa.Table.from_pandas(
                    dataframe,
                    preserve_index=True,
                    safe=True,
                )
                writer = ipc.new_file(
                    sink,
                    empty.schema,
                    options=ipc.IpcWriteOptions(compression="lz4"),
                )
        finally:
            if writer is not None:
                writer.close()
            else:
                sink.close()
        self._drop_clean_page_cache(destination, flush=True)
        return _SpoolRecord(
            path=destination,
            row_count=len(dataframe),
            column_count=len(dataframe.columns),
            refreshed_at=datetime.now(UTC),
            range_index=range_index,
        )

    def _drop_clean_page_cache(self, path: Path, *, flush: bool) -> None:
        """Keep Arrow files on disk without charging them to cgroup memory.

        Linux accounts clean filesystem page cache to the writing container.
        A five-year spool can therefore reach ``memory.max`` even when process
        RSS is small.  ``POSIX_FADV_DONTNEED`` is only a cache-retention hint:
        it cannot change file bytes or dataframe semantics.  ``fsync`` first
        makes freshly written pages eligible for immediate eviction.
        """
        if not (
            hasattr(os, "posix_fadvise")
            and hasattr(os, "POSIX_FADV_DONTNEED")
        ):
            return
        descriptor = os.open(path, os.O_RDWR if flush else os.O_RDONLY)
        try:
            if flush:
                os.fsync(descriptor)
            os.posix_fadvise(
                descriptor,
                0,
                0,
                os.POSIX_FADV_DONTNEED,
            )
        finally:
            os.close(descriptor)
        self._metrics["page_cache_drop_calls"] = int(
            self._metrics["page_cache_drop_calls"]
        ) + 1

    def _refresh_spool_metrics(self) -> None:
        self._metrics["spool_bytes"] = sum(
            record.path.stat().st_size
            for record in self._records.values()
            if record.path.is_file()
        )
        self._metrics["pair_count"] = len(self._records)


_ACTIVE_STORAGE: ReferenceStorage | None = None


def storage_enabled() -> bool:
    return os.environ.get(DATASTORE_MODE_ENV) == DATASTORE_SPOOLED


def begin_storage(*, row_block_rows: int) -> ReferenceStorage:
    """Create the one storage session owned by the current backtest process."""
    global _ACTIVE_STORAGE
    if _ACTIVE_STORAGE is not None:
        raise RuntimeError("reference storage session is already active")
    report_text = os.environ.get(STORAGE_REPORT_ENV)
    if not report_text:
        raise RuntimeError(
            f"{STORAGE_REPORT_ENV} is required for the spooled reference datastore"
        )
    base = Path(
        os.environ.get(
            SPOOL_DIRECTORY_ENV,
            str(Path(tempfile.gettempdir()) / "nfi-reference-spool"),
        )
    )
    cache_text = os.environ.get(CACHE_BYTES_ENV)
    cache_limit = int(cache_text) if cache_text else None
    _ACTIVE_STORAGE = ReferenceStorage(
        base_directory=base,
        report_path=Path(report_text),
        row_block_rows=row_block_rows,
        cache_limit_bytes=cache_limit,
    )
    return _ACTIVE_STORAGE


def active_storage() -> ReferenceStorage:
    if _ACTIVE_STORAGE is None:
        raise RuntimeError("reference storage session has not been initialized")
    return _ACTIVE_STORAGE


def finish_storage() -> dict[str, Any]:
    global _ACTIVE_STORAGE
    storage = active_storage()
    try:
        return storage.finish()
    finally:
        _ACTIVE_STORAGE = None


def patch_data_provider(cls: type) -> None:
    """Patch only DataProvider cache representation in the active spool mode."""
    original_set = cls._set_cached_df
    original_get = cls.get_analyzed_dataframe
    original_clear = cls.clear_cache

    def spooled_set(
        self: Any,
        pair: str,
        timeframe: str,
        dataframe: Any,
        candle_type: Any,
    ) -> None:
        if not storage_enabled():
            original_set(self, pair, timeframe, dataframe, candle_type)
            return
        pair_key = (pair, timeframe, candle_type)
        active_storage().spool_dataframe(self, pair_key, dataframe)

    def spooled_get(self: Any, pair: str, timeframe: str) -> tuple[Any, datetime]:
        if not storage_enabled():
            return original_get(self, pair, timeframe)

        from freqtrade.data.dataprovider import MAX_DATAFRAME_CANDLES
        from freqtrade.enums import CandleType, RunMode
        from pandas import DataFrame

        pair_key = (
            pair,
            timeframe,
            self._config.get("candle_type_def", CandleType.SPOT),
        )
        storage = active_storage()
        if not storage.has_dataframe(self, pair_key):
            return original_get(self, pair, timeframe)
        if self.runmode in (RunMode.DRY_RUN, RunMode.LIVE):
            raise RuntimeError(
                "spooled reference datastore is valid only for official backtesting"
            )
        max_index = self._DataProvider__slice_index.get(pair)
        if max_index is None:
            return DataFrame(), datetime.fromtimestamp(0, tz=UTC)
        return storage.read_window(
            self,
            pair_key,
            start=max(0, max_index - MAX_DATAFRAME_CANDLES),
            stop=max_index,
        )

    def spooled_clear(self: Any) -> None:
        if storage_enabled() and _ACTIVE_STORAGE is not None:
            active_storage().clear_provider(self)
        original_clear(self)

    cls._set_cached_df = spooled_set
    cls.get_analyzed_dataframe = spooled_get
    cls.clear_cache = spooled_clear


def _range_index_record(index: Any) -> dict[str, Any] | None:
    import pandas as pd

    if not isinstance(index, pd.RangeIndex):
        return None
    return {
        "start": index.start,
        "step": index.step,
        "name": index.name,
    }


def _range_index_for_window(
    record: dict[str, Any],
    start: int,
    stop: int,
) -> Any:
    import pandas as pd

    origin = int(record["start"])
    step = int(record["step"])
    return pd.RangeIndex(
        start=origin + start * step,
        stop=origin + stop * step,
        step=step,
        name=record["name"],
    )


def _default_cache_limit_bytes() -> int:
    explicit_limit = _cgroup_memory_limit()
    if explicit_limit is None:
        if hasattr(os, "sysconf"):
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            page_count = int(os.sysconf("SC_PHYS_PAGES"))
            explicit_limit = page_size * page_count
        else:
            raise RuntimeError(
                f"{CACHE_BYTES_ENV} is required when physical memory is unavailable"
            )
    return max(1, int(explicit_limit * CALLBACK_CACHE_MEMORY_FRACTION))


def _cgroup_memory_limit() -> int | None:
    for path in (
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text and text != "max":
            value = int(text)
            # Legacy cgroup v1 reports a page-aligned sentinel near 2**63 when
            # no limit exists.  Values beyond physical address space are not a
            # usable cache budget.
            if value < 2**60:
                return value
    return None
