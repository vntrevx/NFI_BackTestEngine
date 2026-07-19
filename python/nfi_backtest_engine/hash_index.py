"""Persistent SHA-256 index keyed by stable file metadata."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .fixture import sha256_file


class FileHashIndex:
    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database, timeout=30)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS file_hashes (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL
            )
            """
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> FileHashIndex:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def hash_file(self, source: str | Path) -> str:
        path = Path(source).resolve()
        stat = path.stat()
        row = self.connection.execute(
            "SELECT size, mtime_ns, sha256 FROM file_hashes WHERE path = ?",
            (str(path),),
        ).fetchone()
        if row is not None and row[0] == stat.st_size and row[1] == stat.st_mtime_ns:
            return str(row[2])
        digest = sha256_file(path)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO file_hashes(path, size, mtime_ns, sha256)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    sha256 = excluded.sha256
                """,
                (str(path), stat.st_size, stat.st_mtime_ns, digest),
            )
        return digest
