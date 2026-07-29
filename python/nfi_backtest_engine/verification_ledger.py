"""Append-only verification history and status projections."""

from __future__ import annotations

import hashlib
import html
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .canonical import write_json
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file
from .specs import validate_verification_ledger_record

LEDGER_SCHEMA_VERSION = "1.0.0"
STRATEGY_STATES = ("latest_checked", "quick_verified", "release_certified")
RUN_STATES = (
    "prepared",
    "native_complete",
    "official_complete",
    "quick_verified",
    "release_certified",
    "blocked_unsupported_semantics",
    "failed",
)
_STRATEGY_SUCCESS_RANK = {
    "latest_checked": 1,
    "quick_verified": 2,
    "release_certified": 3,
}
_RUN_SUCCESS_RANK = {
    "prepared": 1,
    "native_complete": 2,
    "quick_verified": 3,
    "release_certified": 4,
}
_RELEASE_FINGERPRINT_FIELDS = (
    "upstream_repository",
    "upstream_commit",
    "strategy_version",
    "strategy_source_sha256",
    "strategy_ir_sha256",
    "hot_callback_ir_sha256",
    "config_sha256",
    "pairlist_sha256",
    "data_seal_sha256",
    "market_snapshot_sha256",
    "timerange",
    "mode_contract",
    "reference_version",
    "reference_image_index_digest",
    "reference_image_platform_digest",
    "reference_platform",
    "package_sha256",
    "wheel_sha256",
    "native_binary_sha256",
)

SubjectKind = Literal["strategy_revision", "run"]
Outcome = Literal["success", "failure"]


