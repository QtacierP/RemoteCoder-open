"""Session lifecycle and routing logic."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from app.codex.base import CodexBackend, CodexReplyCancelled
from app.db import Database
from app.services.audit_service import AuditService
from app.services.workspace_guard import WorkspaceGuard

logger = logging.getLogger(__name__)


class SessionService:
    DEFAULT_CODEX_PROVIDER_LABEL = "default"
    DEFAULT_CLAUDE_CODE_PROVIDER_LABEL = "default"
    DEFAULT_CONTEXT_HANDOFF_MODE = "light"

    def __init__(
        self,
        db: Database,
        audit_service: AuditService,
        workspace_guard: WorkspaceGuard,
        backends: dict[str, CodexBackend],
        default_mode: str,
        default_claude_code_provider_label: str = "",
    ) -> None:
        self.db = db
        self.audit = audit_service
        self.workspace_guard = workspace_guard
        self.backends = backends
        self.default_mode = default_mode
        self.default_claude_code_provider_label = self._normalize_provider_label(default_claude_code_provider_label)

    @staticmethod
    def _runtime_backend_state(backend_status: dict) -> dict:
        state: dict[str, object] = {}
        for key in (
            "thread_id",
            "config_dir",
            "has_started",
            "timeout_seconds",
            "last_return_code",
            "latest_reply_preview",
            "provider_label",
            "provider_model",
            "provider_base_url",
            "running",
            "current_prompt",
            "current_started_at",
            "current_finished_at",
            "event_count",
            "recent_events",
            "recent_raw_events",
            "stderr_lines",
            "cancel_requested",
        ):
            value = backend_status.get(key)
            if value is not None:
                state[key] = value
        return state

    @classmethod
    def _normalize_context_handoff_mode(cls, raw: str | None) -> str:
        mode = (raw or "").strip().lower()
        if mode in {"off", "light"}:
            return mode
        return cls.DEFAULT_CONTEXT_HANDOFF_MODE

    @staticmethod
    def _fallback_backend_status(session: dict) -> dict:
        backend_state = dict(session.get("backend_state") or {})
        fallback = {"exists": False, "alive": False, "running": False}
        for key in (
            "thread_id",
            "config_dir",
            "has_started",
            "timeout_seconds",
            "last_return_code",
            "latest_reply_preview",
            "provider_label",
            "provider_model",
            "provider_base_url",
            "current_prompt",
            "current_started_at",
            "current_finished_at",
            "event_count",
            "recent_events",
            "recent_raw_events",
            "stderr_lines",
            "cancel_requested",
        ):
            value = backend_state.get(key)
            if value is not None:
                fallback[key] = value
        return fallback

    @staticmethod
    def _normalize_provider_label(label: str | None) -> str:
        return (label or "").strip()

    def _provider_backend_kind(self, mode: str) -> str | None:
        if mode == "codex_cli_session":
            return "codex"
        if mode == "claude_code_cli_session":
            return "claude_code"
        return None

    def _default_provider_label(self, mode: str) -> str:
        backend_kind = self._provider_backend_kind(mode)
        if backend_kind == "claude_code":
            return self.DEFAULT_CLAUDE_CODE_PROVIDER_LABEL
        return self.DEFAULT_CODEX_PROVIDER_LABEL

    def _configured_default_provider_record(self, mode: str) -> dict | None:
        backend_kind = self._provider_backend_kind(mode)
        if backend_kind != "claude_code":
            return None
        label = self.default_claude_code_provider_label
        if not label or label.lower() == self.DEFAULT_CLAUDE_CODE_PROVIDER_LABEL:
            return None
        provider = self.db.get_claude_code_provider(label)
        if provider is None:
            logger.warning(
                "configured default Claude provider is missing",
                extra={"provider_label": label},
            )
        return provider

    def _effective_default_provider_label(self, mode: str) -> str:
        provider = self._configured_default_provider_record(mode)
        if provider is not None:
            return str(provider["label"]).strip()
        return self._default_provider_label(mode)

    def _resolve_provider_record(self, mode: str, label: str) -> dict | None:
        backend_kind = self._provider_backend_kind(mode)
        if backend_kind is None:
            return None
        normalized = self._normalize_provider_label(label)
        if not normalized or normalized.lower() == self._default_provider_label(mode):
            return self._configured_default_provider_record(mode)
        if backend_kind == "codex":
            provider = self.db.get_codex_provider(normalized)
        else:
            provider = self.db.get_claude_code_provider(normalized)
        if provider is None:
            raise KeyError(label)
        return provider

    def _desired_provider_label(self, session: dict) -> str:
        default_label = self._effective_default_provider_label(session["integration_mode"])
        backend_state = session.get("backend_state") or {}
        raw = backend_state.get("provider_label")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return default_label

    def _apply_session_provider(self, session: dict) -> None:
        mode = session["integration_mode"]
        backend = self.backends[mode]
        if self._provider_backend_kind(mode) is None:
            return
        status = backend.get_status(session["session_id"])
        if not status.get("exists"):
            return
        desired_label = self._desired_provider_label(session)
        current_label = str(status.get("provider_label") or self._default_provider_label(mode))
        provider = self._resolve_provider_record(mode, desired_label)
        if provider is None and current_label == desired_label:
            return
        if (
            provider is not None
            and current_label == desired_label
            and status.get("provider_model") == provider["model"]
            and status.get("provider_base_url") == provider["base_url"]
            and self._provider_backend_kind(mode) != "claude_code"
        ):
            return
        backend.set_session_provider(session["session_id"], provider)
        self._persist_backend_state(session["session_id"])

    def _persist_backend_state(self, session_id: str) -> None:
        session = self.db.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        mode = session["integration_mode"]
        backend_status = self.backends[mode].get_status(session_id)
        if not backend_status.get("exists"):
            return
        merged_state = dict(session.get("backend_state") or {})
        merged_state.update(self._runtime_backend_state(backend_status))
        self.db.update_session_backend_state(session_id, merged_state)

    def _session_context_handoff_mode(self, session: dict) -> str:
        backend_state = session.get("backend_state") or {}
        return self._normalize_context_handoff_mode(str(backend_state.get("context_handoff_mode") or ""))

    def set_chat_context_handoff(self, chat_id: int, mode: str) -> dict:
        session = self.get_or_create_chat_session(chat_id)
        backend_state = dict(session.get("backend_state") or {})
        backend_state["context_handoff_mode"] = self._normalize_context_handoff_mode(mode)
        self.db.update_session_backend_state(session["session_id"], backend_state)
        updated = self.db.get_session(session["session_id"])
        if updated is None:
            raise KeyError(session["session_id"])
        return self.get_session_status(updated["session_id"])

    def queue_session_context_handoff(self, session_id: str, handoff_text: str) -> dict:
        session = self.db.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        backend_state = dict(session.get("backend_state") or {})
        backend_state.setdefault("context_handoff_mode", self._session_context_handoff_mode(session))
        if handoff_text.strip():
            backend_state["pending_context_handoff"] = handoff_text.strip()
        else:
            backend_state.pop("pending_context_handoff", None)
        self.db.update_session_backend_state(session_id, backend_state)
        updated = self.db.get_session(session_id)
        if updated is None:
            raise KeyError(session_id)
        return updated

    def _apply_runtime_overrides(self, session_id: str, mode: str, backend_state: dict | None) -> None:
        if not backend_state:
            return
        timeout_seconds = backend_state.get("timeout_seconds")
        if not isinstance(timeout_seconds, int):
            return
        setter = getattr(self.backends[mode], "set_session_timeout", None)
        if callable(setter):
            setter(session_id, timeout_seconds)
            self._persist_backend_state(session_id)

    @staticmethod
    def _normalize_label(label: str | None) -> str:
        return (label or "").strip()

    def _assert_unique_label(
        self,
        chat_id: int,
        label: str,
        *,
        exclude_session_ids: set[str] | None = None,
    ) -> None:
        normalized = self._normalize_label(label)
        if not normalized:
            return
        excluded = exclude_session_ids or set()
        for item in self.db.list_chat_sessions(chat_id):
            if item["session_id"] in excluded:
                continue
            existing = self._normalize_label(item.get("label"))
            if existing.lower() == normalized.lower():
                raise ValueError(f"Session tag '{normalized}' already exists in this chat")

    def _resolve_chat_session_selector(self, chat_id: int, selector: str) -> dict:
        normalized = selector.strip()
        if not normalized:
            raise KeyError(selector)
        exact = self.db.get_session(normalized)
        if exact is not None:
            if exact["chat_id"] != chat_id:
                raise ValueError("Session does not belong to this chat")
            return exact

        sessions = self.db.list_chat_sessions(chat_id)
        matches = [item for item in sessions if (item.get("label") or "").strip().lower() == normalized.lower()]
        if not matches:
            raise KeyError(selector)
        if len(matches) > 1:
            raise ValueError(f"Multiple sessions match tag '{normalized}'. Use session_id instead.")
        return matches[0]

    def get_or_create_chat_session(self, chat_id: int) -> dict:
        existing = self.db.get_chat_session(chat_id)
        if existing:
            return existing
        return self.new_session(chat_id=chat_id)

    def new_session(
        self,
        chat_id: int,
        mode: str | None = None,
        workspace: str | Path | None = None,
        label: str = "",
        backend_state: dict | None = None,
    ) -> dict:
        mode = mode or self.default_mode
        if mode not in self.backends:
            raise ValueError(f"Unknown mode: {mode}")
        normalized_label = self._normalize_label(label)
        self._assert_unique_label(chat_id, normalized_label)
        workspace_path = self.workspace_guard.normalize(workspace)
        session_id = str(uuid.uuid4())
        now = self.db.now_iso()
        record = {
            "session_id": session_id,
            "chat_id": chat_id,
            "integration_mode": mode,
            "label": normalized_label,
            "backend_state": dict(backend_state or {}),
            "workspace_path": str(workspace_path),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        record["backend_state"].setdefault("context_handoff_mode", self.DEFAULT_CONTEXT_HANDOFF_MODE)
        self.backends[mode].create_session(session_id=session_id, workspace=workspace_path)
        self.db.create_session(record)
        has_default_provider = self._resolve_provider_record(mode, self._default_provider_label(mode)) is not None
        if record["backend_state"] or has_default_provider:
            self._apply_session_provider(record)
        if record["backend_state"]:
            self._apply_runtime_overrides(session_id, mode, record["backend_state"])
        if record["backend_state"] or has_default_provider:
            persisted = self.db.get_session(session_id)
            if persisted is not None:
                record = persisted
        self.audit.log(
            "session_created",
            chat_id,
            session_id,
            {"mode": mode, "workspace": str(workspace_path), "label": normalized_label},
        )
        return record

    def reset_chat_session(self, chat_id: int) -> dict:
        current = self.get_or_create_chat_session(chat_id)
        mode = current["integration_mode"]
        workspace = Path(current["workspace_path"])
        transferred_label = self._normalize_label(current.get("label"))
        transferred_backend_state = dict(current.get("backend_state") or {})
        if transferred_label:
            self.db.update_session_label(current["session_id"], "")
        try:
            new = self.new_session(
                chat_id=chat_id,
                mode=mode,
                workspace=workspace,
                label=transferred_label,
                backend_state=transferred_backend_state,
            )
        except Exception:
            if transferred_label:
                self.db.update_session_label(current["session_id"], transferred_label)
            raise
        self.backends[mode].close_session(current["session_id"])
        self.db.update_session_status(current["session_id"], "reset")
        self.audit.log("session_reset", chat_id, new["session_id"], {"previous_session_id": current["session_id"]})
        return new

    def switch_chat_workspace(self, chat_id: int, workspace: str | Path, label: str | None = None) -> dict:
        current = self.get_or_create_chat_session(chat_id)
        mode = current["integration_mode"]
        new_workspace = self.workspace_guard.normalize(workspace, base_workspace=current["workspace_path"])
        if Path(current["workspace_path"]).resolve() == new_workspace:
            normalized_label = self._normalize_label(label)
            if label is not None and normalized_label != current.get("label", ""):
                self._assert_unique_label(chat_id, normalized_label, exclude_session_ids={current["session_id"]})
                self.db.update_session_label(current["session_id"], normalized_label)
                current["label"] = normalized_label
            return current
        new_label = self._normalize_label(current.get("label", "")) if label is None else self._normalize_label(label)
        transferred_backend_state = dict(current.get("backend_state") or {})
        inherited = bool(new_label and label is None)
        if inherited:
            self.db.update_session_label(current["session_id"], "")
        try:
            new = self.new_session(
                chat_id=chat_id,
                mode=mode,
                workspace=new_workspace,
                label=new_label,
                backend_state=transferred_backend_state,
            )
        except Exception:
            if inherited:
                self.db.update_session_label(current["session_id"], new_label)
            raise
        self.backends[mode].close_session(current["session_id"])
        self.db.update_session_status(current["session_id"], "switched")
        self.audit.log(
            "session_workspace_switched",
            chat_id,
            new["session_id"],
            {
                "previous_session_id": current["session_id"],
                "previous_workspace": current["workspace_path"],
                "workspace": str(new_workspace),
                "label": new_label,
            },
        )
        return new

    def set_session_label(self, chat_id: int, label: str) -> dict:
        session = self.get_or_create_chat_session(chat_id)
        normalized = self._normalize_label(label)
        self._assert_unique_label(chat_id, normalized, exclude_session_ids={session["session_id"]})
        self.db.update_session_label(session["session_id"], normalized)
        self.audit.log(
            "session_label_updated",
            chat_id,
            session["session_id"],
            {"label": normalized},
        )
        updated = self.db.get_session(session["session_id"])
        if updated is None:
            raise KeyError(session["session_id"])
        return updated

    def send_chat_message(self, chat_id: int, text: str) -> tuple[dict, str]:
        logger.debug("session pipeline stage=get_or_create start", extra={"chat_id": chat_id})
        session = self.get_or_create_chat_session(chat_id)
        logger.debug(
            "session pipeline stage=get_or_create done",
            extra={"chat_id": chat_id, "session_id": session["session_id"], "mode": session["integration_mode"]},
        )
        return self.send_session_message(session["session_id"], text, event_name="telegram_message")

    def send_session_message(
        self,
        session_id: str,
        text: str,
        *,
        event_name: str = "session_message",
    ) -> tuple[dict, str]:
        session = self.db.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        mode = session["integration_mode"]
        backend = self.backends[mode]

        logger.debug(
            "session pipeline stage=ensure_backend start",
            extra={"chat_id": session["chat_id"], "session_id": session["session_id"]},
        )
        self._ensure_backend_session(session)
        logger.debug(
            "session pipeline stage=ensure_backend done",
            extra={"chat_id": session["chat_id"], "session_id": session["session_id"]},
        )

        self.audit.log(event_name, session["chat_id"], session["session_id"], {"text": text[:1000], "mode": mode})
        backend_state = session.get("backend_state") or {}
        pending_handoff = str(backend_state.get("pending_context_handoff") or "").strip()
        effective_text = text
        if pending_handoff:
            effective_text = (
                f"{pending_handoff}\n\n"
                "[Current User Message]\n"
                f"{text}"
            )
        logger.debug(
            "session pipeline stage=backend_send start",
            extra={
                "chat_id": session["chat_id"],
                "session_id": session["session_id"],
                "mode": mode,
                "text_len": len(effective_text),
                "event_name": event_name,
            },
        )
        try:
            output = backend.send_message(session["session_id"], effective_text)
        except CodexReplyCancelled:
            logger.info(
                "backend send cancelled",
                extra={"chat_id": session["chat_id"], "session_id": session["session_id"], "mode": mode},
            )
            self._persist_backend_state(session["session_id"])
            self.db.update_session_status(session["session_id"], "cancelled")
            self.audit.log("reply_cancelled", session["chat_id"], session["session_id"], {"source": "backend_send"})
            raise
        except Exception as exc:  # noqa: BLE001 - explicit operational resilience
            logger.exception("backend send failed")
            self._persist_backend_state(session["session_id"])
            self.db.update_session_status(session["session_id"], "error")
            self.audit.log("backend_error", session["chat_id"], session["session_id"], {"error": str(exc)})
            raise
        logger.debug(
            "session pipeline stage=backend_send done",
            extra={"chat_id": session["chat_id"], "session_id": session["session_id"], "mode": mode, "output_len": len(output)},
        )
        self._persist_backend_state(session["session_id"])
        if pending_handoff:
            refreshed = self.db.get_session(session["session_id"])
            if refreshed is not None:
                merged_state = dict(refreshed.get("backend_state") or {})
                merged_state.pop("pending_context_handoff", None)
                self.db.update_session_backend_state(session["session_id"], merged_state)
        self.db.update_session_status(session["session_id"], "active")
        updated = self.db.get_session(session["session_id"])
        if updated is None:
            raise KeyError(session["session_id"])
        return updated, output

    def mark_last_good_session(self, session_id: str) -> dict:
        session = self.db.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        status = self.get_session_status(session_id)
        backend_status = status.get("backend_status", {})
        payload = {
            "session_id": session_id,
            "chat_id": session["chat_id"],
            "integration_mode": session["integration_mode"],
            "workspace_path": session["workspace_path"],
            "label": session.get("label") or "",
            "provider_label": backend_status.get("provider_label") or "default",
            "provider_model": backend_status.get("provider_model"),
            "provider_base_url": backend_status.get("provider_base_url"),
            "updated_at": self.db.now_iso(),
            "latest_reply_preview": backend_status.get("latest_reply_preview") or "",
        }
        self.db.set_app_state("last_good_session", payload)
        return payload

    def _ensure_backend_session(self, session: dict) -> None:
        mode = session["integration_mode"]
        backend = self.backends[mode]
        status = backend.get_status(session["session_id"])
        if status.get("exists"):
            self._apply_session_provider(session)
            return

        workspace = Path(session["workspace_path"])
        restore = getattr(backend, "restore_session", None)
        backend_state = session.get("backend_state") or {}
        restored = False
        if callable(restore):
            restore(session_id=session["session_id"], workspace=workspace, backend_state=backend_state)
            restored = bool(backend_state)
        else:
            backend.create_session(session_id=session["session_id"], workspace=workspace)
        logger.warning(
            "recreated missing backend session from persisted mapping",
            extra={
                "session_id": session["session_id"],
                "chat_id": session["chat_id"],
                "mode": mode,
                "workspace": session["workspace_path"],
                "restored_backend_state": restored,
            },
        )
        self.audit.log(
            "session_rehydrated",
            session["chat_id"],
            session["session_id"],
            {"mode": mode, "workspace": session["workspace_path"], "restored_backend_state": restored},
        )
        self._apply_session_provider(session)
        self._apply_runtime_overrides(session["session_id"], mode, backend_state)

    def get_session_status(self, session_id: str) -> dict:
        session = self.db.get_session(session_id)
        if not session:
            raise KeyError(session_id)
        mode = session["integration_mode"]
        backend_status = self.backends[mode].get_status(session_id)
        if not backend_status.get("exists"):
            backend_status = self._fallback_backend_status(session)
        backend_status["context_handoff_mode"] = self._session_context_handoff_mode(session)
        backend_status["pending_context_handoff"] = str((session.get("backend_state") or {}).get("pending_context_handoff") or "")
        session["backend_status"] = backend_status
        return session

    def list_sessions(self) -> list[dict]:
        return self.db.list_sessions()

    def list_chat_sessions(self, chat_id: int) -> list[dict]:
        return self.db.list_chat_sessions(chat_id)

    def _list_saved_providers(self, mode: str) -> list[dict]:
        backend_kind = self._provider_backend_kind(mode)
        if backend_kind == "codex":
            return self.db.list_codex_providers()
        if backend_kind == "claude_code":
            return self.db.list_claude_code_providers()
        raise ValueError(f"Mode does not support providers: {mode}")

    def _get_saved_provider(self, mode: str, label: str) -> dict | None:
        backend_kind = self._provider_backend_kind(mode)
        if backend_kind == "codex":
            return self.db.get_codex_provider(label)
        if backend_kind == "claude_code":
            return self.db.get_claude_code_provider(label)
        raise ValueError(f"Mode does not support providers: {mode}")

    def _upsert_provider(self, mode: str, *, label: str, model: str, base_url: str, api_key: str) -> None:
        backend_kind = self._provider_backend_kind(mode)
        if backend_kind == "codex":
            self.db.upsert_codex_provider(label=label, model=model, base_url=base_url, api_key=api_key)
            return
        if backend_kind == "claude_code":
            self.db.upsert_claude_code_provider(label=label, model=model, base_url=base_url, api_key=api_key)
            return
        raise ValueError(f"Mode does not support providers: {mode}")

    def _delete_provider(self, mode: str, label: str) -> None:
        backend_kind = self._provider_backend_kind(mode)
        if backend_kind == "codex":
            self.db.delete_codex_provider(label)
            return
        if backend_kind == "claude_code":
            self.db.delete_claude_code_provider(label)
            return
        raise ValueError(f"Mode does not support providers: {mode}")

    def _default_provider_item(self, mode: str) -> dict:
        backend_kind = self._provider_backend_kind(mode)
        if backend_kind == "claude_code":
            configured_default = self._configured_default_provider_record(mode)
            if configured_default is not None:
                return {
                    "label": self.DEFAULT_CLAUDE_CODE_PROVIDER_LABEL,
                    "model": configured_default["model"],
                    "base_url": configured_default["base_url"],
                    "is_default": True,
                }
            return {
                "label": self.DEFAULT_CLAUDE_CODE_PROVIDER_LABEL,
                "model": "(empty)",
                "base_url": "(empty)",
                "is_default": True,
            }
        return {
            "label": self.DEFAULT_CODEX_PROVIDER_LABEL,
            "model": "(codex default)",
            "base_url": "(official)",
            "is_default": True,
        }

    def _list_providers(self, mode: str) -> list[dict]:
        items = [self._default_provider_item(mode)]
        for item in self._list_saved_providers(mode):
            items.append(
                {
                    "label": item["label"],
                    "model": item["model"],
                    "base_url": item["base_url"],
                    "is_default": False,
                }
            )
        return items

    def _add_provider(self, mode: str, *, label: str, model: str, base_url: str, api_key: str) -> dict:
        normalized_label = self._normalize_provider_label(label)
        normalized_model = (model or "").strip()
        normalized_base_url = (base_url or "").strip()
        normalized_api_key = (api_key or "").strip()
        if not normalized_label:
            raise ValueError("Provider label cannot be empty")
        if normalized_label.lower() == self._default_provider_label(mode):
            raise ValueError("Provider label 'default' is reserved")
        if not normalized_model:
            raise ValueError("Provider model cannot be empty")
        if not normalized_base_url:
            raise ValueError("Provider base_url cannot be empty")
        if not normalized_api_key:
            raise ValueError("Provider api_key cannot be empty")
        self._upsert_provider(
            mode,
            label=normalized_label,
            model=normalized_model,
            base_url=normalized_base_url,
            api_key=normalized_api_key,
        )
        provider = self._get_saved_provider(mode, normalized_label)
        if provider is None:
            raise KeyError(normalized_label)
        return provider

    def _delete_provider_checked(self, mode: str, label: str) -> str:
        normalized_label = self._normalize_provider_label(label)
        if not normalized_label:
            raise ValueError("Provider label cannot be empty")
        if normalized_label.lower() == self._default_provider_label(mode):
            raise ValueError("Provider label 'default' cannot be deleted")
        provider = self._get_saved_provider(mode, normalized_label)
        if provider is None:
            raise KeyError(label)
        target_kind = self._provider_backend_kind(mode)
        for session in self.db.list_sessions():
            if self._provider_backend_kind(session["integration_mode"]) != target_kind:
                continue
            backend_state = session.get("backend_state") or {}
            active_label = str(backend_state.get("provider_label") or "").strip()
            if active_label.lower() == normalized_label.lower():
                raise ValueError(
                    f"Provider '{normalized_label}' is still in use by session {session['session_id']} "
                    f"(chat_id={session['chat_id']})"
                )
        self._delete_provider(mode, provider["label"])
        return provider["label"]

    def _switch_chat_provider(self, chat_id: int, mode: str, label: str) -> dict:
        session = self.get_or_create_chat_session(chat_id)
        if session["integration_mode"] != mode:
            raise ValueError(f"Current session mode is {session['integration_mode']}, not {mode}")
        default_label = self._default_provider_label(mode)
        normalized_label = self._normalize_provider_label(label) or default_label
        backend_state = dict(session.get("backend_state") or {})
        if normalized_label.lower() == default_label:
            backend_state.pop("provider_label", None)
        else:
            provider = self._resolve_provider_record(mode, normalized_label)
            if provider is None:
                raise KeyError(label)
            backend_state["provider_label"] = provider["label"]
        self.db.update_session_backend_state(session["session_id"], backend_state)
        updated = self.db.get_session(session["session_id"])
        if updated is None:
            raise KeyError(session["session_id"])
        self._ensure_backend_session(updated)
        self.audit.log(
            "session_provider_switched",
            chat_id,
            updated["session_id"],
            {"provider_label": normalized_label, "mode": mode},
        )
        return self.get_session_status(updated["session_id"])

    def list_codex_providers(self) -> list[dict]:
        return self._list_providers("codex_cli_session")

    def list_claude_code_providers(self) -> list[dict]:
        return self._list_providers("claude_code_cli_session")

    def delete_codex_provider(self, label: str) -> str:
        return self._delete_provider_checked("codex_cli_session", label)

    def delete_claude_code_provider(self, label: str) -> str:
        return self._delete_provider_checked("claude_code_cli_session", label)

    def add_codex_provider(self, *, label: str, model: str, base_url: str, api_key: str) -> dict:
        return self._add_provider("codex_cli_session", label=label, model=model, base_url=base_url, api_key=api_key)

    def add_claude_code_provider(self, *, label: str, model: str, base_url: str, api_key: str) -> dict:
        return self._add_provider(
            "claude_code_cli_session",
            label=label,
            model=model,
            base_url=base_url,
            api_key=api_key,
        )

    def switch_chat_codex_provider(self, chat_id: int, label: str) -> dict:
        return self._switch_chat_provider(chat_id, "codex_cli_session", label)

    def switch_chat_claude_code_provider(self, chat_id: int, label: str) -> dict:
        return self._switch_chat_provider(chat_id, "claude_code_cli_session", label)

    def switch_chat_backend(self, chat_id: int, mode: str) -> dict:
        if mode not in self.backends:
            raise ValueError(f"Unknown mode: {mode}")
        current = self.get_or_create_chat_session(chat_id)
        if current["integration_mode"] == mode:
            return self.get_session_status(current["session_id"])
        transferred_label = self._normalize_label(current.get("label", ""))
        current_backend_state = dict(current.get("backend_state") or {})
        transferred_backend_state: dict[str, object] = {}
        timeout_seconds = current_backend_state.get("timeout_seconds")
        if timeout_seconds is not None:
            transferred_backend_state["timeout_seconds"] = timeout_seconds
        transferred_backend_state["context_handoff_mode"] = self._session_context_handoff_mode(current)
        if self._provider_backend_kind(current["integration_mode"]) == self._provider_backend_kind(mode):
            provider_label = current_backend_state.get("provider_label")
            if provider_label is not None:
                transferred_backend_state["provider_label"] = provider_label
        if transferred_label:
            self.db.update_session_label(current["session_id"], "")
        try:
            new = self.new_session(
                chat_id=chat_id,
                mode=mode,
                workspace=current["workspace_path"],
                label=transferred_label,
                backend_state=transferred_backend_state,
            )
        except Exception:
            if transferred_label:
                self.db.update_session_label(current["session_id"], transferred_label)
            raise
        self.backends[current["integration_mode"]].close_session(current["session_id"])
        self.db.update_session_status(current["session_id"], "switched")
        self.audit.log(
            "session_backend_switched",
            chat_id,
            new["session_id"],
            {
                "previous_session_id": current["session_id"],
                "previous_mode": current["integration_mode"],
                "mode": mode,
                "workspace": current["workspace_path"],
            },
        )
        updated = self.db.get_session(new["session_id"])
        if updated is None:
            raise KeyError(new["session_id"])
        return self.get_session_status(updated["session_id"])

    def switch_chat_session(self, chat_id: int, selector: str) -> dict:
        target = self._resolve_chat_session_selector(chat_id, selector)
        session_id = target["session_id"]
        self.db.update_chat_mapping(chat_id, session_id)
        self._ensure_backend_session(target)
        self.db.update_session_status(session_id, "active")
        self.audit.log(
            "session_switched",
            chat_id,
            session_id,
            {"workspace": target["workspace_path"], "label": target.get("label", "")},
        )
        updated = self.db.get_session(session_id)
        if updated is None:
            raise KeyError(session_id)
        return updated

    def delete_chat_session(self, chat_id: int, selector: str) -> None:
        target = self._resolve_chat_session_selector(chat_id, selector)
        session_id = target["session_id"]
        current = self.db.get_chat_session(chat_id)
        if current and current["session_id"] == session_id:
            raise ValueError("Cannot delete the current session. Use /session_clear or switch first.")
        self.backends[target["integration_mode"]].close_session(session_id)
        self.db.delete_session(session_id)
        self.audit.log(
            "session_deleted",
            chat_id,
            session_id,
            {"workspace": target["workspace_path"], "label": target.get("label", "")},
        )

    def clear_chat_sessions(self, chat_id: int) -> dict:
        current = self.get_or_create_chat_session(chat_id)
        sessions = self.db.list_chat_sessions(chat_id)
        for item in sessions:
            self.backends[item["integration_mode"]].close_session(item["session_id"])
            self.db.delete_session(item["session_id"])
        self.db.delete_chat_mapping(chat_id)
        new_session = self.new_session(
            chat_id=chat_id,
            mode=current["integration_mode"],
            workspace=current["workspace_path"],
            label=current.get("label", ""),
        )
        self.audit.log(
            "sessions_cleared",
            chat_id,
            new_session["session_id"],
            {"cleared_count": len(sessions)},
        )
        return new_session

    def rehydrate_persisted_sessions(self) -> list[dict]:
        restored: list[dict] = []
        for session in self.db.list_current_chat_sessions():
            self._ensure_backend_session(session)
            restored.append(
                {
                    "session_id": session["session_id"],
                    "chat_id": session["chat_id"],
                    "mode": session["integration_mode"],
                    "workspace": session["workspace_path"],
                    "restored_backend_state": bool(session.get("backend_state")),
                }
            )
        return restored

    def get_chat(self, chat_id: int) -> dict | None:
        return self.db.get_chat_session(chat_id)

    def set_chat_timeout(self, chat_id: int, timeout_seconds: int) -> dict:
        session = self.get_or_create_chat_session(chat_id)
        mode = session["integration_mode"]
        backend = self.backends[mode]
        setter = getattr(backend, "set_session_timeout", None)
        if setter is None:
            raise ValueError(f"Timeout override is not supported for mode: {mode}")
        setter(session["session_id"], timeout_seconds)
        self._persist_backend_state(session["session_id"])
        return self.get_session_status(session["session_id"])

    def cancel_chat_reply(self, chat_id: int) -> dict:
        session = self.get_or_create_chat_session(chat_id)
        mode = session["integration_mode"]
        backend = self.backends[mode]
        result = backend.cancel_running_reply(session["session_id"])
        if result.get("ok"):
            self.db.update_session_status(session["session_id"], "cancelled")
            self.audit.log("reply_cancelled", chat_id, session["session_id"], {"pid": result.get("pid")})
        self._persist_backend_state(session["session_id"])
        return {
            "session_id": session["session_id"],
            "workspace": session["workspace_path"],
            "mode": mode,
            **result,
        }
