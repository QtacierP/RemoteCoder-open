"""Pydantic schemas for request/response payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class TelegramInboundMessage(BaseModel):
    update_id: int
    chat_id: int
    message_id: int
    text: str
    username: str | None = None


class SessionRecord(BaseModel):
    session_id: str
    chat_id: int
    integration_mode: Literal["codex_cli_session", "codex_sdk"]
    workspace_path: str
    status: str
    created_at: datetime
    updated_at: datetime


class SessionStatusResponse(BaseModel):
    session: SessionRecord
    backend_status: dict


class HealthResponse(BaseModel):
    status: str
    telegram_mode: str


class ChatResponse(BaseModel):
    chat_id: int
    session: SessionRecord | None
