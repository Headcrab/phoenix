from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TASK_STATUSES = {
    "queued",
    "running",
    "executor_failed",
    "validation_failed",
    "git_failed",
    "waiting_ci",
    "restart_failed",
    "rolled_back",
    "completed",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class TaskRepository:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    instruction TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    branch_name TEXT,
                    commit_sha TEXT,
                    pr_number INTEGER,
                    pr_url TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );

                CREATE TABLE IF NOT EXISTS subagents (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    activity TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def create_task(
        self,
        instruction: str,
        priority: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            if idempotency_key:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return dict(row)
            task_id = str(uuid.uuid4())
            now = _utc_now()
            conn.execute(
                """
                INSERT INTO tasks (
                    id, instruction, priority, status, idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, instruction, priority, "queued", idempotency_key, now, now),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, message, created_at) VALUES (?, ?, ?)",
                (task_id, "Task created", now),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_tasks_by_status(self, status: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at ASC",
                (status,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        if "status" in fields and fields["status"] not in TASK_STATUSES:
            raise ValueError(f"Unsupported status: {fields['status']}")
        if not fields:
            task = self.get_task(task_id)
            if not task:
                raise KeyError(task_id)
            return task
        fields["updated_at"] = _utc_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [task_id]
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values)
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise KeyError(task_id)
        return dict(row)

    def append_event(self, task_id: str, message: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, message, created_at) VALUES (?, ?, ?)",
                (task_id, message, _utc_now()),
            )

    def get_events(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, task_id, message, created_at
                FROM task_events
                WHERE task_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_subagent(
        self,
        subagent_id: str,
        kind: str,
        task_id: str,
        status: str,
        activity: str,
        details: str = "",
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subagents (
                    id, kind, task_id, status, activity, details, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    task_id=excluded.task_id,
                    status=excluded.status,
                    activity=excluded.activity,
                    details=excluded.details,
                    updated_at=excluded.updated_at
                """,
                (subagent_id, kind, task_id, status, activity, details, now, now),
            )
            row = conn.execute("SELECT * FROM subagents WHERE id = ?", (subagent_id,)).fetchone()
        if not row:
            raise KeyError(subagent_id)
        return dict(row)

    def get_subagent(self, subagent_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM subagents WHERE id = ?", (subagent_id,)).fetchone()
        return dict(row) if row else None

    def list_subagents(self, limit: int = 100, active_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM subagents"
        params: list[Any] = []
        if active_only:
            query += " WHERE status IN ('queued', 'running', 'waiting')"
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

