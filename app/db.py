"""SQLite persistence for sessions and audit logs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    integration_mode TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    backend_state TEXT NOT NULL DEFAULT '{}',
                    workspace_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_session_map (
                    chat_id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    chat_id INTEGER,
                    session_id TEXT,
                    payload TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shell_sessions (
                    chat_id INTEGER PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    conda_env TEXT,
                    last_exit_code INTEGER,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shell_jobs (
                    chat_id INTEGER NOT NULL,
                    job_id INTEGER NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    command TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    notified_done INTEGER NOT NULL DEFAULT 0,
                    return_code INTEGER,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, job_id)
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_chat_id ON sessions(chat_id);
                CREATE INDEX IF NOT EXISTS idx_audit_chat_id ON audit_logs(chat_id);
                CREATE INDEX IF NOT EXISTS idx_audit_session_id ON audit_logs(session_id);
                CREATE INDEX IF NOT EXISTS idx_shell_jobs_chat_id ON shell_jobs(chat_id);
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "label" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN label TEXT NOT NULL DEFAULT ''")
            if "backend_state" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN backend_state TEXT NOT NULL DEFAULT '{}'")

    @staticmethod
    def _deserialize_session_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        raw_backend_state = record.get("backend_state")
        if isinstance(raw_backend_state, str) and raw_backend_state.strip():
            try:
                record["backend_state"] = json.loads(raw_backend_state)
            except json.JSONDecodeError:
                record["backend_state"] = {}
        elif isinstance(raw_backend_state, dict):
            record["backend_state"] = raw_backend_state
        else:
            record["backend_state"] = {}
        return record

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_session(self, record: dict[str, Any]) -> None:
        payload = dict(record)
        payload["backend_state"] = json.dumps(payload.get("backend_state", {}), ensure_ascii=False)
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, chat_id, integration_mode, label, backend_state, workspace_path, status, created_at, updated_at
                )
                VALUES (
                    :session_id, :chat_id, :integration_mode, :label, :backend_state, :workspace_path, :status, :created_at, :updated_at
                )
                """,
                payload,
            )
            conn.execute(
                """
                INSERT INTO chat_session_map (chat_id, session_id, updated_at)
                VALUES (:chat_id, :session_id, :updated_at)
                ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id, updated_at=excluded.updated_at
                """,
                {
                    "chat_id": record["chat_id"],
                    "session_id": record["session_id"],
                    "updated_at": record["updated_at"],
                },
            )

    def get_chat_session(self, chat_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT s.*
                FROM sessions s
                JOIN chat_session_map m ON s.session_id = m.session_id
                WHERE m.chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            return self._deserialize_session_row(row)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            return self._deserialize_session_row(row)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
            return [item for row in rows if (item := self._deserialize_session_row(row)) is not None]

    def list_current_chat_sessions(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT s.*
                FROM sessions s
                JOIN chat_session_map m ON s.session_id = m.session_id
                ORDER BY m.updated_at DESC
                """
            ).fetchall()
            return [item for row in rows if (item := self._deserialize_session_row(row)) is not None]

    def update_session_status(self, session_id: str, status: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                (status, self.now_iso(), session_id),
            )

    def update_session_label(self, session_id: str, label: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE sessions SET label = ?, updated_at = ? WHERE session_id = ?",
                (label, self.now_iso(), session_id),
            )

    def update_session_backend_state(self, session_id: str, backend_state: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE sessions SET backend_state = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(backend_state, ensure_ascii=False), self.now_iso(), session_id),
            )

    def update_chat_mapping(self, chat_id: int, session_id: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO chat_session_map (chat_id, session_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id, updated_at=excluded.updated_at
                """,
                (chat_id, session_id, self.now_iso()),
            )

    def add_audit_log(self, event_type: str, chat_id: int | None, session_id: str | None, payload: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (event_type, chat_id, session_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_type, chat_id, session_id, payload, self.now_iso()),
            )

    def recent_audit_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def upsert_shell_session(
        self,
        *,
        chat_id: int,
        cwd: str,
        conda_env: str | None,
        last_exit_code: int | None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO shell_sessions (chat_id, cwd, conda_env, last_exit_code, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    cwd=excluded.cwd,
                    conda_env=excluded.conda_env,
                    last_exit_code=excluded.last_exit_code,
                    updated_at=excluded.updated_at
                """,
                (chat_id, cwd, conda_env, last_exit_code, self.now_iso()),
            )

    def delete_shell_session(self, chat_id: int) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM shell_sessions WHERE chat_id = ?", (chat_id,))

    def list_shell_sessions(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM shell_sessions ORDER BY updated_at DESC").fetchall()
            return [dict(row) for row in rows]

    def upsert_shell_job(
        self,
        *,
        chat_id: int,
        job_id: int,
        label: str,
        command: str,
        cwd: str,
        log_path: str,
        pid: int,
        started_at: float,
        notified_done: bool,
        return_code: int | None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO shell_jobs (
                    chat_id, job_id, label, command, cwd, log_path, pid, started_at, notified_done, return_code, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, job_id) DO UPDATE SET
                    label=excluded.label,
                    command=excluded.command,
                    cwd=excluded.cwd,
                    log_path=excluded.log_path,
                    pid=excluded.pid,
                    started_at=excluded.started_at,
                    notified_done=excluded.notified_done,
                    return_code=excluded.return_code,
                    updated_at=excluded.updated_at
                """,
                (
                    chat_id,
                    job_id,
                    label,
                    command,
                    cwd,
                    log_path,
                    pid,
                    started_at,
                    1 if notified_done else 0,
                    return_code,
                    self.now_iso(),
                ),
            )

    def delete_shell_jobs(self, chat_id: int) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM shell_jobs WHERE chat_id = ?", (chat_id,))

    def list_shell_jobs(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM shell_jobs ORDER BY chat_id, job_id").fetchall()
            records = [dict(row) for row in rows]
            for record in records:
                record["notified_done"] = bool(record.get("notified_done"))
            return records
