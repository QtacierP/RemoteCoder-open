"""Conversation history recorder for backend raw streams and Telegram replies."""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from pathlib import Path

_REPLY_NOISE_PATTERNS = [
    re.compile(r"^\s*Session\s+[`'\"]?[0-9a-f-]+[`'\"]?\s*\((?:codex_cli_session|claude_code_cli_session|codex_sdk)\)\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*>?_\s*OpenAI Codex\b", flags=re.IGNORECASE),
    re.compile(r"^\s*OpenAI Codex\b", flags=re.IGNORECASE),
    re.compile(r"^\s*>?_\s*Claude Code\b", flags=re.IGNORECASE),
    re.compile(r"^\s*Claude Code\b", flags=re.IGNORECASE),
    re.compile(r"^\s*Tip:\s+", flags=re.IGNORECASE),
    re.compile(r"^\s*model:\s+", flags=re.IGNORECASE),
    re.compile(r"^\s*directory:\s+", flags=re.IGNORECASE),
    re.compile(r"^\s*gpt-[\w\-.]+\s+default\b", flags=re.IGNORECASE),
    re.compile(r"\b\d{1,3}%\s+left\b", flags=re.IGNORECASE),
    re.compile(r"\bcontext left\b", flags=re.IGNORECASE),
    re.compile(r"\btab to queue message\b", flags=re.IGNORECASE),
    re.compile(r"\besc to interrupt\b", flags=re.IGNORECASE),
    re.compile(r"^[╭╰│─\s]+$"),
]


class ConversationHistoryService:
    def __init__(self, history_root: Path) -> None:
        self.history_root = history_root
        self.history_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def persist_turn(
        self,
        *,
        chat_id: int,
        session_id: str,
        user_text: str,
        backend_raw_stream: str,
        telegram_reply: str,
    ) -> None:
        """Persist one conversation turn to markdown for debugging/audit."""
        ts = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        transcript_path = self._transcript_path(chat_id=chat_id, session_id=session_id)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            with transcript_path.open("a", encoding="utf-8") as f:
                if transcript_path.stat().st_size == 0:
                    f.write(f"# Chat {chat_id} / Session {session_id}\n\n")
                f.write(f"## {ts} User\n\n{user_text.strip()}\n\n")
                f.write(f"## {ts} Backend Raw Stream\n\n```text\n{backend_raw_stream.strip()}\n```\n\n")
                f.write(f"## {ts} Telegram Reply\n\n{telegram_reply}\n\n")

    def extract_reply(self, backend_raw_stream: str) -> str:
        """Extract meaningful assistant reply from raw backend output."""
        return self._build_telegram_reply(backend_raw_stream)

    def read_latest_reply(self, *, chat_id: int, session_id: str) -> str:
        replies = self.list_replies(chat_id=chat_id, session_id=session_id)
        if not replies:
            return "(No transcript reply available.)"
        return replies[-1]

    def list_replies(self, *, chat_id: int, session_id: str) -> list[str]:
        return [turn["telegram_reply"] for turn in self.list_recent_turns(chat_id=chat_id, session_id=session_id, limit=10_000)]

    def list_recent_turns(self, *, chat_id: int, session_id: str, limit: int = 4) -> list[dict[str, str]]:
        transcript_path = self._transcript_path(chat_id=chat_id, session_id=session_id)
        if not transcript_path.exists():
            return []
        text = transcript_path.read_text(encoding="utf-8")
        marker = "## "
        lines = text.splitlines()
        blocks: list[tuple[str, str]] = []
        current_heading = ""
        block_start = -1
        for i, line in enumerate(lines):
            if not line.startswith(marker):
                continue
            if current_heading:
                value = "\n".join(lines[block_start:i]).strip()
                blocks.append((current_heading, value))
            current_heading = line[len(marker):].strip()
            block_start = i + 1
        if current_heading:
            blocks.append((current_heading, "\n".join(lines[block_start:]).strip()))

        turns: list[dict[str, str]] = []
        current_user = ""
        for heading, value in blocks:
            if heading.endswith("User"):
                current_user = value
                continue
            if not heading.endswith("Telegram Reply"):
                continue
            turns.append(
                {
                    "user_text": current_user,
                    "telegram_reply": value,
                }
            )
        if limit <= 0:
            return []
        return turns[-limit:]

    def transcript_path(self, *, chat_id: int, session_id: str) -> Path:
        return self._transcript_path(chat_id=chat_id, session_id=session_id)

    def _build_telegram_reply(self, backend_raw_stream: str) -> str:
        lines = [line.strip() for line in backend_raw_stream.splitlines() if line.strip()]
        kept: list[str] = []
        for line in lines:
            if line in {">", "›", "•"}:
                continue
            if any(pattern.search(line) for pattern in _REPLY_NOISE_PATTERNS):
                continue
            kept.append(line)
        cleaned = "\n".join(kept).strip()
        if cleaned:
            return cleaned
        fallback = backend_raw_stream.strip()
        fallback_lines = [line.strip() for line in fallback.splitlines() if line.strip()]
        if fallback_lines and any(not any(pattern.search(line) for pattern in _REPLY_NOISE_PATTERNS) for line in fallback_lines):
            return fallback
        return "(No meaningful response captured from backend output.)"

    def _transcript_path(self, *, chat_id: int, session_id: str) -> Path:
        return self.history_root / f"chat_{chat_id}" / f"session_{session_id}.md"
