from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.codex.base import CodexBackend, CodexReplyCancelled, CodexSessionInfo
from app.db import Database
from app.services.audit_service import AuditService
from app.services.session_service import SessionService
from app.services.workspace_guard import WorkspaceGuard


class FakeBackend(CodexBackend):
    def __init__(self, mode: str = "fake") -> None:
        self.mode = mode
        self.sessions: dict[str, dict] = {}

    def create_session(self, session_id: str, workspace: Path) -> CodexSessionInfo:
        self.sessions[session_id] = {
            "exists": True,
            "workspace": str(workspace),
            "thread_id": None,
            "timeout_seconds": 120,
            "last_return_code": None,
            "latest_reply_preview": "",
            "provider_label": "default",
            "provider_model": None,
            "provider_base_url": None,
        }
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode=self.mode)

    def restore_session(self, session_id: str, workspace: Path, backend_state: dict | None = None) -> CodexSessionInfo:
        state = {
            "exists": True,
            "workspace": str(workspace),
            "thread_id": None,
            "timeout_seconds": 120,
            "last_return_code": None,
            "latest_reply_preview": "",
            "provider_label": "default",
            "provider_model": None,
            "provider_base_url": None,
        }
        if backend_state:
            state.update(backend_state)
            state["exists"] = True
            state["workspace"] = str(workspace)
        self.sessions[session_id] = state
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode=self.mode)

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

    def set_session_provider(self, session_id: str, provider: dict | None) -> dict:
        state = self.sessions[session_id]
        if provider is None:
            state["provider_label"] = "default"
            state["provider_model"] = None
            state["provider_base_url"] = None
            state["thread_id"] = None
        else:
            state["provider_label"] = provider["label"]
            state["provider_model"] = provider["model"]
            state["provider_base_url"] = provider["base_url"]
            state["thread_id"] = None
        return state


