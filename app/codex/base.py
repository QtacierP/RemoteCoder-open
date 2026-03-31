"""Codex backend abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CodexSessionInfo:
    session_id: str
    workspace: Path
    mode: str


class CodexBackend(ABC):
    @abstractmethod
    def create_session(self, session_id: str, workspace: Path) -> CodexSessionInfo: ...

    @abstractmethod
    def send_message(self, session_id: str, message: str) -> str: ...

    @abstractmethod
    def get_status(self, session_id: str) -> dict: ...

    @abstractmethod
    def reset_session(self, session_id: str, workspace: Path) -> CodexSessionInfo: ...

    @abstractmethod
    def close_session(self, session_id: str) -> None: ...
