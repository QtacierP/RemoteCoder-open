from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.conversation_history import ConversationHistoryService


def test_list_replies_returns_all_telegram_replies_in_order(tmp_path: Path) -> None:
    service = ConversationHistoryService(tmp_path)
    transcript = service.transcript_path(chat_id=7, session_id="session-1")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "# Chat 7 / Session session-1\n\n"
        "## 2026-04-01T00:00:00Z User\n\nhello\n\n"
        "## 2026-04-01T00:00:00Z Telegram Reply\n\nfirst reply\n\n"
        "## 2026-04-01T00:01:00Z User\n\nworld\n\n"
        "## 2026-04-01T00:01:00Z Telegram Reply\n\nsecond reply\n\n",
        encoding="utf-8",
    )

    replies = service.list_replies(chat_id=7, session_id="session-1")

    assert replies == ["first reply", "second reply"]


def test_read_latest_reply_returns_last_reply_block(tmp_path: Path) -> None:
    service = ConversationHistoryService(tmp_path)
    transcript = service.transcript_path(chat_id=7, session_id="session-1")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "# Chat 7 / Session session-1\n\n"
        "## 2026-04-01T00:00:00Z Telegram Reply\n\nolder reply\n\n"
        "## 2026-04-01T00:01:00Z Telegram Reply\n\nlatest reply\n\n",
        encoding="utf-8",
    )

    latest = service.read_latest_reply(chat_id=7, session_id="session-1")

    assert latest == "latest reply"


def test_extract_reply_filters_claude_session_noise(tmp_path: Path) -> None:
    service = ConversationHistoryService(tmp_path)

    reply = service.extract_reply(
        "Session 12345678-1234-1234-1234-123456789abc (claude_code_cli_session)\n"
        "Claude Code\n"
        "model: kimi-k2.5\n"
        "directory: /workspace/project\n"
        "最终答案"
    )

    assert reply == "最终答案"


def test_persist_turn_writes_backend_raw_stream_heading(tmp_path: Path) -> None:
    service = ConversationHistoryService(tmp_path)

    service.persist_turn(
        chat_id=7,
        session_id="session-1",
        user_text="hello",
        backend_raw_stream="raw output",
        telegram_reply="reply text",
    )

    text = service.transcript_path(chat_id=7, session_id="session-1").read_text(encoding="utf-8")
    assert "Backend Raw Stream" in text


def test_list_recent_turns_returns_user_reply_pairs(tmp_path: Path) -> None:
    service = ConversationHistoryService(tmp_path)
    transcript = service.transcript_path(chat_id=7, session_id="session-1")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "# Chat 7 / Session session-1\n\n"
        "## 2026-04-01T00:00:00Z User\n\nfirst user\n\n"
        "## 2026-04-01T00:00:00Z Backend Raw Stream\n\n```text\nraw\n```\n\n"
        "## 2026-04-01T00:00:00Z Telegram Reply\n\nfirst reply\n\n"
        "## 2026-04-01T00:01:00Z User\n\nsecond user\n\n"
        "## 2026-04-01T00:01:00Z Telegram Reply\n\nsecond reply\n\n",
        encoding="utf-8",
    )

    turns = service.list_recent_turns(chat_id=7, session_id="session-1", limit=2)

    assert turns == [
        {"user_text": "first user", "telegram_reply": "first reply"},
        {"user_text": "second user", "telegram_reply": "second reply"},
    ]
