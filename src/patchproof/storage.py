"""SQLite-backed durable state and tamper-evident event evidence.

The database is intentionally boring: one short-lived sqlite3 connection per
operation, WAL mode, and JSON for extensible task payloads. The important
invariant is that an event's hash covers its canonical payload and the hash of
the previous event in the same task. A database edit is therefore detectable
by ``verify_chain`` even though SQLite itself is not a write-once store.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ApprovalSnapshot, ReceiptSnapshot, TaskEvent, TaskStatus

GENESIS_HASH = "0" * 64

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    status TEXT NOT NULL,
    current_stage TEXT NOT NULL,
    iteration INTEGER NOT NULL DEFAULT 0,
    max_iterations INTEGER NOT NULL,
    max_steps INTEGER NOT NULL DEFAULT 32,
    check_command TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT,
    failure_category TEXT,
    plan_json TEXT,
    test_result_json TEXT,
    diff TEXT NOT NULL DEFAULT '',
    changed_files_json TEXT NOT NULL DEFAULT '[]',
    pending_command_json TEXT,
    pending_approval_id TEXT,
    pending_risk TEXT,
    pending_reason TEXT,
    workspace_kind TEXT,
    workspace_reason TEXT,
    workspace_baseline_json TEXT NOT NULL DEFAULT '{}',
    tool_calls INTEGER NOT NULL DEFAULT 0,
    invalid_actions INTEGER NOT NULL DEFAULT 0,
    budget_used INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT NOT NULL DEFAULT '{}',
    required_check_argv_json TEXT NOT NULL DEFAULT '[]',
    required_check_verified INTEGER NOT NULL DEFAULT 0,
    required_check_evidence_generation INTEGER,
    edit_generation INTEGER NOT NULL DEFAULT 0,
    required_check_last_result_json TEXT,
    precondition_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    argv_json TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    resolved_at TEXT,
    approved INTEGER,
    event_seq INTEGER
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    path TEXT,
    sha256 TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    receipt_hash TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmark_runs (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    variant TEXT NOT NULL,
    status TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    report_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id, requested_at);
CREATE INDEX IF NOT EXISTS idx_benchmark_case ON benchmark_runs(case_id, variant);
"""