def create_verification_record(
    *,
    subject_kind: SubjectKind,
    subject_id: str,
    state: str,
    outcome: Outcome,
    fingerprint: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]] = (),
    failure: Mapping[str, Any] | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Create a self-hashed ledger record from explicit, identity-only inputs."""
    fingerprint_document = dict(fingerprint)
    fingerprint_sha256 = _canonical_sha256(fingerprint_document)
    record: dict[str, Any] = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "recorded_at": recorded_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "subject": {
            "kind": subject_kind,
            "id": subject_id,
        },
        "state": state,
        "outcome": outcome,
        "fingerprint": fingerprint_document,
        "fingerprint_sha256": fingerprint_sha256,
        "evidence": [dict(item) for item in evidence],
        "failure": dict(failure) if failure is not None else None,
    }
    record["event_id"] = _canonical_sha256(record)
    _validate_record(record)
    return record


def record_strategy_compatibility(
    ledger: VerificationLedger,
    report: Mapping[str, Any],
    *,
    upstream_repository: str | None = None,
    upstream_commit: str | None = None,
    strategy_version: str | None = None,
    report_path: str | Path | None = None,
) -> int:
    """Append a compatibility result as ``latest_checked`` without implying parity."""
    source = report.get("source")
    if not isinstance(source, Mapping):
        raise SpecValidationError("compatibility report is missing source identity")
    strategy_sha = source.get("sha256")
    if not isinstance(strategy_sha, str):
        raise SpecValidationError("compatibility report source SHA-256 is missing")
    config = report.get("config")
    config_sha = config.get("sha256") if isinstance(config, Mapping) else None
    callback_ir = report.get("callback_ir")
    hot_ir_sha = callback_ir.get("fingerprint") if isinstance(callback_ir, Mapping) else None
    compatible = report.get("native_compatible") is True
    blockers = report.get("blockers")
    first_blocker = (
        blockers[0]
        if isinstance(blockers, list) and blockers and isinstance(blockers[0], Mapping)
        else None
    )
    failure = None
    if not compatible:
        failure = {
            "code": (
                str(first_blocker.get("code", "NATIVE_COMPATIBILITY_INCOMPLETE"))
                if first_blocker is not None
                else "NATIVE_COMPATIBILITY_INCOMPLETE"
            ),
            "message": (
                str(first_blocker.get("message", "native compatibility did not complete"))
                if first_blocker is not None
                else "native compatibility did not complete"
            ),
        }
    evidence = []
    if report_path is not None:
        path = Path(report_path).resolve()
        if not path.is_file():
            raise SpecValidationError(f"compatibility report does not exist: {path}")
        evidence.append(
            {
                "kind": "compatibility_report",
                "location": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    fingerprint = {
        "upstream_repository": upstream_repository,
        "upstream_commit": upstream_commit,
        "strategy_version": strategy_version,
        "strategy_source_sha256": strategy_sha,
        "strategy_ir_sha256": None,
        "hot_callback_ir_sha256": hot_ir_sha,
        "config_sha256": config_sha,
        "pairlist_sha256": None,
        "data_seal_sha256": None,
        "market_snapshot_sha256": None,
        "timerange": None,
        "mode_contract": report.get("trading_mode"),
        "reference_version": None,
        "reference_image_index_digest": None,
        "reference_image_platform_digest": None,
        "reference_platform": None,
        "package_sha256": None,
        "wheel_sha256": None,
        "native_binary_sha256": None,
    }
    record = create_verification_record(
        subject_kind="strategy_revision",
        subject_id=strategy_sha,
        state="latest_checked",
        outcome="success" if compatible else "failure",
        fingerprint=fingerprint,
        evidence=evidence,
        failure=failure,
        recorded_at=str(report["checked_at"]),
    )
    return ledger.append(record)


class VerificationLedger:
    """SQLite-backed ledger whose rows cannot be updated or deleted."""

    def __init__(self, source: str | Path, *, create: bool = True) -> None:
        self.path = Path(source).resolve()
        if not create and not self.path.is_file():
            raise BenchmarkError(f"verification ledger does not exist: {self.path}")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=30000")
        if create:
            self._initialize()
        else:
            self._validate_existing()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> VerificationLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def append(self, record: Mapping[str, Any]) -> int:
        """Append one event, returning its durable sequence number.

        Replaying the byte-identical event is idempotent. A different event is never
        allowed to replace an existing row.
        """
        document = dict(record)
        _validate_record(document)
        serialized = _canonical_json(document)
        subject = document["subject"]
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                """
                SELECT sequence, record_json
                FROM verification_records
                WHERE event_id = ?
                """,
                (document["event_id"],),
            ).fetchone()
            if existing is not None:
                if existing["record_json"] != serialized:
                    raise BenchmarkError(
                        f"verification event identity collision: {document['event_id']}"
                    )
                return int(existing["sequence"])

            latest = self.connection.execute(
                """
                SELECT recorded_at
                FROM verification_records
                WHERE subject_kind = ? AND subject_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (subject["kind"], subject["id"]),
            ).fetchone()
            if latest is not None and _parse_timestamp(document["recorded_at"]) < _parse_timestamp(
                latest["recorded_at"]
            ):
                raise BenchmarkError(
                    "verification records for one subject must be appended in timestamp order"
                )
            self._validate_transition(document)
            cursor = self.connection.execute(
                """
                INSERT INTO verification_records (
                    event_id, recorded_at, subject_kind, subject_id, state,
                    outcome, fingerprint_sha256, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document["event_id"],
                    document["recorded_at"],
                    subject["kind"],
                    subject["id"],
                    document["state"],
                    document["outcome"],
                    document["fingerprint_sha256"],
                    serialized,
                ),
            )
        sequence = cursor.lastrowid
        if sequence is None:
            raise BenchmarkError("verification ledger did not return an inserted sequence")
        return int(sequence)

    def records(
        self,
        *,
        subject_kind: SubjectKind | None = None,
        subject_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[str] = []
        if subject_kind is not None:
            clauses.append("subject_kind = ?")
            parameters.append(subject_kind)
        if subject_id is not None:
            clauses.append("subject_id = ?")
            parameters.append(subject_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT sequence, record_json
            FROM verification_records
            {where}
            ORDER BY sequence
            """,
            parameters,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = json.loads(row["record_json"])
            if not isinstance(record, dict):
                raise BenchmarkError("verification ledger contains a non-object record")
            result.append({"sequence": int(row["sequence"]), **record})
        return result

    def project(self) -> dict[str, Any]:
        """Project latest checks without conflating them with successful certification."""
        records = self.records()
        strategies = [
            record for record in records if record["subject"]["kind"] == "strategy_revision"
        ]
        strategy_status = {
            "latest_checked": _latest_matching(strategies, lambda _record: True),
            "quick_verified": _latest_matching(
                strategies,
                lambda record: (
                    record["outcome"] == "success"
                    and _STRATEGY_SUCCESS_RANK.get(record["state"], 0) >= 2
                ),
            ),
            "release_certified": _latest_matching(
                strategies,
                lambda record: (
                    record["outcome"] == "success" and record["state"] == "release_certified"
                ),
            ),
        }
        run_groups: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            if record["subject"]["kind"] == "run":
                run_groups.setdefault(record["subject"]["id"], []).append(record)
        runs = []
        for run_id, run_records in run_groups.items():
            successful = [
                record
                for record in run_records
                if record["outcome"] == "success" and record["state"] in _RUN_SUCCESS_RANK
            ]
            highest_rank = max(
                (_RUN_SUCCESS_RANK[record["state"]] for record in successful),
                default=0,
            )
            runs.append(
                {
                    "run_id": run_id,
                    "latest": run_records[-1],
                    "highest_success": _latest_matching(
                        successful,
                        lambda record, rank=highest_rank: (
                            _RUN_SUCCESS_RANK[record["state"]] == rank
                        ),
                    ),
                    "official_complete": _latest_matching(
                        run_records,
                        lambda record: (
                            record["outcome"] == "success"
                            and record["state"] == "official_complete"
                        ),
                    ),
                    "latest_failure": _latest_matching(
                        run_records,
                        lambda record: record["outcome"] == "failure",
                    ),
                }
            )
        runs.sort(key=lambda item: item["latest"]["sequence"], reverse=True)
        return {
            "schema_version": "1.0.0",
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "record_count": len(records),
            "strategy": strategy_status,
            "runs": runs,
        }

    def _initialize(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS verification_ledger_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO verification_ledger_meta (singleton, schema_version)
                VALUES (1, ?)
                """,
                (LEDGER_SCHEMA_VERSION,),
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS verification_records (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    recorded_at TEXT NOT NULL,
                    subject_kind TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    fingerprint_sha256 TEXT NOT NULL,
                    record_json TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS verification_records_subject
                ON verification_records (subject_kind, subject_id, sequence)
                """
            )
            self.connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS verification_records_no_update
                BEFORE UPDATE ON verification_records
                BEGIN
                    SELECT RAISE(ABORT, 'verification ledger is append-only');
                END
                """
            )
            self.connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS verification_records_no_delete
                BEFORE DELETE ON verification_records
                BEGIN
                    SELECT RAISE(ABORT, 'verification ledger is append-only');
                END
                """
            )
        self._validate_existing()

    def _validate_existing(self) -> None:
        try:
            row = self.connection.execute(
                """
                SELECT schema_version
                FROM verification_ledger_meta
                WHERE singleton = 1
                """
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise BenchmarkError(f"invalid verification ledger: {self.path}") from exc
        if row is None or row["schema_version"] != LEDGER_SCHEMA_VERSION:
            actual = row["schema_version"] if row is not None else None
            raise BenchmarkError(
                "unsupported verification ledger schema: "
                f"expected {LEDGER_SCHEMA_VERSION}, got {actual!r}"
            )

    def _validate_transition(self, record: Mapping[str, Any]) -> None:
        if record["outcome"] != "success":
            return
        subject = record["subject"]
        rank_table = (
            _STRATEGY_SUCCESS_RANK if subject["kind"] == "strategy_revision" else _RUN_SUCCESS_RANK
        )
        proposed_rank = rank_table.get(record["state"])
        if subject["kind"] == "run" and record["state"] == "official_complete":
            return
        if proposed_rank is None:
            raise BenchmarkError(
                f"failure-only verification state cannot succeed: {record['state']}"
            )
        rows = self.connection.execute(
            """
            SELECT state
            FROM verification_records
            WHERE subject_kind = ? AND subject_id = ? AND fingerprint_sha256 = ?
              AND outcome = 'success'
            """,
            (
                subject["kind"],
                subject["id"],
                record["fingerprint_sha256"],
            ),
        ).fetchall()
        highest = max((rank_table.get(row["state"], 0) for row in rows), default=0)
        if proposed_rank < highest:
            raise BenchmarkError(
                "verification state cannot regress for the same fingerprint: "
                f"{record['state']} follows rank {highest}"
            )


def format_verification_projection(projection: Mapping[str, Any]) -> str:
    """Render a stable terminal snapshot with independent status lines."""
    strategy = projection.get("strategy")
    if not isinstance(strategy, Mapping):
        raise BenchmarkError("verification projection is missing strategy status")
    lines = [
        f"Verification ledger     {projection.get('record_count', 0)} records",
        f"Latest checked          {_format_record(strategy.get('latest_checked'), 'CHECKED')}",
        f"Quick verified          {_format_record(strategy.get('quick_verified'), 'VERIFIED')}",
        f"Release certified       {_format_record(strategy.get('release_certified'), 'CERTIFIED')}",
        f"Tracked runs            {len(projection.get('runs', []))}",
    ]
    return "\n".join(lines)


def write_verification_projection(
    projection: Mapping[str, Any],
    *,
    json_path: str | Path | None = None,
    html_path: str | Path | None = None,
) -> None:
    """Write derived status reports without modifying source evidence or the ledger."""
    if json_path is not None:
        write_json(json_path, dict(projection))
    if html_path is not None:
        destination = Path(html_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_render_html(projection), encoding="utf-8")


def _validate_record(record: dict[str, Any]) -> None:
    validate_verification_ledger_record(record)
    fingerprint_sha256 = _canonical_sha256(record["fingerprint"])
    if record["fingerprint_sha256"] != fingerprint_sha256:
        raise SpecValidationError("$.fingerprint_sha256: does not match the canonical fingerprint")
    event_payload = {key: value for key, value in record.items() if key != "event_id"}
    if record["event_id"] != _canonical_sha256(event_payload):
        raise SpecValidationError("$.event_id: does not match the canonical record")
    subject = record["subject"]
    state = record["state"]
    if subject["kind"] == "strategy_revision" and state not in STRATEGY_STATES:
        raise SpecValidationError(f"strategy revision cannot enter run state {state!r}")
    if subject["kind"] == "run" and state not in RUN_STATES:
        raise SpecValidationError(f"run cannot enter strategy state {state!r}")
    if (
        subject["kind"] == "strategy_revision"
        and subject["id"] != record["fingerprint"]["strategy_source_sha256"]
    ):
        raise SpecValidationError(
            "strategy revision subject id must equal fingerprint.strategy_source_sha256"
        )
    if record["outcome"] == "success" and record["failure"] is not None:
        raise SpecValidationError("$.failure must be null for a successful record")
    if record["outcome"] == "failure" and record["failure"] is None:
        raise SpecValidationError("$.failure is required for a failed record")
    if state in {"blocked_unsupported_semantics", "failed"} and record["outcome"] != "failure":
        raise SpecValidationError(f"{state} requires outcome='failure'")
    if state in {"quick_verified", "release_certified"} and record["outcome"] != "success":
        raise SpecValidationError(f"{state} requires outcome='success'")
    if state == "release_certified":
        missing = [
            name for name in _RELEASE_FINGERPRINT_FIELDS if record["fingerprint"][name] is None
        ]
        if missing:
            raise SpecValidationError(
                "release_certified requires a complete fingerprint: " + ", ".join(missing)
            )
    _parse_timestamp(record["recorded_at"])


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SpecValidationError(f"invalid verification timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise SpecValidationError("verification timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _latest_matching(
    records: Sequence[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    matches = [record for record in records if predicate(record)]
    return (
        max(
            matches,
            key=lambda record: (
                _parse_timestamp(record["recorded_at"]),
                int(record["sequence"]),
            ),
        )
        if matches
        else None
    )


def _format_record(value: Any, success_label: str) -> str:
    if not isinstance(value, Mapping):
        return "—"
    subject = value.get("subject")
    subject_id = str(subject.get("id", "")) if isinstance(subject, Mapping) else ""
    if value.get("outcome") == "failure":
        failure = value.get("failure")
        code = failure.get("code") if isinstance(failure, Mapping) else "FAILED"
        return f"FAILED {subject_id[:12]} {code}"
    return f"{success_label} {subject_id[:12]} {value.get('state')}"


def _render_html(projection: Mapping[str, Any]) -> str:
    strategy = projection.get("strategy")
    if not isinstance(strategy, Mapping):
        raise BenchmarkError("verification projection is missing strategy status")
    rows = []
    for label, key, success_label in (
        ("Latest checked", "latest_checked", "CHECKED"),
        ("Quick verified", "quick_verified", "VERIFIED"),
        ("Release certified", "release_certified", "CERTIFIED"),
    ):
        rows.append(
            "<tr>"
            f"<th>{html.escape(label)}</th>"
            f"<td>{html.escape(_format_record(strategy.get(key), success_label))}</td>"
            "</tr>"
        )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Verification status</title>"
        "<style>body{font:15px system-ui;margin:2rem;max-width:58rem}"
        "table{border-collapse:collapse;width:100%}th,td{padding:.7rem;"
        "border-bottom:1px solid #ddd;text-align:left}th{width:12rem}</style>"
        "<h1>Verification status</h1>"
        "<p>Latest compatibility, quick parity, and Full certification are independent.</p>"
        f"<table>{''.join(rows)}</table>"
        f"<p>Tracked runs: {len(projection.get('runs', []))}</p>"
        "</html>\n"
    )
