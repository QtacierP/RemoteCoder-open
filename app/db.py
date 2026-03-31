"""SQLite persistence for sessions and audit logs."""

from __future__ import annotations

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

                CREATE INDEX IF NOT EXISTS idx_sessions_chat_id ON sessions(chat_id);
                CREATE INDEX IF NOT EXISTS idx_audit_chat_id ON audit_logs(chat_id);
                CREATE INDEX IF NOT EXISTS idx_audit_session_id ON audit_logs(session_id);
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "label" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN label TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_session(self, record: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, chat_id, integration_mode, label, workspace_path, status, created_at, updated_at)
                VALUES (:session_id, :chat_id, :integration_mode, :label, :workspace_path, :status, :created_at, :updated_at)
                """,
                record,
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
            return dict(row) if row else None

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            return dict(row) if row else None

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
            return [dict(row) for row in rows]

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
