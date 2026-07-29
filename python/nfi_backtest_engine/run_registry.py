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
        self._ensure_selection_columns()

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
                    strategy_sha256, config_sha256, pair_count, trade_count, updated_at,
                    native_status, selected_status, selected_lane, official_trade_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    output_directory = excluded.output_directory,
                    pair_count = excluded.pair_count,
                    trade_count = excluded.trade_count,
                    native_status = excluded.native_status,
                    selected_status = excluded.selected_status,
                    selected_lane = excluded.selected_lane,
                    official_trade_count = excluded.official_trade_count,
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
                    report.get("native_status", report["status"]),
                    report.get("selected_status", report["status"]),
                    report.get("selected_lane", "native"),
                    (trade_count if report.get("selected_lane") == "official" else None),
                ),
            )

    def record_selection(self, output_directory: str | Path) -> None:
        """Project a hash-valid selected result without rewriting Native status."""

        root = Path(output_directory).resolve()
        run = read_json(root / "run.json")
        if not isinstance(run, dict):
            raise BenchmarkError(f"run report must be an object: {root / 'run.json'}")
        from .selected_result import load_selected_run_view

        selected, selection = load_selected_run_view(root, run)
        if selection is None:
            raise BenchmarkError(f"selected result does not exist: {root}")
        result = selected.get("result")
        trade_count = result.get("trade_count") if isinstance(result, dict) else None
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE research_runs
                SET status = ?, native_status = ?, selected_status = ?,
                    selected_lane = ?, official_trade_count = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    selected["selected_status"],
                    selected["native_status"],
                    selected["selected_status"],
                    selected["selected_lane"],
                    trade_count,
                    selection["selected_at"],
                    selected["run_id"],
                ),
            )
        if cursor.rowcount != 1:
            raise BenchmarkError(
                f"run registry does not contain selected run: {selected['run_id']}"
            )

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 1000:
            raise BenchmarkError("run registry limit must be between 1 and 1000")
        rows = self.connection.execute(
            """
            SELECT run_id, status, output_directory, strategy_class, strategy_sha256,
                   config_sha256, pair_count, trade_count, updated_at,
                   native_status, selected_status, selected_lane, official_trade_count
            FROM research_runs
            ORDER BY updated_at DESC, run_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def show(self, run_id: str) -> dict[str, Any]:
        requested_id = run_id.strip()
        if not requested_id:
            raise BenchmarkError("run id must not be empty")
        row = self.connection.execute(
            """
            SELECT run_id, status, output_directory, strategy_class, strategy_sha256,
                   config_sha256, pair_count, trade_count, updated_at,
                   native_status, selected_status, selected_lane, official_trade_count
            FROM research_runs
            WHERE run_id = ?
            """,
            (requested_id,),
        ).fetchone()
        if row is None:
            # Human-readable listings deliberately abbreviate the SHA-256 run ID.
            # Resolve that displayed prefix only when it identifies one run.
            prefix_rows = self.connection.execute(
                """
                SELECT run_id, status, output_directory, strategy_class, strategy_sha256,
                       config_sha256, pair_count, trade_count, updated_at,
                       native_status, selected_status, selected_lane, official_trade_count
                FROM research_runs
                WHERE substr(run_id, 1, length(?)) = ?
                ORDER BY run_id
                LIMIT 2
                """,
                (requested_id, requested_id),
            ).fetchall()
            if len(prefix_rows) > 1:
                raise BenchmarkError(f"run id prefix is ambiguous: {requested_id}")
            row = prefix_rows[0] if prefix_rows else None
        if row is None:
            raise BenchmarkError(f"run registry does not contain: {requested_id}")
        record = dict(row)
        run_path = Path(record["output_directory"]) / "run.json"
        record["report"] = read_json(run_path) if run_path.is_file() else None
        selected_path = Path(record["output_directory"]) / "selected-result.json"
        record["selected_result"] = read_json(selected_path) if selected_path.is_file() else None
        return record

    def _ensure_selection_columns(self) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(research_runs)").fetchall()
        }
        additions = {
            "native_status": "TEXT",
            "selected_status": "TEXT",
            "selected_lane": "TEXT",
            "official_trade_count": "INTEGER",
        }
        with self.connection:
            for name, sql_type in additions.items():
                if name not in columns:
                    self.connection.execute(
                        f"ALTER TABLE research_runs ADD COLUMN {name} {sql_type}"
                    )
