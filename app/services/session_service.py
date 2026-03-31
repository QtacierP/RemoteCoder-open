"""Session lifecycle and routing logic."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from app.codex.base import CodexBackend
from app.db import Database
from app.services.audit_service import AuditService
from app.services.workspace_guard import WorkspaceGuard

logger = logging.getLogger(__name__)


class SessionService:
    def __init__(
        self,
        db: Database,
        audit_service: AuditService,
        workspace_guard: WorkspaceGuard,
        backends: dict[str, CodexBackend],
        default_mode: str,
    ) -> None:
        self.db = db
        self.audit = audit_service
        self.workspace_guard = workspace_guard
        self.backends = backends
        self.default_mode = default_mode

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
    ) -> dict:
        mode = mode or self.default_mode
        if mode not in self.backends:
            raise ValueError(f"Unknown mode: {mode}")
        workspace_path = self.workspace_guard.normalize(workspace)
        session_id = str(uuid.uuid4())
        now = self.db.now_iso()
        record = {
            "session_id": session_id,
            "chat_id": chat_id,
            "integration_mode": mode,
            "label": label.strip(),
            "workspace_path": str(workspace_path),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        self.backends[mode].create_session(session_id=session_id, workspace=workspace_path)
        self.db.create_session(record)
        self.audit.log(
            "session_created",
            chat_id,
            session_id,
            {"mode": mode, "workspace": str(workspace_path), "label": label.strip()},
        )
        return record

    def reset_chat_session(self, chat_id: int) -> dict:
        current = self.get_or_create_chat_session(chat_id)
        mode = current["integration_mode"]
        workspace = Path(current["workspace_path"])
        new = self.new_session(chat_id=chat_id, mode=mode, workspace=workspace, label=current.get("label", ""))
        self.backends[mode].close_session(current["session_id"])
        self.db.update_session_status(current["session_id"], "reset")
        self.audit.log("session_reset", chat_id, new["session_id"], {"previous_session_id": current["session_id"]})
        return new

    def switch_chat_workspace(self, chat_id: int, workspace: str | Path, label: str | None = None) -> dict:
        current = self.get_or_create_chat_session(chat_id)
        mode = current["integration_mode"]
        new_workspace = self.workspace_guard.normalize(workspace)
        if Path(current["workspace_path"]).resolve() == new_workspace:
            if label is not None and label.strip() != current.get("label", ""):
                self.db.update_session_label(current["session_id"], label.strip())
                current["label"] = label.strip()
            return current
        new_label = current.get("label", "") if label is None else label.strip()
        new = self.new_session(chat_id=chat_id, mode=mode, workspace=new_workspace, label=new_label)
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
        normalized = label.strip()
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
        mode = session["integration_mode"]
        backend = self.backends[mode]
        logger.debug(
            "session pipeline stage=get_or_create done",
            extra={"chat_id": chat_id, "session_id": session["session_id"], "mode": mode},
        )

        logger.debug("session pipeline stage=ensure_backend start", extra={"chat_id": chat_id, "session_id": session["session_id"]})
        self._ensure_backend_session(session)
        logger.debug("session pipeline stage=ensure_backend done", extra={"chat_id": chat_id, "session_id": session["session_id"]})

        self.audit.log("telegram_message", chat_id, session["session_id"], {"text": text[:1000], "mode": mode})
        logger.debug(
            "session pipeline stage=backend_send start",
            extra={"chat_id": chat_id, "session_id": session["session_id"], "mode": mode, "text_len": len(text)},
        )
        try:
            output = backend.send_message(session["session_id"], text)
        except Exception as exc:  # noqa: BLE001 - explicit operational resilience
            logger.exception("codex backend send failed")
            self.db.update_session_status(session["session_id"], "error")
            self.audit.log("backend_error", chat_id, session["session_id"], {"error": str(exc)})
            raise
        logger.debug(
            "session pipeline stage=backend_send done",
            extra={"chat_id": chat_id, "session_id": session["session_id"], "mode": mode, "output_len": len(output)},
        )
        self.db.update_session_status(session["session_id"], "active")
        return session, output

    def _ensure_backend_session(self, session: dict) -> None:
        mode = session["integration_mode"]
        backend = self.backends[mode]
        status = backend.get_status(session["session_id"])
        if status.get("exists"):
            return

        workspace = Path(session["workspace_path"])
        backend.create_session(session_id=session["session_id"], workspace=workspace)
        logger.warning(
            "recreated missing backend session from persisted mapping",
            extra={
                "session_id": session["session_id"],
                "chat_id": session["chat_id"],
                "mode": mode,
                "workspace": session["workspace_path"],
            },
        )
        self.audit.log(
            "session_rehydrated",
            session["chat_id"],
            session["session_id"],
            {"mode": mode, "workspace": session["workspace_path"]},
        )

    def get_session_status(self, session_id: str) -> dict:
        session = self.db.get_session(session_id)
        if not session:
            raise KeyError(session_id)
        mode = session["integration_mode"]
        session["backend_status"] = self.backends[mode].get_status(session_id)
        return session

    def list_sessions(self) -> list[dict]:
        return self.db.list_sessions()

    def get_chat(self, chat_id: int) -> dict | None:
        return self.db.get_chat_session(chat_id)