def utc_iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class SQLiteStore:
    """Durable repository for task state, events, approvals and receipts."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._memory_uri = (
            f"file:patchproof-{id(self)}?mode=memory&cache=shared" if self.db_path == ":memory:" else None
        )
        self._memory_keeper: sqlite3.Connection | None = None
        self.initialize()

    def _raw_connect(self) -> sqlite3.Connection:
        if self._memory_uri:
            connection = sqlite3.connect(self._memory_uri, uri=True, timeout=30, check_same_thread=False)
        else:
            connection = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if not self._memory_uri:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self._memory_uri and self._memory_keeper is None:
            self._memory_keeper = self._raw_connect()
            self._memory_keeper.executescript(SCHEMA)
        connection = self._memory_keeper if self._memory_uri else self._raw_connect()
        assert connection is not None
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if not self._memory_uri:
                connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            self._ensure_task_columns(connection)

    @staticmethod
    def _ensure_task_columns(connection: sqlite3.Connection) -> None:
        """Migrate databases created by v0.2 without losing task history."""

        existing = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
        migrations = {
            "required_check_argv_json": "TEXT NOT NULL DEFAULT '[]'",
            "required_check_verified": "INTEGER NOT NULL DEFAULT 0",
            "required_check_evidence_generation": "INTEGER",
            "edit_generation": "INTEGER NOT NULL DEFAULT 0",
            "required_check_last_result_json": "TEXT",
            "precondition_failures": "INTEGER NOT NULL DEFAULT 0",
            "provider_json": "TEXT",
        }
        for name, definition in migrations.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")

    def close(self) -> None:
        if self._memory_keeper is not None:
            self._memory_keeper.close()
            self._memory_keeper = None

    def create_task(
        self,
        *,
        task_id: str,
        goal: str,
        repo_path: str,
        check_command: str,
        max_iterations: int,
        max_steps: int,
        required_check_argv: list[str] | None = None,
        status: TaskStatus = TaskStatus.QUEUED,
        provider: dict[str, Any] | None = None,
    ) -> None:
        now = utc_iso()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO tasks
                (id, goal, repo_path, status, current_stage, iteration, max_iterations,
                 max_steps, check_command, created_at, updated_at, required_check_argv_json,
                 provider_json)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    goal,
                    repo_path,
                    status.value,
                    status.value,
                    max_iterations,
                    max_steps,
                    check_command,
                    now,
                    now,
                    json_dumps(required_check_argv or []),
                    json_dumps(provider) if provider else None,
                ),
            )

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        with self.connection() as connection:
            return connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def list_tasks(self) -> list[sqlite3.Row]:
        with self.connection() as connection:
            return connection.execute("SELECT * FROM tasks ORDER BY updated_at DESC, created_at DESC").fetchall()

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {
            "status", "current_stage", "iteration", "max_iterations", "max_steps", "updated_at", "error",
            "failure_category", "plan_json", "test_result_json", "diff", "changed_files_json",
            "pending_command_json", "pending_approval_id", "pending_risk", "pending_reason", "workspace_kind",
            "workspace_reason", "workspace_baseline_json", "tool_calls", "invalid_actions", "budget_used", "usage_json",
            "required_check_argv_json", "required_check_verified", "required_check_evidence_generation",
            "edit_generation", "required_check_last_result_json",
            "precondition_failures",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不允许更新的 task 字段: {sorted(unknown)}")
        if not fields:
            return
        fields.setdefault("updated_at", utc_iso())
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [task_id]
        with self.connection() as connection:
            cursor = connection.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values)
            if cursor.rowcount != 1:
                raise KeyError(task_id)

    def recover_running_tasks(self) -> list[str]:
        statuses = tuple(item.value for item in {
            TaskStatus.QUEUED,
            TaskStatus.INSPECTING,
            TaskStatus.PLANNING,
            TaskStatus.EDITING,
            TaskStatus.TESTING,
            TaskStatus.REPAIRING,
            TaskStatus.AWAITING_COMMAND_APPROVAL,
        })
        placeholders = ",".join("?" for _ in statuses)
        now = utc_iso()
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT id FROM tasks WHERE status IN ({placeholders})", statuses
            ).fetchall()
            ids = [row["id"] for row in rows]
            connection.execute(
                f"""
                UPDATE tasks
                SET status = ?, current_stage = ?, error = ?, failure_category = ?, updated_at = ?
                WHERE status IN ({placeholders})
                """,
                (
                    TaskStatus.INTERRUPTED.value,
                    TaskStatus.INTERRUPTED.value,
                    "PatchProof 进程重启，任务没有可恢复的运行上下文",
                    "process_interrupted",
                    now,
                    *statuses,
                ),
            )
        for task_id in ids:
            self.append_event(
                task_id,
                stage=TaskStatus.INTERRUPTED.value,
                message="任务因进程重启被标记为 interrupted",
                data={"failure_category": "process_interrupted", "recoverable": True},
            )
        return ids

    def append_event(self, task_id: str, *, stage: str, message: str, data: dict[str, Any] | None = None) -> TaskEvent:
        data = data or {}
        timestamp = utc_iso()
        with self.connection() as connection:
            previous = connection.execute(
                "SELECT seq, event_hash FROM events WHERE task_id = ? ORDER BY seq DESC LIMIT 1", (task_id,)
            ).fetchone()
            seq = int(previous["seq"] + 1) if previous else 1
            prev_hash = previous["event_hash"] if previous else GENESIS_HASH
            payload = {
                "task_id": task_id,
                "seq": seq,
                "ts": timestamp,
                "stage": stage,
                "message": message,
                "data": data,
            }
            canonical_payload = json_dumps(payload)
            event_hash = hashlib.sha256(f"{prev_hash}:{canonical_payload}".encode()).hexdigest()
            connection.execute(
                """
                INSERT INTO events(task_id, seq, ts, stage, message, payload_json, prev_hash, event_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, seq, timestamp, stage, message, canonical_payload, prev_hash, event_hash),
            )
        return TaskEvent(
            seq=seq,
            ts=parse_datetime(timestamp),
            stage=stage,
            message=message,
            data=data,
            prev_hash=prev_hash,
            event_hash=event_hash,
            canonical_payload=canonical_payload,
        )

    def get_events(self, task_id: str, after: int = 0) -> list[TaskEvent]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE task_id = ? AND seq > ? ORDER BY seq", (task_id, after)
            ).fetchall()
        return [
            TaskEvent(
                seq=row["seq"],
                ts=parse_datetime(row["ts"]),
                stage=row["stage"],
                message=row["message"],
                data=json_loads(row["payload_json"], {}).get("data", {}),
                prev_hash=row["prev_hash"],
                event_hash=row["event_hash"],
                canonical_payload=row["payload_json"],
            )
            for row in rows
        ]

    def chain_head(self, task_id: str) -> str:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT event_hash FROM events WHERE task_id = ? ORDER BY seq DESC LIMIT 1", (task_id,)
            ).fetchone()
        return row["event_hash"] if row else GENESIS_HASH

    def verify_chain(self, task_id: str) -> bool:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY seq", (task_id,)
            ).fetchall()
        previous = GENESIS_HASH
        for expected_seq, row in enumerate(rows, start=1):
            if row["seq"] != expected_seq or row["prev_hash"] != previous:
                return False
            canonical = row["payload_json"]
            payload = json_loads(canonical, {})
            if (
                payload.get("task_id") != task_id
                or payload.get("seq") != row["seq"]
                or payload.get("ts") != row["ts"]
                or payload.get("stage") != row["stage"]
                or payload.get("message") != row["message"]
            ):
                return False
            expected = hashlib.sha256(f"{previous}:{canonical}".encode()).hexdigest()
            if expected != row["event_hash"]:
                return False
            previous = row["event_hash"]
        return True

    def create_approval(
        self,
        task_id: str,
        *,
        kind: str,
        argv: list[str],
        risk_level: str,
        reason: str,
    ) -> ApprovalSnapshot:
        approval_id = uuid.uuid4().hex[:16]
        requested_at = utc_iso()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO approvals(id, task_id, kind, argv_json, risk_level, reason, requested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (approval_id, task_id, kind, json_dumps(argv), risk_level, reason, requested_at),
            )
        return ApprovalSnapshot(
            id=approval_id,
            task_id=task_id,
            kind=kind,
            argv=argv,
            risk_level=risk_level,
            reason=reason,
            requested_at=parse_datetime(requested_at),
        )

    def resolve_approval(self, approval_id: str, approved: bool, event_seq: int | None = None) -> ApprovalSnapshot:
        resolved_at = utc_iso()
        with self.connection() as connection:
            connection.execute(
                "UPDATE approvals SET approved = ?, resolved_at = ?, event_seq = ? WHERE id = ?",
                (int(approved), resolved_at, event_seq, approval_id),
            )
        snapshot = self.get_approval(approval_id)
        if snapshot is None:
            raise KeyError(approval_id)
        return snapshot

    def get_approval(self, approval_id: str) -> ApprovalSnapshot | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return self._approval_from_row(row) if row else None

    def get_approvals(self, task_id: str) -> list[ApprovalSnapshot]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM approvals WHERE task_id = ? ORDER BY requested_at, id", (task_id,)
            ).fetchall()
        return [self._approval_from_row(row) for row in rows]

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> ApprovalSnapshot:
        return ApprovalSnapshot(
            id=row["id"],
            task_id=row["task_id"],
            kind=row["kind"],
            argv=json_loads(row["argv_json"], []),
            risk_level=row["risk_level"],
            reason=row["reason"],
            requested_at=parse_datetime(row["requested_at"]),
            resolved_at=parse_datetime(row["resolved_at"]) if row["resolved_at"] else None,
            approved=bool(row["approved"]) if row["approved"] is not None else None,
            event_seq=row["event_seq"],
        )

    def save_artifact(
        self,
        task_id: str,
        *,
        kind: str,
        path: str | None,
        sha256: str | None,
        metadata: dict[str, Any],
    ) -> str:
        artifact_id = uuid.uuid4().hex[:16]
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(id, task_id, kind, path, sha256, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, task_id, kind, path, sha256, json_dumps(metadata), utc_iso()),
            )
        return artifact_id

    def get_artifacts(self, task_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM artifacts WHERE task_id = ?"
        parameters: list[Any] = [task_id]
        if kind is not None:
            query += " AND kind = ?"
            parameters.append(kind)
        query += " ORDER BY rowid DESC"
        with self.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "path": row["path"],
                "sha256": row["sha256"],
                "metadata": json_loads(row["metadata_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def save_receipt(self, task_id: str, receipt: dict[str, Any]) -> ReceiptSnapshot:
        receipt_hash = receipt.get("receipt_hash") or ""
        if not receipt_hash:
            raise ValueError("receipt 缺少 receipt_hash")
        now = utc_iso()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO receipts(task_id, receipt_hash, receipt_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    receipt_hash = excluded.receipt_hash,
                    receipt_json = excluded.receipt_json,
                    updated_at = excluded.updated_at
                """,
                (task_id, receipt_hash, json_dumps(receipt), now, now),
            )
        artifact = self.get_artifacts(task_id, kind="patch_receipt")[:1]
        artifact_row = artifact[0] if artifact else {}
        file_verified = False
        if artifact_row.get("path"):
            from .receipt import verify_receipt_file

            file_verified = verify_receipt_file(artifact_row["path"], artifact_row.get("sha256"))
        return ReceiptSnapshot(
            receipt_hash=receipt_hash,
            receipt=receipt,
            verified=True,
            artifact_path=artifact_row.get("path"),
            file_sha256=artifact_row.get("sha256"),
            file_verified=file_verified,
        )

    def get_receipt(self, task_id: str) -> ReceiptSnapshot | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM receipts WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            return None
        receipt = json_loads(row["receipt_json"], {})
        artifact = self.get_artifacts(task_id, kind="patch_receipt")[:1]
        artifact_row = artifact[0] if artifact else {}
        file_verified = False
        if artifact_row.get("path"):
            from .receipt import verify_receipt_file

            file_verified = verify_receipt_file(artifact_row["path"], artifact_row.get("sha256"))
        return ReceiptSnapshot(
            receipt_hash=row["receipt_hash"],
            receipt=receipt,
            verified=True,
            artifact_path=artifact_row.get("path"),
            file_sha256=artifact_row.get("sha256"),
            file_verified=file_verified,
        )

    def create_benchmark_run(self, case_id: str, variant: str, *, status: str = "running") -> str:
        run_id = uuid.uuid4().hex[:16]
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO benchmark_runs(id, case_id, variant, status, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, case_id, variant, status, utc_iso()),
            )
        return run_id

    def finish_benchmark_run(
        self,
        run_id: str,
        *,
        status: str,
        metrics: dict[str, Any],
        report: dict[str, Any],
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE benchmark_runs
                SET status = ?, metrics_json = ?, report_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, json_dumps(metrics), json_dumps(report), utc_iso(), run_id),
            )

    def list_benchmark_runs(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM benchmark_runs ORDER BY started_at DESC").fetchall()
        return [
            {
                "id": row["id"],
                "case_id": row["case_id"],
                "variant": row["variant"],
                "status": row["status"],
                "metrics": json_loads(row["metrics_json"], {}),
                "report": json_loads(row["report_json"], {}),
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
            }
            for row in rows
        ]
