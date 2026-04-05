from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.codex.cli_session import CodexCliSessionBackend

WORKSPACE = Path("/tmp/project")


def test_cli_backend_trace_status_exposes_recent_events() -> None:
    backend = CodexCliSessionBackend("codex", "-a never", timeout_seconds=600)
    backend.create_session("session-1", WORKSPACE)
    state = backend.sessions["session-1"]

    backend._mark_trace_started(state, "inspect repository")
    backend._record_stdout_line(state, '{"type":"thread.started","thread_id":"thread-123"}\n')
    backend._record_stdout_line(state, '{"type":"exec.command","command":"git status"}\n')
    backend._record_stderr_line(state, "warning line\n")
    backend._mark_trace_finished(state, "done", 0)

    status = backend.get_status("session-1")

    assert status["running"] is False
    assert status["thread_id"] == "thread-123"
    assert status["event_count"] == 2
    assert any("thread.started" in item for item in status["recent_events"])
    assert any("git status" in item for item in status["recent_events"])
    assert status["stderr_lines"][-1] == "warning line"
    assert status["latest_reply_preview"] == "done"


def test_cli_backend_supports_unlimited_timeout_override() -> None:
    backend = CodexCliSessionBackend("codex", "-a never", timeout_seconds=600)
    backend.create_session("session-1", WORKSPACE)

    backend.set_session_timeout("session-1", -1)
    status = backend.get_status("session-1")

    assert status["timeout_seconds"] == -1


def test_cli_backend_restores_unlimited_timeout_override() -> None:
    backend = CodexCliSessionBackend("codex", "-a never", timeout_seconds=600)
    backend.restore_session(
        "session-1",
        WORKSPACE,
        backend_state={"timeout_seconds": -1},
    )

    status = backend.get_status("session-1")

    assert status["timeout_seconds"] == -1


def test_cli_backend_switches_provider_and_exposes_status() -> None:
    backend = CodexCliSessionBackend("codex", "-a never", timeout_seconds=600)
    backend.create_session("session-1", WORKSPACE)

    backend.set_session_provider(
        "session-1",
        {
            "label": "relay",
            "model": "gpt-5.4",
            "base_url": "https://relay.example/v1",
            "api_key": "secret-token",
        },
    )
    status = backend.get_status("session-1")

    assert status["provider_label"] == "relay"
    assert status["provider_model"] == "gpt-5.4"
    assert status["provider_base_url"] == "https://relay.example/v1"
    assert status["thread_id"] is None
