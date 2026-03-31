from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.codex.cli_session import CodexCliSessionBackend


def test_cli_backend_trace_status_exposes_recent_events(tmp_path: Path) -> None:
    backend = CodexCliSessionBackend("codex", "-a never", timeout_seconds=600)
    backend.create_session("session-1", tmp_path)
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
