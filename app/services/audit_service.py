"""Audit event recorder."""

from __future__ import annotations

import json
from typing import Any

from app.db import Database


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def log(self, event_type: str, chat_id: int | None, session_id: str | None, payload: dict[str, Any]) -> None:
        self.db.add_audit_log(event_type, chat_id, session_id, json.dumps(payload, ensure_ascii=False))
