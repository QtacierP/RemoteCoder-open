"""Codex SDK integration placeholder.

Implemented: typed provider and lifecycle surface.
Not implemented: real network/API calls to Codex SDK.
"""

from __future__ import annotations

from pathlib import Path

from app.codex.base import CodexBackend, CodexSessionInfo


class CodexSdkBackend(CodexBackend):
    def __init__(self) -> None:
        self.sessions: dict[str, Path] = {}

    def create_session(self, session_id: str, workspace: Path) -> CodexSessionInfo:
        self.sessions[session_id] = workspace
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode="codex_sdk")

    def send_message(self, session_id: str, message: str) -> str:
        if session_id not in self.sessions:
            raise RuntimeError(f"SDK session {session_id} does not exist")
        return (
            "Codex SDK mode is a stub in this MVP. "
            "Implement provider calls in app/codex/sdk_mode.py::send_message. "
            f"Received message: {message[:250]}"
        )

    def get_status(self, session_id: str) -> dict:
        workspace = self.sessions.get(session_id)
        return {"exists": session_id in self.sessions, "workspace": str(workspace) if workspace else None, "stub": True}

    def reset_session(self, session_id: str, workspace: Path) -> CodexSessionInfo:
        self.sessions[session_id] = workspace
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode="codex_sdk")

    def close_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
