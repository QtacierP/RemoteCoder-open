from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.codex.base import CodexBackend, CodexSessionInfo
from app.db import Database
from app.services.audit_service import AuditService
from app.services.session_service import SessionService
from app.services.workspace_guard import WorkspaceGuard


class FakeBackend(CodexBackend):
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}

    def create_session(self, session_id: str, workspace: Path) -> CodexSessionInfo:
        self.sessions[session_id] = {
            "exists": True,
            "workspace": str(workspace),
            "thread_id": None,
            "timeout_seconds": 120,
            "last_return_code": None,
            "latest_reply_preview": "",
        }
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode="fake")

    def restore_session(self, session_id: str, workspace: Path, backend_state: dict | None = None) -> CodexSessionInfo:
        state = {
            "exists": True,
            "workspace": str(workspace),
            "thread_id": None,
            "timeout_seconds": 120,
            "last_return_code": None,
            "latest_reply_preview": "",
        }
        if backend_state:
            state.update(backend_state)
            state["exists"] = True
            state["workspace"] = str(workspace)
        self.sessions[session_id] = state
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode="fake")

    def send_message(self, session_id: str, message: str) -> str:
        state = self.sessions[session_id]
        state["thread_id"] = "thread-restored"
        state["last_return_code"] = 0
        state["latest_reply_preview"] = f"reply:{message}"
        return f"reply:{message}"

    def get_status(self, session_id: str) -> dict:
        return self.sessions.get(session_id, {"exists": False})

    def reset_session(self, session_id: str, workspace: Path) -> CodexSessionInfo:
        self.close_session(session_id)
        return self.create_session(session_id, workspace)

    def close_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    def set_session_timeout(self, session_id: str, timeout_seconds: int) -> dict:
        self.sessions[session_id]["timeout_seconds"] = timeout_seconds
        return self.sessions[session_id]


def _build_service(tmp_path: Path, backend: FakeBackend) -> SessionService:
    db = Database(tmp_path / "bridge.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    guard = WorkspaceGuard(allowed_roots=[workspace], default_workspace=workspace)
    return SessionService(
        db=db,
        audit_service=AuditService(db),
        workspace_guard=guard,
        backends={"fake": backend},
        default_mode="fake",
    )


def test_session_backend_state_survives_service_restart(tmp_path: Path) -> None:
    backend1 = FakeBackend()
    service1 = _build_service(tmp_path, backend1)

    session, reply = service1.send_chat_message(42, "hello")

    assert reply == "reply:hello"
    persisted = service1.db.get_session(session["session_id"])
    assert persisted is not None
    assert persisted["backend_state"]["thread_id"] == "thread-restored"
    assert persisted["backend_state"]["last_return_code"] == 0

    backend2 = FakeBackend()
    service2 = _build_service(tmp_path, backend2)
    restored = service2.rehydrate_persisted_sessions()

    assert len(restored) == 1
    status = service2.get_session_status(session["session_id"])
    assert status["backend_status"]["exists"] is True
    assert status["backend_status"]["thread_id"] == "thread-restored"
    assert status["backend_status"]["latest_reply_preview"] == "reply:hello"


def test_timeout_override_is_persisted_for_rehydration(tmp_path: Path) -> None:
    backend1 = FakeBackend()
    service1 = _build_service(tmp_path, backend1)

    session = service1.get_or_create_chat_session(7)
    service1.set_chat_timeout(7, 300)

    backend2 = FakeBackend()
    service2 = _build_service(tmp_path, backend2)
    service2.rehydrate_persisted_sessions()

    status = service2.get_session_status(session["session_id"])
    assert status["backend_status"]["timeout_seconds"] == 300