def _build_service(
    tmp_path: Path,
    backend: FakeBackend,
    *,
    claude_backend: FakeBackend | None = None,
    default_mode: str = "codex_cli_session",
    default_claude_code_provider_label: str = "",
) -> SessionService:
    db = Database(tmp_path / "bridge.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    guard = WorkspaceGuard(allowed_roots=[workspace], default_workspace=workspace)
    claude = claude_backend or FakeBackend(mode="claude_code_cli_session")
    return SessionService(
        db=db,
        audit_service=AuditService(db),
        workspace_guard=guard,
        backends={
            "codex_cli_session": backend,
            "claude_code_cli_session": claude,
        },
        default_mode=default_mode,
        default_claude_code_provider_label=default_claude_code_provider_label,
    )


class CancelBackend(FakeBackend):
    def send_message(self, session_id: str, message: str) -> str:
        state = self.sessions[session_id]
        state["thread_id"] = "thread-cancelled"
        state["last_return_code"] = 143
        state["latest_reply_preview"] = ""
        raise CodexReplyCancelled("Codex reply cancelled by user.")


class ErrorBackend(FakeBackend):
    def send_message(self, session_id: str, message: str) -> str:
        state = self.sessions[session_id]
        state["thread_id"] = "thread-error"
        state["last_return_code"] = 1
        state["current_prompt"] = message
        state["current_started_at"] = 100.0
        state["current_finished_at"] = 101.0
        state["event_count"] = 3
        state["recent_events"] = [
            "started: prompt=help me analyze this project",
            "progress: waiting elapsed=5s pid=123",
            "stderr: provider returned 500",
        ]
        state["recent_raw_events"] = [
            "stderr: upstream connection failed",
            "stdout: partial reply",
        ]
        state["stderr_lines"] = ["upstream connection failed"]
        state["latest_reply_preview"] = "partial reply"
        raise RuntimeError("backend exploded")


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


def test_send_session_message_targets_specific_session_and_can_mark_last_good(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    first = service.new_session(chat_id=7, label="first")
    second = service.new_session(chat_id=7, label="second")

    session, reply = service.send_session_message(first["session_id"], "repair this service", event_name="rescue_message")

    assert session["session_id"] == first["session_id"]
    assert reply == "reply:repair this service"
    payload = service.mark_last_good_session(first["session_id"])
    assert payload["session_id"] == first["session_id"]
    assert service.db.get_app_state("last_good_session")["session_id"] == first["session_id"]
    assert backend.sessions[second["session_id"]]["latest_reply_preview"] == ""


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


def test_context_handoff_defaults_to_light_and_preserves_custom_backend_state(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    session = service.get_or_create_chat_session(7)
    service.queue_session_context_handoff(session["session_id"], "handoff text")
    service.send_chat_message(7, "hello")

    stored = service.db.get_session(session["session_id"])
    assert stored is not None
    assert stored["backend_state"]["context_handoff_mode"] == "light"
    assert "pending_context_handoff" not in stored["backend_state"]

    status = service.get_session_status(session["session_id"])
    assert status["backend_status"]["context_handoff_mode"] == "light"


def test_can_disable_context_handoff_per_session(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    updated = service.set_chat_context_handoff(7, "off")

    assert updated["backend_status"]["context_handoff_mode"] == "off"


def test_pending_context_handoff_is_prefixed_as_structured_block(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    session = service.get_or_create_chat_session(7)
    service.queue_session_context_handoff(
        session["session_id"],
        "[Context Handoff]\n[Session Summary]\n- workspace: /tmp/demo\n[End Context Handoff]",
    )
    service.send_chat_message(7, "continue the task")

    sent = backend.sessions[session["session_id"]]["latest_reply_preview"]
    assert "[Context Handoff]" in sent
    assert "[End Context Handoff]" in sent
    assert "[Current User Message]\ncontinue the task" in sent


def test_cancelled_reply_preserves_cancelled_session_status(tmp_path: Path) -> None:
    backend = CancelBackend()
    service = _build_service(tmp_path, backend)

    session = service.get_or_create_chat_session(7)

    try:
        service.send_chat_message(7, "stop")
    except CodexReplyCancelled:
        pass
    else:
        raise AssertionError("expected CodexReplyCancelled")

    stored = service.db.get_session(session["session_id"])
    assert stored is not None
    assert stored["status"] == "cancelled"
    assert stored["backend_state"]["thread_id"] == "thread-cancelled"
    assert stored["backend_state"]["last_return_code"] == 143


def test_failed_reply_persists_trace_context_for_restart_fallback(tmp_path: Path) -> None:
    backend1 = ErrorBackend()
    service1 = _build_service(tmp_path, backend1)
    session = service1.get_or_create_chat_session(7)

    try:
        service1.send_chat_message(7, "help me analyze this project")
    except RuntimeError as exc:
        assert str(exc) == "backend exploded"
    else:
        raise AssertionError("expected RuntimeError")

    stored = service1.db.get_session(session["session_id"])
    assert stored is not None
    assert stored["status"] == "error"
    assert stored["backend_state"]["thread_id"] == "thread-error"
    assert stored["backend_state"]["last_return_code"] == 1
    assert stored["backend_state"]["current_prompt"] == "help me analyze this project"
    assert stored["backend_state"]["event_count"] == 3
    assert stored["backend_state"]["recent_events"][-1] == "stderr: provider returned 500"
    assert stored["backend_state"]["stderr_lines"] == ["upstream connection failed"]

    backend2 = FakeBackend()
    service2 = _build_service(tmp_path, backend2)
    status = service2.get_session_status(session["session_id"])

    assert status["backend_status"]["exists"] is False
    assert status["backend_status"]["alive"] is False
    assert status["backend_status"]["running"] is False
    assert status["backend_status"]["thread_id"] == "thread-error"
    assert status["backend_status"]["last_return_code"] == 1
    assert status["backend_status"]["current_prompt"] == "help me analyze this project"
    assert status["backend_status"]["event_count"] == 3
    assert status["backend_status"]["recent_raw_events"] == [
        "stderr: upstream connection failed",
        "stdout: partial reply",
    ]
    assert status["backend_status"]["stderr_lines"] == ["upstream connection failed"]


def test_list_codex_providers_includes_default(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    providers = service.list_codex_providers()

    assert providers[0]["label"] == "default"
    assert providers[0]["is_default"] is True


def test_list_claude_code_providers_includes_empty_default(tmp_path: Path) -> None:
    backend = FakeBackend(mode="codex_cli_session")
    service = _build_service(tmp_path, backend)

    providers = service.list_claude_code_providers()

    assert providers[0]["label"] == "default"
    assert providers[0]["model"] == "(empty)"
    assert providers[0]["base_url"] == "(empty)"
    assert providers[0]["is_default"] is True


def test_list_claude_code_providers_uses_configured_default(tmp_path: Path) -> None:
    backend = FakeBackend(mode="codex_cli_session")
    service = _build_service(tmp_path, backend, default_claude_code_provider_label="provider-a")
    service.add_claude_code_provider(
        label="provider-a",
        model="provider-model-v2",
        base_url="https://provider.example.invalid/v1",
        api_key="secret-token",
    )

    providers = service.list_claude_code_providers()

    assert providers[0]["label"] == "default"
    assert providers[0]["model"] == "provider-model-v2"
    assert providers[0]["base_url"] == "https://provider.example.invalid/v1"
    assert providers[0]["is_default"] is True


def test_delete_codex_provider_removes_saved_provider(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)
    service.add_codex_provider(
        label="relay",
        model="gpt-5.4",
        base_url="https://provider.example.invalid/v1",
        api_key="secret-token",
    )

    deleted_label = service.delete_codex_provider("relay")

    assert deleted_label == "relay"
    assert service.db.get_codex_provider("relay") is None


def test_delete_codex_provider_rejects_default(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    try:
        service.delete_codex_provider("default")
    except ValueError as exc:
        assert "cannot be deleted" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_delete_codex_provider_rejects_provider_in_use(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)
    service.add_codex_provider(
        label="relay",
        model="gpt-5.4",
        base_url="https://provider.example.invalid/v1",
        api_key="secret-token",
    )
    service.switch_chat_codex_provider(7, "relay")

    try:
        service.delete_codex_provider("relay")
    except ValueError as exc:
        assert "still in use" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_switch_chat_codex_provider_updates_current_session(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)
    service.add_codex_provider(
        label="relay",
        model="gpt-5.4",
        base_url="https://provider.example.invalid/v1",
        api_key="secret-token",
    )

    status = service.switch_chat_codex_provider(7, "relay")

    backend_status = status["backend_status"]
    assert backend_status["provider_label"] == "relay"


class FakeClaudeBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__(mode="claude_code_cli_session")

    def set_session_provider(self, session_id: str, provider: dict | None) -> dict:
        state = self.sessions[session_id]
        if provider is None:
            state["provider_label"] = "default"
            state["provider_model"] = None
            state["provider_base_url"] = None
            state.pop("provider_api_key", None)
        else:
            state["provider_label"] = provider["label"]
            state["provider_model"] = provider["model"]
            state["provider_base_url"] = provider["base_url"]
            state["provider_api_key"] = provider["api_key"]
        return state

    def restore_session(self, session_id: str, workspace: Path, backend_state: dict | None = None) -> CodexSessionInfo:
        super().restore_session(session_id, workspace, backend_state)
        self.sessions[session_id].pop("provider_api_key", None)
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode=self.mode)


def test_switch_chat_claude_provider_reinjects_provider_on_restore(tmp_path: Path) -> None:
    codex_backend = FakeBackend()
    claude_backend = FakeClaudeBackend()
    service = _build_service(tmp_path, codex_backend, claude_backend=claude_backend)
    service.switch_chat_backend(7, "claude_code_cli_session")
    service.add_claude_code_provider(
        label="provider-a",
        model="demo-model",
        base_url="https://example.invalid/api/coding",
        api_key="secret-token",
    )

    status = service.switch_chat_claude_code_provider(7, "provider-a")

    backend_status = status["backend_status"]
    assert backend_status["provider_label"] == "provider-a"
    assert backend_status["provider_model"] == "demo-model"
    assert backend_status["provider_base_url"] == "https://example.invalid/api/coding"
    assert claude_backend.sessions[status["session_id"]]["provider_api_key"] == "secret-token"
    stored = service.db.get_session(status["session_id"])
    assert stored is not None
    assert stored["backend_state"]["provider_label"] == "provider-a"


def test_switch_chat_codex_provider_can_return_to_default(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)
    service.add_codex_provider(
        label="relay",
        model="gpt-5.4",
        base_url="https://provider.example.invalid/v1",
        api_key="secret-token",
    )
    service.switch_chat_codex_provider(7, "relay")

    status = service.switch_chat_codex_provider(7, "default")

    backend_status = status["backend_status"]
    assert backend_status["provider_label"] == "default"
    assert backend_status["provider_model"] is None
    assert backend_status["provider_base_url"] is None
    stored = service.db.get_session(status["session_id"])
    assert stored is not None
    assert stored["backend_state"]["provider_label"] == "default"


def test_reset_chat_session_preserves_provider_selection(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)
    service.add_codex_provider(
        label="relay",
        model="gpt-5.4",
        base_url="https://provider.example.invalid/v1",
        api_key="secret-token",
    )
    service.switch_chat_codex_provider(7, "relay")

    new_session = service.reset_chat_session(7)
    status = service.get_session_status(new_session["session_id"])

    assert status["backend_status"]["provider_label"] == "relay"
    assert status["backend_status"]["provider_model"] == "gpt-5.4"


def test_switch_chat_backend_moves_to_claude_and_preserves_timeout(tmp_path: Path) -> None:
    codex_backend = FakeBackend(mode="codex_cli_session")
    claude_backend = FakeBackend(mode="claude_code_cli_session")
    service = _build_service(tmp_path, codex_backend, claude_backend=claude_backend)

    service.set_chat_timeout(7, 300)
    status = service.switch_chat_backend(7, "claude_code_cli_session")

    assert status["integration_mode"] == "claude_code_cli_session"
    assert status["workspace_path"].endswith("workspace")
    assert status["backend_status"]["timeout_seconds"] == 300
    current = service.get_chat(7)
    assert current is not None
    assert current["session_id"] == status["session_id"]


def test_switch_chat_backend_transfers_existing_label(tmp_path: Path) -> None:
    codex_backend = FakeBackend(mode="codex_cli_session")
    claude_backend = FakeBackend(mode="claude_code_cli_session")
    service = _build_service(tmp_path, codex_backend, claude_backend=claude_backend)
    current = service.new_session(chat_id=7, label="coder")

    status = service.switch_chat_backend(7, "claude_code_cli_session")

    assert status["integration_mode"] == "claude_code_cli_session"
    assert status["label"] == "coder"
    old = service.db.get_session(current["session_id"])
    assert old is not None
    assert old["label"] == ""


def test_switch_chat_claude_code_provider_updates_current_session(tmp_path: Path) -> None:
    codex_backend = FakeBackend(mode="codex_cli_session")
    claude_backend = FakeBackend(mode="claude_code_cli_session")
    service = _build_service(tmp_path, codex_backend, claude_backend=claude_backend)
    service.switch_chat_backend(7, "claude_code_cli_session")
    service.add_claude_code_provider(
        label="provider-a",
        model="provider-model-v1",
        base_url="http://127.0.0.1:18080",
        api_key="secret-token",
    )

    status = service.switch_chat_claude_code_provider(7, "provider-a")

    backend_status = status["backend_status"]
    assert backend_status["provider_label"] == "provider-a"
    assert backend_status["provider_model"] == "provider-model-v1"
    assert backend_status["provider_base_url"] == "http://127.0.0.1:18080"
    stored = service.db.get_session(status["session_id"])
    assert stored is not None
    assert stored["backend_state"]["provider_label"] == "provider-a"


def test_switch_chat_claude_code_provider_can_return_to_empty_default(tmp_path: Path) -> None:
    codex_backend = FakeBackend(mode="codex_cli_session")
    claude_backend = FakeBackend(mode="claude_code_cli_session")
    service = _build_service(tmp_path, codex_backend, claude_backend=claude_backend)
    service.switch_chat_backend(7, "claude_code_cli_session")
    service.add_claude_code_provider(
        label="provider-a",
        model="provider-model-v1",
        base_url="http://127.0.0.1:18080",
        api_key="secret-token",
    )
    service.switch_chat_claude_code_provider(7, "provider-a")

    status = service.switch_chat_claude_code_provider(7, "default")

    backend_status = status["backend_status"]
    assert backend_status["provider_label"] == "default"
    assert backend_status["provider_model"] is None
    assert backend_status["provider_base_url"] is None
    stored = service.db.get_session(status["session_id"])
    assert stored is not None
    assert stored["backend_state"]["provider_label"] == "default"


def test_new_claude_session_uses_configured_default_provider(tmp_path: Path) -> None:
    codex_backend = FakeBackend(mode="codex_cli_session")
    claude_backend = FakeBackend(mode="claude_code_cli_session")
    service = _build_service(
        tmp_path,
        codex_backend,
        claude_backend=claude_backend,
        default_claude_code_provider_label="provider-a",
    )
    service.add_claude_code_provider(
        label="provider-a",
        model="provider-model-v2",
        base_url="https://provider.example.invalid/v1",
        api_key="secret-token",
    )

    status = service.switch_chat_backend(7, "claude_code_cli_session")

    assert status["backend_status"]["provider_label"] == "provider-a"
    assert status["backend_status"]["provider_model"] == "provider-model-v2"
    assert status["backend_status"]["provider_base_url"] == "https://provider.example.invalid/v1"


def test_switch_chat_session_rebinds_current_mapping(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    first = service.new_session(chat_id=7, label="first")
    second = service.new_session(chat_id=7, label="second")

    current = service.get_chat(7)
    assert current is not None
    assert current["session_id"] == second["session_id"]

    switched = service.switch_chat_session(7, first["session_id"])

    assert switched["session_id"] == first["session_id"]
    rebound = service.get_chat(7)
    assert rebound is not None
    assert rebound["session_id"] == first["session_id"]


def test_switch_chat_session_rejects_foreign_session(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    foreign = service.new_session(chat_id=99, label="foreign")

    try:
        service.switch_chat_session(7, foreign["session_id"])
    except ValueError as exc:
        assert str(exc) == "Session does not belong to this chat"
    else:
        raise AssertionError("expected ValueError")


def test_delete_non_current_chat_session(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    first = service.new_session(chat_id=7, label="first")
    second = service.new_session(chat_id=7, label="second")

    service.delete_chat_session(7, first["session_id"])

    assert service.db.get_session(first["session_id"]) is None
    current = service.get_chat(7)
    assert current is not None
    assert current["session_id"] == second["session_id"]


def test_clear_chat_sessions_creates_fresh_current_session(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    current = service.new_session(chat_id=7, label="keep")
    service.new_session(chat_id=7, label="old")

    fresh = service.clear_chat_sessions(7)

    sessions = service.list_chat_sessions(7)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == fresh["session_id"]
    assert fresh["session_id"] != current["session_id"]


def test_switch_chat_session_accepts_unique_tag(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    first = service.new_session(chat_id=7, label="alpha")
    service.new_session(chat_id=7, label="beta")

    switched = service.switch_chat_session(7, "alpha")

    assert switched["session_id"] == first["session_id"]
    current = service.get_chat(7)
    assert current is not None
    assert current["session_id"] == first["session_id"]


def test_new_session_rejects_duplicate_tag_in_same_chat(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    service.new_session(chat_id=7, label="same")

    try:
        service.new_session(chat_id=7, label="same")
    except ValueError as exc:
        assert str(exc) == "Session tag 'same' already exists in this chat"
    else:
        raise AssertionError("expected ValueError")


def test_delete_chat_session_accepts_unique_tag(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    service.new_session(chat_id=7, label="keep")
    target = service.new_session(chat_id=7, label="old-tag")
    service.switch_chat_session(7, "keep")

    service.delete_chat_session(7, "old-tag")

    assert service.db.get_session(target["session_id"]) is None


def test_reset_transfers_tag_to_new_session(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    first = service.new_session(chat_id=7, label="train")
    new = service.reset_chat_session(7)

    old = service.db.get_session(first["session_id"])
    assert old is not None
    assert old["label"] == ""
    assert new["label"] == "train"


def test_workspace_switch_transfers_tag_to_new_session(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)
    alt = tmp_path / "alt"
    alt.mkdir()
    service.workspace_guard.allowed_roots.append(alt.resolve())

    first = service.new_session(chat_id=7, label="bench")
    new = service.switch_chat_workspace(7, str(alt))

    old = service.db.get_session(first["session_id"])
    assert old is not None
    assert old["label"] == ""
    assert new["label"] == "bench"


def test_set_session_label_rejects_duplicate_tag_on_other_session(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = _build_service(tmp_path, backend)

    first = service.new_session(chat_id=7, label="alpha")
    service.new_session(chat_id=7, label="beta")
    service.switch_chat_session(7, first["session_id"])

    try:
        service.set_session_label(7, "beta")
    except ValueError as exc:
        assert str(exc) == "Session tag 'beta' already exists in this chat"
    else:
        raise AssertionError("expected ValueError")
