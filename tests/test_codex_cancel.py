from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.codex.cli_session import CodexCliSessionBackend


class _FakeProc:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.terminated = False

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True


def test_cancel_running_reply_terminates_active_process(tmp_path: Path) -> None:
    backend = CodexCliSessionBackend("codex", "--search", timeout_seconds=600)
    backend.create_session("session-1", tmp_path)
    state = backend.sessions["session-1"]
    fake_proc = _FakeProc()

    state.running = True
    state.active_process = fake_proc
    state.active_pid = fake_proc.pid

    result = backend.cancel_running_reply("session-1")

    assert result["ok"] is True
    assert result["pid"] == 4321
    assert state.cancel_requested is True
    assert fake_proc.terminated is True


def test_cancel_running_reply_reports_not_running(tmp_path: Path) -> None:
    backend = CodexCliSessionBackend("codex", "--search", timeout_seconds=600)
    backend.create_session("session-1", tmp_path)

    result = backend.cancel_running_reply("session-1")

    assert result["ok"] is False
    assert result["reason"] == "not_running"
