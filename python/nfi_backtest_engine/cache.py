"""Small content-addressed artifact cache with deterministic metadata."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import psutil

from .canonical import read_json, write_json
from .errors import SpecValidationError
from .fixture import sha256_file


def cache_key(namespace: str, identity: dict[str, Any]) -> str:
    if not namespace or "/" in namespace or "\\" in namespace:
        raise SpecValidationError("cache namespace must be one path-safe component")
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"{namespace}-{hashlib.sha256(encoded).hexdigest()}"


class ContentCache:
    def __init__(self, root: str | Path, *, max_bytes: int = 50 * 1024**3) -> None:
        self.root = Path(root).resolve()
        if max_bytes <= 0:
            raise SpecValidationError("cache max_bytes must be positive")
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> Path | None:
        entry = self._entry(key)
        payload = entry / "payload"
        metadata = entry / "metadata.json"
        if not payload.is_file() or not metadata.is_file():
            return None
        try:
            metadata.touch(exist_ok=True)
        except FileNotFoundError:
            return None
        return payload

    def put_file(self, key: str, source: str | Path) -> Path:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise SpecValidationError(f"cache source is not a file: {source_path}")
        source_digest = sha256_file(source_path)
        with self._key_lock(key):
            entry = self._entry(key)
            existing = self.get(key)
            if existing is not None:
                if sha256_file(existing) != source_digest:
                    raise SpecValidationError(f"cache key collision: {key}")
                return existing
            if entry.exists():
                raise SpecValidationError(f"cache entry is incomplete: {key}")
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{key}.", suffix=".tmp", dir=self.root)
            )
            try:
                payload = temporary / "payload"
                shutil.copyfile(source_path, payload)
                payload_digest = sha256_file(payload)
                if payload_digest != source_digest:
                    raise SpecValidationError(f"cache copy changed while publishing: {key}")
                write_json(
                    temporary / "metadata.json",
                    {
                        "schema_version": "1.0.0",
                        "key": key,
                        "bytes": payload.stat().st_size,
                        "sha256": payload_digest,
                        "last_access_ns": time.time_ns(),
                    },
                )
                temporary.replace(entry)
            finally:
                self._discard_temporary(temporary)
        self.prune()
        return entry / "payload"

    def put_bytes(self, key: str, contents: bytes) -> Path:
        digest = hashlib.sha256(contents).hexdigest()
        with self._key_lock(key):
            entry = self._entry(key)
            existing = self.get(key)
            if existing is not None:
                if sha256_file(existing) != digest:
                    raise SpecValidationError(f"cache key collision: {key}")
                return existing
            if entry.exists():
                raise SpecValidationError(f"cache entry is incomplete: {key}")
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{key}.", suffix=".tmp", dir=self.root)
            )
            try:
                payload = temporary / "payload"
                payload.write_bytes(contents)
                write_json(
                    temporary / "metadata.json",
                    {
                        "schema_version": "1.0.0",
                        "key": key,
                        "bytes": len(contents),
                        "sha256": digest,
                        "last_access_ns": time.time_ns(),
                    },
                )
                temporary.replace(entry)
            finally:
                self._discard_temporary(temporary)
        self.prune()
        return entry / "payload"

    def prune(self) -> None:
        with self._key_lock("cache-prune"):
            self._prune_locked()

    def _prune_locked(self) -> None:
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for entry in self.root.iterdir():
            payload = entry / "payload"
            metadata = entry / "metadata.json"
            if not entry.is_dir() or not payload.is_file() or not metadata.is_file():
                continue
            size = payload.stat().st_size
            total += size
            record = read_json(metadata)
            last_access_ns = record.get("last_access_ns")
            if not isinstance(last_access_ns, int):
                last_access_ns = metadata.stat().st_mtime_ns
            else:
                last_access_ns = max(last_access_ns, metadata.stat().st_mtime_ns)
            entries.append((last_access_ns, size, entry))
        for _, size, entry in sorted(entries):
            if total <= self.max_bytes:
                break
            for child in entry.iterdir():
                if child.is_file():
                    child.unlink()
            entry.rmdir()
            total -= size

    def _entry(self, key: str) -> Path:
        if (
            not key
            or key.startswith(".")
            or "/" in key
            or "\\" in key
            or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for char in key
            )
        ):
            raise SpecValidationError("cache key is not path-safe")
        entry = (self.root / key).resolve()
        if not entry.is_relative_to(self.root):
            raise SpecValidationError("cache key escapes the cache root")
        return entry

    @staticmethod
    def _discard_temporary(temporary: Path) -> None:
        if not temporary.exists():
            return
        for child in temporary.iterdir():
            if child.is_file():
                child.unlink()
        temporary.rmdir()

    @contextmanager
    def _key_lock(self, key: str, *, timeout_seconds: float = 120.0) -> Iterator[None]:
        lock = self.root / f".{key}.lock"
        deadline = time.monotonic() + timeout_seconds
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(descriptor, str(os.getpid()).encode())
                os.fsync(descriptor)
            except FileExistsError as exc:
                self._discard_dead_lock(lock)
                if time.monotonic() >= deadline:
                    raise SpecValidationError(f"cache lock timed out: {key}") from exc
                time.sleep(0.01)
        try:
            yield
        finally:
            os.close(descriptor)
            lock.unlink(missing_ok=True)

    @staticmethod
    def _discard_dead_lock(lock: Path) -> None:
        try:
            owner = int(lock.read_text(encoding="ascii"))
        except (FileNotFoundError, ValueError):
            return
        if psutil.pid_exists(owner):
            return
        with suppress(FileNotFoundError):
            lock.unlink()
