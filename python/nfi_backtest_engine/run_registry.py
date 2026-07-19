"""Small durable index of checkpointed research runs."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .canonical import read_json
from .errors import BenchmarkError


class RunRegistry:
    def __init__(self, source: str | Path) -> None:
        self.path = Path(source).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                output_directory TEXT NOT NULL,
                strategy_class TEXT NOT NULL,
                strategy_sha256 TEXT NOT NULL,
                config_sha256 TEXT NOT NULL,
                pair_count INTEGER NOT NULL,
                trade_count INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> RunRegistry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def record(self, report: dict[str, Any], output_directory: str | Path) -> None:
        identity = report["inputs"]
        result = report.get("result")
        trade_count = result.get("trade_count") if isinstance(result, dict) else None
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO research_runs (
                    run_id, status, output_directory, strategy_class,
                    strategy_sha256, config_sha256, pair_count, trade_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    output_directory = excluded.output_directory,
                    pair_count = excluded.pair_count,
                    trade_count = excluded.trade_count,
                    updated_at = excluded.updated_at
                """,
                (
                    report["run_id"],
                    report["status"],
                    str(Path(output_directory).resolve()),
                    identity["strategy"]["class_name"],
                    identity["strategy"]["file_sha256"],
                    identity["config"]["run_effective_sha256"],
                    report["vectors"]["pair_count"],
                    trade_count,
                    report["created_at"],
                ),
            )

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 1000:
            raise BenchmarkError("run registry limit must be between 1 and 1000")
        rows = self.connection.execute(
            """
            SELECT run_id, status, output_directory, strategy_class, strategy_sha256,
                   config_sha256, pair_count, trade_count, updated_at
            FROM research_runs
            ORDER BY updated_at DESC, run_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def show(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT run_id, status, output_directory, strategy_class, strategy_sha256,
                   config_sha256, pair_count, trade_count, updated_at
            FROM research_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise BenchmarkError(f"run registry does not contain: {run_id}")
        record = dict(row)
        run_path = Path(record["output_directory"]) / "run.json"
        record["report"] = read_json(run_path) if run_path.is_file() else None
        return record
