"""Entrypoint for Telegram-to-Codex bridge MVP."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI

from app.adapters.telegram import TelegramAdapter
from app.codex.base import CodexReplyCancelled
from app.api.routes import router
from app.codex.cli_session import CodexCliSessionBackend
from app.codex.claude_code import ClaudeCodeSessionBackend
from app.codex.sdk_mode import CodexSdkBackend
from app.config import settings
from app.db import Database
from app.logging import configure_logging
from app.schemas import TelegramInboundMessage
from app.services.audit_service import AuditService
from app.services.conversation_history import ConversationHistoryService
from app.services.claude_proxy_service import ClaudeCodeProxyService
from app.services.restart_service import (
    SERVICE_RESTART_DELAY_SECONDS,
    SERVICE_UNIT_NAME,
    build_restart_command,
    build_user_systemd_env,
)
from app.services.session_service import SessionService
from app.services.shell_service import ShellService
from app.services.workspace_guard import WorkspaceGuard

configure_logging(
    settings.log_dir,
    debug=settings.telegram_debug_mode or settings.codex_debug_mode,
)
logger = logging.getLogger(__name__)


def normalize_command_alias(text: str) -> str:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return text
    parts = raw.split()
    if not parts:
        return text
    head = parts[0].lower()
    tail = parts[1:]

    def _join(cmd: str, rest: list[str]) -> str:
        return " ".join([cmd, *rest]).strip()

    if head == "/backend":
        return _join("/coder_backend", tail)
    if (head == "/restart" and tail[:1] == ["service"]) or (head == "/service" and tail[:1] == ["restart"]):
        return _join("/restart_service", tail[1:])
    if head == "/session" and tail[:1] == ["clear"]:
        return _join("/session_clear", tail[1:])
    if head == "/session" and tail[:1] == ["delete"]:
        return _join("/session_delete", tail[1:])
    if head == "/cmd" and tail[:2] == ["bg", "all"]:
        return _join("/cmd_bg_all", tail[2:])
    if head == "/cmd" and tail[:2] == ["job", "all"]:
        return _join("/cmd_job_all", tail[2:])
    if head == "/cmd" and tail[:2] == ["bg", "delete"]:
        return _join("/cmd_bg_delete", tail[2:])
    if head == "/cmd" and tail[:2] == ["bg", "clear"]:
        return _join("/cmd_bg_clear", tail[2:])
    if head == "/cmd" and tail[:1] == ["bg"]:
        return _join("/cmd_bg", tail[1:])
    if head == "/cmd" and tail[:2] == ["stop", "all"]:
        return _join("/cmd_stop_all", tail[2:])
    if head == "/cmd" and tail[:1] == ["stop"]:
        return _join("/cmd_stop", tail[1:])
    if head == "/cmd" and tail[:1] == ["status"]:
        return _join("/cmd_status", tail[1:])
    if head == "/cmd" and tail[:1] == ["jobs"]:
        return _join("/cmd_jobs", tail[1:])
    if head == "/cmd" and tail[:1] == ["reset"]:
        return _join("/cmd_reset", tail[1:])
    if head == "/cmd" and tail[:1] == ["top"]:
        return _join("/cmd_top", tail[1:])
    if head == "/conda" and tail[:1] == ["envs"]:
        return _join("/conda_envs", tail[1:])
    if head == "/conda" and tail[:1] == ["off"]:
        return _join("/conda_off", tail[1:])
    if head == "/git" and tail[:1] and tail[0] in {"add", "commit", "show", "push", "status", "diff", "log", "branch"}:
        return _join(f"/git_{tail[0]}", tail[1:])
    if head == "/trace" and tail[:1] == ["raw"]:
        return _join("/trace_raw", tail[1:])
    if head == "/trace" and tail[:1] == ["error"]:
        return _join("/trace_error", tail[1:])
    if head == "/codex" and tail[:2] and tail[:2] in (["api", "add"], ["api", "delete"], ["api", "list"], ["api", "switch"]):
        return _join(f"/codex_api_{tail[1]}", tail[2:])
    if head == "/codex" and tail[:1] == ["proxy"]:
        return _join("/codex_proxy", tail[1:])
    if head in {"/claude", "/claude_code", "/claude-code"} and tail[:2] and tail[:2] in (
        ["api", "add"],
        ["api", "delete"],
        ["api", "list"],
        ["api", "switch"],
    ):
        return _join(f"/claude_code_api_{tail[1]}", tail[2:])
    if head in {"/claude", "/claude_code", "/claude-code"} and tail[:1] == ["proxy"]:
        return _join("/claude_code_proxy", tail[1:])
    if head == "/context" and tail[:1] == ["handoff"]:
        return _join("/context_handoff", tail[1:])
    return raw


def build_app() -> FastAPI:
    app = FastAPI(title="Telegram Codex Bridge", version="0.1.0")

    db = Database(settings.database_path)
    audit = AuditService(db)
    workspace_guard = WorkspaceGuard(
        allowed_roots=settings.allowed_workspace_paths,
        default_workspace=settings.default_workspace,
    )

    backends = {
        "codex_cli_session": CodexCliSessionBackend(
            codex_bin=settings.codex_bin,
            codex_args=settings.codex_cli_args,
            timeout_seconds=settings.codex_message_timeout_seconds,
            proxy_url=settings.shared_effective_proxy_url,
            debug_mode=settings.codex_debug_mode,
            web_search_enabled=settings.codex_web_search_enabled,
        ),
        "claude_code_cli_session": ClaudeCodeSessionBackend(
            claude_bin=settings.claude_code_bin,
            claude_args=settings.claude_code_cli_args,
            thinking_mode=settings.claude_code_thinking_mode,
            timeout_seconds=settings.codex_message_timeout_seconds,
            proxy_url=settings.claude_code_effective_proxy_url,
        ),
        "codex_sdk": CodexSdkBackend(),
    }

    session_service = SessionService(
        db=db,
        audit_service=audit,
        workspace_guard=workspace_guard,
        backends=backends,
        default_mode=settings.default_codex_mode,
        default_claude_code_provider_label=settings.default_claude_code_provider_label,
    )
    conversation_history = ConversationHistoryService(settings.conversation_history_dir)
    claude_proxy_service = ClaudeCodeProxyService(
        enabled=settings.claude_code_proxy_enabled,
        listen_host=settings.claude_code_proxy_host,
        listen_port=settings.claude_code_proxy_port,
        upstream_base=settings.claude_code_proxy_upstream_base,
        upstream_key=settings.claude_code_proxy_upstream_key,
        proxy_url=settings.shared_effective_proxy_url,
    )
    shell_service = ShellService(
        settings.default_workspace,
        timeout_seconds=settings.codex_message_timeout_seconds,
        db=db,
    )
    telegram = TelegramAdapter(
        settings.telegram_bot_token,
        chunk_size=settings.telegram_long_message_chunk,
        debug=settings.telegram_debug_mode,
        proxy_url=settings.shared_effective_proxy_url,
    )

    app.state.settings = settings
    app.state.db = db
    app.state.audit = audit
    app.state.session_service = session_service
    app.state.shell_service = shell_service
    app.state.telegram = telegram
    app.state.conversation_history = conversation_history
    app.state.claude_proxy_service = claude_proxy_service
    app.state.telegram_offset = None
    app.state.poll_task = None
    app.state.shell_notify_task = None
    app.state.service_restart_task = None
    app.state.active_chats = set()
    app.state.chat_locks = {}
    app.state.update_tasks = set()

    def _runtime_proxy_url(mode: str) -> str | None:
        backend = backends[mode]
        raw = getattr(backend, "proxy_url", None)
        if not isinstance(raw, str):
            return None
        value = raw.strip()
        return value or None

    def _runtime_proxy_enabled(mode: str) -> bool:
        return _runtime_proxy_url(mode) is not None

    def _set_runtime_proxy(mode: str, enabled: bool) -> str | None:
        backend = backends[mode]
        if enabled:
            proxy_url = settings.shared_effective_proxy_url
            if not proxy_url:
                raise ValueError("Shared proxy is not configured.")
            backend.proxy_url = proxy_url
            return proxy_url
        backend.proxy_url = None
        return None

    def _is_session_reset_command(text: str) -> bool:
        command = text.split(maxsplit=1)[0].lower()
        return command in {"/new", "/reset"}

    def _drop_stale_updates_after_reset(
        normalized_updates: list[tuple[dict, TelegramInboundMessage]],
    ) -> list[tuple[dict, TelegramInboundMessage]]:
        last_reset_index_by_chat: dict[int, int] = {}
        for idx, (_, normalized) in enumerate(normalized_updates):
            if _is_session_reset_command(normalized.text):
                last_reset_index_by_chat[normalized.chat_id] = idx

        if not last_reset_index_by_chat:
            return normalized_updates

        filtered: list[tuple[dict, TelegramInboundMessage]] = []
        dropped_by_chat: dict[int, int] = {}
        for idx, item in enumerate(normalized_updates):
            normalized = item[1]
            reset_index = last_reset_index_by_chat.get(normalized.chat_id)
            if reset_index is not None and idx < reset_index:
                dropped_by_chat[normalized.chat_id] = dropped_by_chat.get(normalized.chat_id, 0) + 1
                continue
            filtered.append(item)

        if dropped_by_chat:
            logger.warning(
                "dropped stale cached updates before latest /new or /reset",
                extra={"dropped_by_chat": dropped_by_chat},
            )
        return filtered

    def _is_local_bypass_command(text: str) -> bool:
        command = normalize_command_alias(text).split(maxsplit=1)[0].lower()
        return command in {
            "/status",
            "/trace",
            "/trace_raw",
            "/trace_error",
            "/cancel",
            "/help",
            "/resend",
            "/pwd",
            "/mode",
            "/debug",
            "/restart_service",
            "/workspace",
            "/workspaces",
            "/session",
            "/session_clear",
            "/session_delete",
            "/session_label",
            "/codex_api_add",
            "/codex_api_delete",
            "/codex_api_list",
            "/codex_api_switch",
            "/codex_proxy",
            "/claude_code_api_add",
            "/claude_code_api_delete",
            "/claude_code_api_list",
            "/claude_code_api_switch",
            "/claude_code_proxy",
            "/coder_backend",
            "/git_add",
            "/git_commit",
            "/git_show",
            "/git_push",
            "/git_status",
            "/git_diff",
            "/git_log",
            "/git_branch",
            "/ls",
            "/tree",
            "/read",
            "/tail",
            "/find",
            "/grep",
            "/show",
            "/download",
            "/cmd_top",
            "/gpu",
            "/cmd",
            "/cmd_bg",
            "/cmd_bg_all",
            "/cmd_job_all",
            "/cmd_bg_delete",
            "/cmd_bg_clear",
            "/cmd_jobs",
            "/cmd_stop",
            "/cmd_stop_all",
            "/log",
            "/watch",
            "/cmd_status",
            "/cmd_reset",
        }

    def _chat_lock(chat_id: int) -> asyncio.Lock:
        lock = app.state.chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            app.state.chat_locks[chat_id] = lock
        return lock

    async def _process_single_update(normalized: TelegramInboundMessage, source: str) -> None:
        effective_text = normalize_command_alias(normalized.text)
        logger.info(
            "processing telegram update",
            extra={
                "update_id": normalized.update_id,
                "chat_id": normalized.chat_id,
                "source": source,
            },
        )
        app.state.audit.log(
            "telegram_update",
            normalized.chat_id,
            None,
            {"update_id": normalized.update_id, "source": source, "text": normalized.text[:1000]},
        )
        t0 = asyncio.get_running_loop().time()

        async def _run() -> str:
            logger.debug(
                "update stage=handle_chat_text start",
                extra={"update_id": normalized.update_id, "chat_id": normalized.chat_id},
            )
            reply = await handle_chat_text(normalized.chat_id, effective_text)
            t1 = asyncio.get_running_loop().time()
            logger.debug(
                "update stage=handle_chat_text done",
                extra={
                    "update_id": normalized.update_id,
                    "chat_id": normalized.chat_id,
                    "elapsed_ms": int((t1 - t0) * 1000),
                    "reply_chars": len(reply),
                },
            )
            logger.debug(
                "update stage=send_text start",
                extra={"update_id": normalized.update_id, "chat_id": normalized.chat_id},
            )
            if not reply.strip():
                logger.info(
                    "skipping telegram send for empty reply",
                    extra={"update_id": normalized.update_id, "chat_id": normalized.chat_id},
                )
                return reply
            if _is_local_bypass_command(effective_text):
                if effective_text.split(maxsplit=1)[0].lower() == "/resend":
                    await telegram.send_codex_reply(normalized.chat_id, reply)
                else:
                    markdown = _render_local_markdown(effective_text, reply)
                    if markdown is not None:
                        await telegram.send_markdown(normalized.chat_id, markdown)
                    else:
                        await telegram.send_markdown_card(normalized.chat_id, effective_text[:80], reply)
            else:
                await telegram.send_codex_reply(normalized.chat_id, reply)
            t2 = asyncio.get_running_loop().time()
            logger.debug(
                "update stage=send_text done",
                extra={
                    "update_id": normalized.update_id,
                    "chat_id": normalized.chat_id,
                    "elapsed_ms": int((t2 - t1) * 1000),
                    "total_elapsed_ms": int((t2 - t0) * 1000),
                },
            )
            return reply

        if _is_local_bypass_command(effective_text):
            await _run()
            return

        async with _chat_lock(normalized.chat_id):
            await _run()

    async def process_updates(updates: list[dict], source: str) -> None:
        normalized_updates: list[tuple[dict, TelegramInboundMessage]] = []
        for raw_update in updates:
            normalized = telegram.normalize_update(raw_update)
            if normalized:
                normalized_updates.append((raw_update, normalized))

        filtered_updates = _drop_stale_updates_after_reset(normalized_updates)
        for _, normalized in filtered_updates:
            task = asyncio.create_task(_process_single_update(normalized, source))
            app.state.update_tasks.add(task)
            task.add_done_callback(app.state.update_tasks.discard)

    app.state.telegram_update_processor = process_updates

    def _parse_key_values(text: str) -> tuple[dict[str, str], str]:
        values: dict[str, str] = {}
        extra_lines: list[str] = []
        for line in text.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                if key and "\n" not in value:
                    values[key.strip()] = value.strip()
                    continue
            extra_lines.append(line)
        return values, "\n".join(extra_lines).strip()

    def _md(text: str) -> str:
        return telegram.escape_markdown_v2(text)

    def _code(text: str) -> str:
        return telegram.escape_inline_code(text)

    def _kv_line(key: str, value: str) -> str:
        return f"*{_md(key)}:* `{_code(value)}`"

    def _code_block(text: str) -> str:
        escaped = text.replace("\\", "\\\\").replace("`", "\\`")
        return f"```text\n{escaped}\n```"

    def _render_local_markdown(command_text: str, reply: str) -> str | None:
        cmd = command_text.split(maxsplit=1)[0].lower()
        command_lower = command_text.lower()
        kv, extra = _parse_key_values(reply)

        def _title(text: str) -> str:
            return f"*{_md(text)}*"

        def _section(text: str) -> str:
            return f"*{_md(text)}*"

        def _bullet_code(text: str) -> str:
            return f"• `{_code(text)}`"

        def _bullet_text(text: str) -> str:
            return f"• {_md(text)}"

        def _divider() -> str:
            return "────────"

        def _inline_kv(key: str, value: str) -> str:
            return f"*{_md(key)}* `{_code(value)}`"

        def _truncate_text(text: str, limit: int) -> str:
            text = text.strip()
            if len(text) <= limit:
                return text
            return f"{text[: max(0, limit - 3)].rstrip()}..."

        def _project_name(path_text: str) -> str:
            try:
                return Path(path_text).name or path_text
            except Exception:  # noqa: BLE001
                return path_text

        def _status_chip(name: str, value: str) -> str:
            return f"`{_code(name)}={_code(value)}`"

        def _summarize_git_status(
            status_text: str,
        ) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]], str | None]:
            branch_line: str | None = None
            staged: list[tuple[str, str]] = []
            unstaged: list[tuple[str, str]] = []
            untracked: list[tuple[str, str]] = []
            for raw_line in status_text.splitlines():
                line = raw_line.rstrip()
                if not line:
                    continue
                if line.startswith("## "):
                    branch_line = line[3:]
                    continue
                if line.startswith("?? "):
                    untracked.append(("??", line[3:]))
                    continue
                if len(line) >= 3:
                    x = line[0]
                    y = line[1]
                    path = line[3:].strip()
                    if x not in {" ", "?"}:
                        staged.append((x, path))
                    if y not in {" "}:
                        unstaged.append((y, path))
            return staged, unstaged, untracked, branch_line

        def _render_path_list(
            title: str,
            items: list[tuple[str, str]],
            limit: int = 8,
            show_status: bool = True,
        ) -> list[str]:
            if not items:
                return []
            lines = [f"*{_md(title)}*"]
            shown = items[:limit]
            for status, item in shown:
                if show_status:
                    lines.append(f"• `{_code(status)}` `{_code(item)}`")
                else:
                    lines.append(f"• `{_code(item)}`")
            if len(items) > limit:
                lines.append(f"_and {len(items) - limit} more_")
            return lines

        if cmd == "/help":
            lines = [line.strip() for line in reply.splitlines() if line.strip()]
            if len(lines) <= 1:
                return None
            categories: list[tuple[str, list[tuple[str, str]]]] = [
                ("Session", []),
                ("Backend", []),
                ("Git", []),
                ("Files", []),
                ("Media", []),
                ("Shell", []),
                ("System", []),
            ]
            def _bucket(name: str) -> list[tuple[str, str]]:
                lowered = name.lower()
                if lowered.startswith("/session") or lowered in {
                    "/new",
                    "/reset",
                    "/status",
                    "/workspace",
                    "/workspaces",
                    "/pwd",
                    "/mode",
                    "/backend",
                }:
                    return categories[0][1]
                if lowered in {
                    "/codex api add",
                    "/codex api delete",
                    "/codex api list",
                    "/codex api switch",
                    "/codex proxy",
                    "/claude api add",
                    "/claude api delete",
                    "/claude api list",
                    "/claude api switch",
                    "/claude proxy",
                }:
                    return categories[1][1]
                if lowered.startswith("/trace"):
                    return categories[1][1]
                if lowered == "/cancel":
                    return categories[1][1]
                if lowered == "/timeout":
                    return categories[1][1]
                if lowered == "/context_handoff":
                    return categories[1][1]
                if lowered.startswith("/git "):
                    return categories[2][1]
                if lowered in {"/ls", "/tree", "/read", "/tail", "/find", "/grep"}:
                    return categories[3][1]
                if lowered in {"/show", "/download"}:
                    return categories[4][1]
                if lowered.startswith("/cmd") or lowered in {"/log", "/watch", "/conda", "/conda envs", "/conda off"}:
                    return categories[5][1]
                return categories[6][1]
            for line in lines[1:]:
                if " - " in line:
                    name, desc = line.split(" - ", 1)
                    _bucket(name).append((name, desc))
            body = [_title("RemoteCoder Commands")]
            for section, items in categories:
                if not items:
                    continue
                body.append("")
                body.append(_section(section))
                for name, desc in items:
                    body.append(f"`{_code(name)}`")
                    body.append(f"_{_md(desc)}_")
            return "\n".join(body)

        if cmd == "/status":
            verbose = " verbose" in command_lower or " detail" in command_lower or " full" in command_lower
            transcript = kv.pop("latest_reply", "")
            if not transcript and "latest_reply:\n" in extra:
                _, transcript = extra.split("latest_reply:\n", 1)
                extra = ""
            blocks = [_title("Session Overview")]
            project = _project_name(kv.get("workspace", ""))
            headline_bits: list[str] = []
            if project:
                headline_bits.append(_status_chip("project", project))
            if "reply_state" in kv:
                headline_bits.append(_status_chip("reply", kv["reply_state"]))
            if "session_status" in kv:
                headline_bits.append(_status_chip("session", kv["session_status"]))
            if "mode" in kv:
                headline_bits.append(_status_chip("mode", kv["mode"]))
            if "provider_label" in kv:
                headline_bits.append(_status_chip("provider", kv["provider_label"]))
            if headline_bits:
                blocks.append(" ".join(headline_bits))
            meta_lines: list[str] = []
            if "session_label" in kv and kv["session_label"] not in {"", "(none)"}:
                meta_lines.append(_inline_kv("label", kv["session_label"]))
            if "thread_id" in kv and kv["thread_id"] not in {"None", ""}:
                meta_lines.append(_inline_kv("thread", kv["thread_id"]))
            if "session_id" in kv:
                meta_lines.append(_inline_kv("session", kv["session_id"]))
            if "timeout_seconds" in kv:
                meta_lines.append(_inline_kv("timeout", kv["timeout_seconds"]))
            if "context_handoff" in kv:
                meta_lines.append(_inline_kv("context_handoff", kv["context_handoff"]))
            if "provider_model" in kv and kv["provider_model"] not in {"", "(codex default)", "(empty)"}:
                meta_lines.append(_inline_kv("provider_model", kv["provider_model"]))
            if meta_lines:
                blocks.append("")
                blocks.extend(meta_lines)
            for key in ["session_label", "reply_state", "session_status", "mode", "provider_label", "provider_model"]:
                kv.pop(key, None)
            if "workspace" in kv:
                blocks.append("")
                blocks.append(_section("Workspace"))
                blocks.append(_bullet_code(kv["workspace"]))
            shell_lines = []
            for key in ["active_jobs", "shell_busy", "shell_cwd", "latest_job_id", "shell_last_exit_code"]:
                if key in kv:
                    shell_lines.append(_inline_kv(key, kv[key]))
            if shell_lines:
                blocks.append("")
                blocks.append(_section("Shell"))
                blocks.extend(shell_lines)
            if verbose:
                detail_lines = []
                for key in ["session_id", "transcript_exists", "transcript_path", "last_return_code"]:
                    if key in kv and (key != "transcript_path" or kv.get("transcript_exists") == "True"):
                        detail_lines.append(_inline_kv(key, kv[key]))
                if detail_lines:
                    blocks.append("")
                    blocks.append(_section("Details"))
                    blocks.extend(detail_lines)
            elif transcript:
                blocks.append("")
                blocks.append(_section("Latest Reply"))
                preview = transcript.splitlines()
                short_preview = "\n".join(preview[:8]).strip()
                if len(preview) > 8:
                    short_preview = f"{short_preview}\n..."
                short_preview = _truncate_text(short_preview, 900)
                blocks.append(_code_block(short_preview))
            elif extra:
                blocks.append("")
                blocks.append(_code_block(extra))
            if verbose and transcript:
                blocks.append("")
                blocks.append(_section("Latest Reply"))
                blocks.append(_code_block(transcript))
            return "\n".join(blocks)

        if cmd in {"/workspace", "/workspaces"}:
            title = _title("Workspace") if cmd == "/workspace" else _title("Allowed Workspaces")
            blocks = [title]
            workspace_value = kv.get("workspace") or kv.get("current_workspace")
            if workspace_value:
                blocks.append(_status_chip("project", _project_name(workspace_value)))
            for key in ["current_workspace", "workspace", "session_id", "session_label", "label", "default_workspace"]:
                if key in kv:
                    blocks.append(_inline_kv(key, kv[key]))
            allowed = []
            if "allowed_roots" in extra:
                lines = [line.strip() for line in extra.splitlines() if line.strip() and line.strip() != "allowed_roots:"]
                allowed.extend(lines)
            elif extra:
                extra_lines = [line.strip() for line in extra.splitlines() if line.strip()]
                if extra_lines:
                    blocks.append("")
                    blocks.append(_section("Message"))
                    for line in extra_lines:
                        blocks.append(_bullet_text(line))
            if allowed:
                blocks.append("")
                blocks.append(_section("Roots"))
                for item in allowed:
                    blocks.append(_bullet_code(item))
            if len(blocks) == 1:
                blocks.append(_bullet_text("No workspace details available."))
            return "\n".join(blocks)

        if cmd == "/session":
            blocks = [_title("Sessions")]
            current_session_id = kv.get("current_session_id")
            current_workspace = kv.get("current_workspace")
            if current_session_id:
                blocks.append(_inline_kv("current_session", current_session_id))
            if current_workspace:
                blocks.append(_inline_kv("current_workspace", current_workspace))
            if extra:
                lines = [line.strip() for line in extra.splitlines() if line.strip()]
                if lines:
                    blocks.append("")
                    blocks.append(_section("History"))
                    blocks.extend(_bullet_code(line) for line in lines)
            return "\n".join(blocks)

        if cmd in {"/cmd_status", "/cmd_jobs", "/log", "/watch"}:
            title_map = {
                "/cmd_status": _title("Shell Status"),
                "/cmd_jobs": _title("Shell Jobs"),
                "/log": _title("Shell Logs"),
                "/watch": _title("Training Watch"),
            }
            title = title_map[cmd]
            blocks = [title]
            chips = []
            for key, short in [
                ("conda_env", "conda"),
                ("active_jobs", "jobs"),
                ("latest_job_id", "latest"),
                ("shell_busy", "busy"),
                ("shell_last_exit_code", "last_exit"),
                ("job_id", "job"),
                ("pid", "pid"),
                ("status", "status"),
            ]:
                if key in kv:
                    chips.append(_status_chip(short, kv[key]))
            if chips:
                blocks.append(" ".join(chips))
            if "shell_cwd" in kv:
                blocks.append("")
                blocks.append(_section("Current Directory"))
                blocks.append(_bullet_code(kv["shell_cwd"]))
            if cmd in {"/log", "/watch"}:
                meta_lines = []
                for key in ["label", "cwd", "log_path", "showing_last", "matched_lines", "keywords"]:
                    if key in kv:
                        meta_lines.append(_inline_kv(key, kv[key]))
                if meta_lines:
                    blocks.append("")
                    blocks.append(_section("Metadata"))
                    blocks.extend(meta_lines)
                if extra:
                    blocks.append("")
                    blocks.append(_section("Matched Progress" if cmd == "/watch" else "Log Tail"))
                    blocks.append(_code_block(extra))
                return "\n".join(blocks)
            if extra:
                job_lines = [line.strip() for line in extra.splitlines() if line.strip()]
                jobs: list[tuple[str, str]] = []
                for line in job_lines:
                    if line.startswith("#"):
                        jobs.append((line, ""))
                    elif line.startswith("cmd: ") and jobs:
                        jobs[-1] = (jobs[-1][0], line[5:])
                if jobs:
                    running_jobs = [item for item in jobs if " running" in item[0]]
                    finished_jobs = [item for item in jobs if " running" not in item[0]]
                    blocks.append("")
                    if running_jobs:
                        blocks.append(_section("Running"))
                        for header, command in running_jobs[:6]:
                            blocks.append(f"• `{_code(_truncate_text(header, 110))}`")
                            if command:
                                blocks.append(f"  `{_code(_truncate_text(command, 110))}`")
                    if finished_jobs:
                        if running_jobs:
                            blocks.append("")
                        blocks.append(_section("Recent"))
                        for header, command in finished_jobs[:6]:
                            blocks.append(f"• `{_code(_truncate_text(header, 110))}`")
                            if command:
                                blocks.append(f"  `{_code(_truncate_text(command, 110))}`")
                    if len(jobs) > 12:
                        blocks.append("")
                        blocks.append(_md(f"showing 12 of {len(jobs)} jobs"))
                elif cmd == "/cmd_status":
                    blocks.append("")
                    blocks.append(_code_block(extra))
            return "\n".join(blocks)

        if cmd in {"/trace", "/trace_raw", "/trace_error"}:
            title_map = {
                "/trace": _title("Backend Trace"),
                "/trace_raw": _title("Backend Trace Raw"),
                "/trace_error": _title("Backend Error Trace"),
            }
            title = title_map[cmd]
            blocks = [title]
            for key in [
                "running",
                "cancel_requested",
                "active_pid",
                "thread_id",
                "event_count",
                "timeout_seconds",
                "last_return_code",
                "current_started_at",
                "current_finished_at",
            ]:
                if key in kv:
                    blocks.append(_inline_kv(key, kv[key]))
            for key, heading in [("current_prompt", "Prompt"), ("latest_reply_preview", "Reply Preview")]:
                if key in kv and kv[key]:
                    blocks.append("")
                    blocks.append(_section(heading))
                    blocks.append(_code_block(kv[key]))
            if extra:
                blocks.append("")
                blocks.append(_section("Events"))
                blocks.append(_code_block(extra))
            return "\n".join(blocks)

        if cmd in {"/conda", "/conda_envs", "/conda_off"}:
            title_map = {
                "/conda": _title("Conda"),
                "/conda_envs": _title("Conda Environments"),
                "/conda_off": _title("Conda"),
            }
            blocks = [title_map[cmd]]
            for key in ["conda_env", "selected_conda_env", "previous", "path", "shell_cwd"]:
                if key in kv:
                    blocks.append(_inline_kv(key, kv[key]))
            if extra:
                extra_lines = [line.strip() for line in extra.splitlines() if line.strip()]
                if extra_lines:
                    blocks.append("")
                    section = "Available" if cmd == "/conda_envs" else "Message"
                    blocks.append(_section(section))
                    for line in extra_lines:
                        blocks.append(_bullet_code(line) if cmd == "/conda_envs" else _bullet_text(line))
            return "\n".join(blocks)

        if cmd == "/git_status":
            blocks = [_title("Git Status")]
            repo = kv.get("repo", "")
            branch = kv.get("branch", "")
            header_chips = []
            if repo:
                header_chips.append(_status_chip("repo", _project_name(repo)))
            if branch:
                header_chips.append(_status_chip("branch", branch))
            if header_chips:
                blocks.append(" ".join(header_chips))
            for key in ["repo", "branch"]:
                if key in kv:
                    blocks.append(_inline_kv(key, kv[key]))
            if extra:
                staged, unstaged, untracked, branch_line = _summarize_git_status(extra)
                if branch_line:
                    blocks.append(_inline_kv("head", branch_line))
                counts = " ".join(
                    [
                        _status_chip("staged", str(len(staged))),
                        _status_chip("unstaged", str(len(unstaged))),
                        _status_chip("untracked", str(len(untracked))),
                    ]
                )
                blocks.append("")
                blocks.append(counts)
                sections = []
                sections.extend(_render_path_list("Staged Changes", staged, show_status=False))
                sections.extend(_render_path_list("Modified Files", unstaged, show_status=False))
                sections.extend(_render_path_list("New Files", untracked, show_status=False))
                if sections:
                    blocks.append("")
                    blocks.extend(sections)
                else:
                    blocks.append("")
                    blocks.append("_working tree clean_")
            return "\n".join(blocks)

        if cmd in {"/git_diff", "/git_log", "/git_branch", "/git_show"}:
            title_map = {
                "/git_diff": _title("Git Diff"),
                "/git_log": _title("Git Log"),
                "/git_branch": _title("Git Branches"),
                "/git_show": _title("Git Show"),
            }
            blocks = [title_map[cmd]]
            for key in ["repo", "path", "ref"]:
                if key in kv:
                    blocks.append(_inline_kv(key, kv[key]))
            rest = extra
            if rest:
                blocks.append("")
                blocks.append(_code_block(rest))
            return "\n".join(blocks)

        return None

    async def shell_job_notify_loop() -> None:
        logger.info("shell job notify loop started")
        while True:
            try:
                notifications = await asyncio.to_thread(shell_service.collect_finished_notifications, 20)
                for item in notifications:
                    job = item["job"]
                    state_label = f"exit={job['return_code']}"
                    body = item["output"] or "(log is empty)"
                    label_line = f"label: {job['label']}\n" if job.get("label") else ""
                    message = (
                        f"Background job #{job['job_id']} finished.\n"
                        f"{label_line}"
                        f"status: {state_label}\n"
                        f"pid: {job['pid']}\n"
                        f"cwd: {job['cwd']}\n"
                        f"log_path: {job['log_path']}\n"
                        f"showing_last: {item['shown_lines']} of {item['line_count']} lines\n\n"
                        f"{body}"
                    )
                    await telegram.send_markdown_card(item["chat_id"], f"job #{job['job_id']} finished", message)
            except Exception:  # noqa: BLE001
                logger.exception("shell job notify iteration failed")
            await asyncio.sleep(2.0)

    async def handle_chat_text(chat_id: int, text: str) -> str:
        if text.startswith("/"):
            return await handle_command(chat_id, normalize_command_alias(text))
        try:
            app.state.active_chats.add(chat_id)
            logger.debug("chat pipeline stage=session_send start", extra={"chat_id": chat_id, "text_len": len(text)})
            started = asyncio.get_running_loop().time()
            session, output = await asyncio.to_thread(session_service.send_chat_message, chat_id, text)
            reply = await asyncio.to_thread(conversation_history.extract_reply, output)
            await asyncio.to_thread(
                conversation_history.persist_turn,
                chat_id=chat_id,
                session_id=session["session_id"],
                user_text=text,
                backend_raw_stream=output,
                telegram_reply=reply,
            )
            await asyncio.to_thread(session_service.mark_last_good_session, session["session_id"])
            elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            logger.debug(
                "chat pipeline stage=session_send done",
                extra={
                    "chat_id": chat_id,
                    "session_id": session["session_id"],
                    "mode": session["integration_mode"],
                    "elapsed_ms": elapsed_ms,
                    "output_chars": len(output),
                    "reply_chars": len(reply),
                },
            )
            return reply
        except CodexReplyCancelled:
            logger.info("backend reply cancelled", extra={"chat_id": chat_id})
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed handling chat message")
            return f"Error talking to coder backend: {exc}"
        finally:
            app.state.active_chats.discard(chat_id)

    async def handle_command(chat_id: int, text: str) -> str:
        parts = text.split(maxsplit=1)
        cmd = parts[0]
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        session = session_service.get_chat(chat_id)
        command_workspace = settings.default_workspace if session is None else session["workspace_path"]

        def _shell_session() -> dict:
            return session_service.get_or_create_chat_session(chat_id)

        def _shell_status_text(status: dict) -> str:
            return (
                f"shell_exists: {status['exists']}\n"
                f"shell_busy: {status['busy']}\n"
                f"shell_cwd: {status.get('cwd') or status['workspace']}\n"
                f"conda_env: {status.get('conda_env', '(default)')}\n"
                f"shell_last_exit_code: {status['last_exit_code']}\n"
                f"active_jobs: {len(status.get('active_job_ids', []))}\n"
                f"latest_job_id: {status.get('latest_job_id')}"
            )

        def _backend_alias_to_mode(raw_mode: str) -> str:
            normalized = raw_mode.strip().lower()
            mapping = {
                "codex": "codex_cli_session",
                "codex_cli_session": "codex_cli_session",
                "claude_code": "claude_code_cli_session",
                "claude": "claude_code_cli_session",
                "claude_code_cli_session": "claude_code_cli_session",
            }
            if normalized not in mapping:
                raise ValueError(f"Unknown backend: {raw_mode}")
            return mapping[normalized]

        def _mode_display_name(mode: str) -> str:
            if mode == "claude_code_cli_session":
                return "Claude Code"
            if mode == "codex_cli_session":
                return "Codex"
            return mode

        def _provider_default_values(mode: str) -> tuple[str, str]:
            if mode == "claude_code_cli_session":
                provider = session_service._configured_default_provider_record(mode)
                if provider is not None:
                    return str(provider["model"]), str(provider["base_url"])
                return "(empty)", "(empty)"
            return "(codex default)", "(official)"

        def _provider_switch_warning(mode: str, backend_status: dict) -> str:
            if mode != "claude_code_cli_session":
                return ""
            if session_service._configured_default_provider_record(mode) is not None:
                return ""
            if (backend_status.get("provider_label") or "default") != "default":
                return ""
            return (
                "\nwarning: claude code default provider is empty.\n"
                "Add one with /claude_code_api_add <label> :: <model> :: <base_url> :: <api_key>\n"
                "Then switch with /claude_code_api_switch <label>"
            )

        async def _restart_service_later(delay_seconds: float = SERVICE_RESTART_DELAY_SECONDS) -> None:
            await asyncio.sleep(delay_seconds)
            cmd = build_restart_command()
            env = build_user_systemd_env()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
            except Exception:  # noqa: BLE001
                logger.exception("failed launching user systemd restart", extra={"chat_id": chat_id, "cmd": cmd})
                return
            if proc.returncode != 0:
                logger.error(
                    "user systemd restart command failed",
                    extra={
                        "chat_id": chat_id,
                        "cmd": cmd,
                        "return_code": proc.returncode,
                        "stdout": (stdout or b"").decode(errors="ignore")[-800:],
                        "stderr": (stderr or b"").decode(errors="ignore")[-800:],
                    },
                )
                return
            logger.warning("user systemd restart command completed", extra={"chat_id": chat_id, "cmd": cmd})

        def _parse_tail_and_job(raw: str) -> tuple[int | None, int]:
            if not raw:
                return None, 20
            tokens = raw.split()
            job_id: int | None = None
            lines = 20
            if tokens:
                try:
                    job_id = int(tokens[0])
                except ValueError:
                    try:
                        lines = int(tokens[0])
                    except ValueError:
                        job_id = None
            if len(tokens) >= 2:
                try:
                    lines = int(tokens[1])
                except ValueError:
                    lines = 20
            return job_id, max(1, min(lines, 200))

        def _parse_watch_args(raw: str) -> tuple[int | None, int, list[str]]:
            left, right = _split_with_label(raw)
            job_id, lines = _parse_tail_and_job(left)
            if not right.strip():
                return job_id, lines, []
            keywords = [item.strip() for item in right.replace("，", ",").split(",") if item.strip()]
            return job_id, lines, keywords

        def _split_args(raw_text: str) -> list[str]:
            return [token for token in raw_text.split() if token]

        def _split_with_label(raw_text: str) -> tuple[str, str]:
            if "::" not in raw_text:
                return raw_text.strip(), ""
            left, right = raw_text.split("::", 1)
            return left.strip(), right.strip()

        async def _run_local(fn, *args) -> str:
            try:
                return await asyncio.to_thread(fn, *args)
            except Exception as exc:  # noqa: BLE001
                return str(exc)

        async def _send_workspace_file(requested_path: str, caption_prefix: str, prefer_photo: bool) -> str:
            try:
                target = await asyncio.to_thread(shell_service.resolve_workspace_path, command_workspace, requested_path)
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            if not target.exists():
                return f"Path not found: {target}"
            if not target.is_file():
                return f"Path is not a file: {target}"

            image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
            caption = f"{caption_prefix}: {target.relative_to(Path(command_workspace).resolve())}"
            try:
                if prefer_photo and target.suffix.lower() in image_suffixes:
                    await telegram.send_photo(chat_id, target, caption=caption)
                else:
                    await telegram.send_document(chat_id, target, caption=caption)
            except Exception as exc:  # noqa: BLE001
                return f"Failed to send file: {exc}"
            return f"Sent file: {target}"

        def _build_context_handoff(source_session: dict, *, reason: str, target_mode: str, target_provider_label: str | None = None) -> str:
            source_status = session_service.get_session_status(source_session["session_id"])
            backend_status = source_status.get("backend_status", {})
            recent_turns = conversation_history.list_recent_turns(
                chat_id=chat_id,
                session_id=source_session["session_id"],
                limit=3,
            )
            lines = [
                "[Context Handoff]",
                "[Session Summary]",
                f"reason={reason}",
                f"source_mode={source_session['integration_mode']}",
                f"target_mode={target_mode}",
                f"workspace={source_session['workspace_path']}",
                f"session_label={source_session.get('label') or '(none)'}",
                f"provider_label={backend_status.get('provider_label') or 'default'}",
                f"provider_model={backend_status.get('provider_model') or '(default)'}",
            ]
            if target_provider_label:
                lines.append(f"target_provider_label={target_provider_label}")
            latest_reply = conversation_history.read_latest_reply(chat_id=chat_id, session_id=source_session["session_id"])
            if latest_reply and latest_reply != "(No transcript reply available.)":
                lines.extend(["", "[Latest Assistant Reply]", latest_reply[:1600]])
            if recent_turns:
                lines.extend(["", "[Recent Turns]"])
                for idx, turn in enumerate(recent_turns, start=1):
                    user_text = (turn.get("user_text") or "").strip() or "(none)"
                    reply_text = (turn.get("telegram_reply") or "").strip() or "(none)"
                    lines.append(f"[Turn {idx}]")
                    lines.append("[User]")
                    lines.append(user_text[:800])
                    lines.append("[Assistant]")
                    lines.append(reply_text[:800])
            lines.extend(["", "[End Context Handoff]"])
            return "\n".join(lines).strip()

        if cmd == "/help":
            return (
                "Available commands:\n"
                "\n"
                "Session commands:\n"
                "/new [label] - create a new session, optionally with a label\n"
                "/reset - reset current session\n"
                "/status [verbose] - show current session status\n"
                "/workspace [path] [:: label] - show or switch the current workspace\n"
                "/workspaces - list allowed workspace roots\n"
                "/session list - list this chat's sessions\n"
                "/session <session_id|tag> - switch to a session\n"
                "/session label <tag> - update the current session tag\n"
                "/session clear - clear this chat's sessions and create a fresh one\n"
                "/session delete <session_id|tag> - delete a specific non-current session\n"
                "/pwd - show current workspace\n"
                "/mode - show integration mode\n"
                "\n"
                "Backend commands:\n"
                "/backend <codex|claude_code> - switch the current backend and keep the same workspace\n"
                "/codex api add <label> :: <model> :: <base_url> :: <api_key> - add or update a Codex API provider\n"
                "/codex api delete <label> - delete a saved Codex API provider when no session is using it\n"
                "/codex api list - list available Codex API providers\n"
                "/codex api switch <label|default> - switch the current session's Codex provider\n"
                "/codex proxy [on|off] - show or toggle whether Codex uses the shared proxy\n"
                "/claude api add <label> :: <model> :: <base_url> :: <api_key> - add or update a Claude Code provider\n"
                "/claude api delete <label> - delete a saved Claude Code provider when no session is using it\n"
                "/claude api list - list available Claude Code providers\n"
                "/claude api switch <label|default> - switch the current session's Claude Code provider\n"
                "/claude proxy [on|off] - show or toggle whether Claude Code uses the shared proxy\n"
                "/trace [n] - show recent backend event summaries for the current session\n"
                "/trace raw [n] - show recent raw backend event lines\n"
                "/trace error [n] - show the most recent backend error context\n"
                "/cancel - stop the current backend reply for this chat\n"
                "/resend [n] - resend the nth most recent reply from the current session\n"
                "/timeout [seconds] - show or set the current reply timeout\n"
                "/context_handoff [off|light] - show or set cross-switch context handoff for this session\n"
                "\n"
                "Git commands:\n"
                "/git add <path> - stage a file or directory\n"
                "/git commit <message> - create a commit from staged changes\n"
                "/git show [ref] - show a commit with stats\n"
                "/git push [remote] [branch] - push current branch to remote\n"
                "/git status - show git branch and working tree status\n"
                "/git diff [path] - show git diff, optionally for one path\n"
                "/git log [n] - show recent commits\n"
                "/git branch - show local branches\n"
                "\n"
                "File commands:\n"
                "/ls [path] - list files in the current workspace\n"
                "/tree [path] [depth] - show a truncated directory tree\n"
                "/read <path> [start_line] [lines] - read part of a text file\n"
                "/tail <path> [lines] - tail a text file\n"
                "/find <pattern> [path] - find files by name under the workspace\n"
                "/grep <pattern> [path] - search text in workspace files\n"
                "\n"
                "Media commands:\n"
                "/show <path> - send an image or file back to Telegram\n"
                "/download <path> - send a file back to Telegram as a document\n"
                "\n"
                "Shell commands:\n"
                "/cmd top - show CPU, memory, disk, and hot processes\n"
                "/gpu - show GPU status from nvidia-smi\n"
                "/cmd <command> - run a direct shell command in a persistent per-chat shell\n"
                "/cmd bg <command> - start a long-running shell job in the background\n"
                "/cmd bg all - list background jobs across all sessions\n"
                "/cmd bg delete <job_id> - delete one background shell job and its log\n"
                "/cmd bg clear - delete all background shell jobs and logs for this chat\n"
                "/conda [env] - show or switch the current conda environment for shell commands\n"
                "/conda envs - list available conda environments\n"
                "/conda off - clear the selected conda environment\n"
                "/cmd jobs - list shell background jobs for this chat\n"
                "/cmd status [lines] - show shell status and latest job tail\n"
                "/log [job_id] [lines] - tail a shell job log\n"
                "/watch [job_id] [lines] [:: kw1,kw2] - show filtered training progress lines from a shell job log\n"
                "/cmd stop <job_id> - stop a background shell job\n"
                "/cmd stop all - stop all background shell jobs for this chat\n"
                "/cmd reset - reset the per-chat shell session\n"
                "\n"
                "System commands:\n"
                "/debug - show Telegram diagnostics summary\n"
                "/debug verbose - show detailed Telegram diagnostics\n"
                "/restart service - restart the RemoteCoder user service after a short delay\n"
                "/help - this help\n"
                "Legacy underscore commands still work as aliases."
            )
        if cmd == "/restart_service":
            existing_task = getattr(app.state, "service_restart_task", None)
            if existing_task and not existing_task.done():
                return (
                    "Service restart is already scheduled.\n"
                    f"unit: {SERVICE_UNIT_NAME}\n"
                    f"delay_seconds: {int(SERVICE_RESTART_DELAY_SECONDS)}"
                )
            app.state.service_restart_task = asyncio.create_task(_restart_service_later())
            return (
                "Service restart scheduled.\n"
                f"unit: {SERVICE_UNIT_NAME}\n"
                f"delay_seconds: {int(SERVICE_RESTART_DELAY_SECONDS)}\n"
                "This chat may disconnect during restart."
            )
        if cmd == "/new":
            raw = parts[1].strip() if len(parts) > 1 else ""
            session = session_service.new_session(chat_id=chat_id, label=raw)
            return (
                f"Created new session {session['session_id']} in {session['workspace_path']}\n"
                f"label: {session.get('label') or '(none)'}\n"
                f"mode: {session['integration_mode']}"
            )
        if cmd == "/reset":
            session = session_service.reset_chat_session(chat_id=chat_id)
            return f"Reset complete. New session: {session['session_id']}"
        if cmd == "/workspace":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                session = session_service.get_or_create_chat_session(chat_id)
                allowed = "\n".join(str(path) for path in settings.allowed_workspace_paths)
                return (
                    f"current_workspace: {session['workspace_path']}\n"
                    f"session_label: {session.get('label') or '(none)'}\n"
                    f"default_workspace: {settings.default_workspace}\n"
                    f"allowed_roots:\n{allowed}"
                )
            try:
                workspace_arg, label = _split_with_label(raw)
                if not workspace_arg:
                    return "Usage: /workspace <path> [:: label]"
                session = session_service.switch_chat_workspace(chat_id, workspace_arg, label or None)
                await asyncio.to_thread(shell_service.reset, session["session_id"], chat_id, session["workspace_path"])
                return (
                    f"Switched workspace.\n"
                    f"session_id: {session['session_id']}\n"
                    f"workspace: {session['workspace_path']}\n"
                    f"label: {session.get('label') or '(none)'}"
                )
            except Exception as exc:  # noqa: BLE001
                return str(exc)
        if cmd == "/workspaces":
            lines = [
                f"default_workspace: {settings.default_workspace}",
                "allowed_roots:",
            ]
            lines.extend(str(path) for path in settings.allowed_workspace_paths)
            return "\n".join(lines)
        if cmd == "/session":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw or raw.lower() == "list":
                current = session_service.get_or_create_chat_session(chat_id)
                sessions = session_service.list_chat_sessions(chat_id)
                lines = [
                    f"current_session_id: {current['session_id']}",
                    f"current_workspace: {current['workspace_path']}",
                    "sessions:",
                ]
                for item in sessions[:20]:
                    marker = "*" if item["session_id"] == current["session_id"] else "-"
                    label = item.get("label") or "(none)"
                    lines.append(
                        f"{marker} {item['session_id']} | {item['status']} | {item['integration_mode']} | "
                        f"{item['workspace_path']} | label={label}"
                    )
                if len(sessions) > 20:
                    lines.append(f"... and {len(sessions) - 20} more")
                lines.append("Use /session <session_id|tag> to switch.")
                return "\n".join(lines)
            if raw.lower().startswith("label "):
                label = raw[6:].strip()
                if not label:
                    return "Usage: /session label <tag>"
                session = session_service.set_session_label(chat_id, label)
                return (
                    "Updated session tag.\n"
                    f"session_id: {session['session_id']}\n"
                    f"label: {session.get('label') or '(none)'}"
                )
            if raw.lower() == "label":
                return "Usage: /session label <tag>"
            try:
                session = session_service.switch_chat_session(chat_id, raw)
                shell_sync = await asyncio.to_thread(
                    shell_service.sync_workspace,
                    session["session_id"],
                    chat_id,
                    session["workspace_path"],
                )
            except KeyError:
                return f"Session or tag not found: {raw}"
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            message = (
                "Switched current session.\n"
                f"session_id: {session['session_id']}\n"
                f"workspace: {session['workspace_path']}\n"
                f"label: {session.get('label') or '(none)'}\n"
                f"mode: {session['integration_mode']}"
            )
            if not shell_sync.get("workspace_applied"):
                message += (
                    f"\nshell_cwd: {shell_sync.get('cwd')}"
                    "\nnote: shell workspace was kept because background jobs are still active"
                )
            return message
        if cmd == "/session_clear":
            session = session_service.clear_chat_sessions(chat_id)
            await asyncio.to_thread(shell_service.reset, session["session_id"], chat_id, session["workspace_path"])
            return (
                "Cleared session history and created a fresh session.\n"
                f"session_id: {session['session_id']}\n"
                f"workspace: {session['workspace_path']}\n"
                f"label: {session.get('label') or '(none)'}\n"
                f"mode: {session['integration_mode']}"
            )
        if cmd == "/session_delete":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /session_delete <session_id|tag>"
            try:
                session_service.delete_chat_session(chat_id, raw)
            except KeyError:
                return f"Session or tag not found: {raw}"
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            return f"Deleted session: {raw}"
        if cmd == "/session_label":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /session label <tag>"
            session = session_service.set_session_label(chat_id, raw)
            return (
                f"Updated session tag.\n"
                f"session_id: {session['session_id']}\n"
                f"label: {session.get('label') or '(none)'}"
            )
        if cmd == "/coder_backend":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                session = session_service.get_or_create_chat_session(chat_id)
                return (
                    f"backend: {session['integration_mode']}\n"
                    "available: codex, claude_code"
                )
            try:
                source_session = session_service.get_or_create_chat_session(chat_id)
                target_mode = _backend_alias_to_mode(raw)
                status = session_service.switch_chat_backend(chat_id, target_mode)
                source_status = session_service.get_session_status(source_session["session_id"])
                if source_status.get("backend_status", {}).get("context_handoff_mode", "light") == "light":
                    handoff_text = _build_context_handoff(
                        source_session,
                        reason="backend_switch",
                        target_mode=target_mode,
                    )
                    session_service.queue_session_context_handoff(status["session_id"], handoff_text)
                    status = session_service.get_session_status(status["session_id"])
                shell_sync = await asyncio.to_thread(
                    shell_service.sync_workspace,
                    status["session_id"],
                    chat_id,
                    status["workspace_path"],
                )
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            backend_status = status.get("backend_status", {})
            provider_model_default, provider_base_default = _provider_default_values(target_mode)
            message = (
                "Backend switched.\n"
                f"mode: {status['integration_mode']}\n"
                f"provider_label: {backend_status.get('provider_label') or 'default'}\n"
                f"provider_model: {backend_status.get('provider_model') or provider_model_default}\n"
                f"provider_base_url: {backend_status.get('provider_base_url') or provider_base_default}\n"
                f"context_handoff: {backend_status.get('context_handoff_mode')}\n"
                f"context_queued: {'yes' if backend_status.get('pending_context_handoff') else 'no'}"
            )
            if not shell_sync.get("workspace_applied"):
                message += (
                    f"\nshell_cwd: {shell_sync.get('cwd')}"
                    "\nnote: shell workspace was kept because background jobs are still active"
                )
            return message + _provider_switch_warning(target_mode, backend_status)
        if cmd == "/codex_api_add":
            raw = parts[1].strip() if len(parts) > 1 else ""
            fields = [item.strip() for item in raw.split("::")]
            if len(fields) != 4 or not all(fields):
                return "Usage: /codex_api_add <label> :: <model> :: <base_url> :: <api_key>"
            try:
                provider = session_service.add_codex_provider(
                    label=fields[0],
                    model=fields[1],
                    base_url=fields[2],
                    api_key=fields[3],
                )
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            return (
                "Codex API provider saved.\n"
                f"label: {provider['label']}\n"
                f"model: {provider['model']}\n"
                f"base_url: {provider['base_url']}"
            )
        if cmd == "/claude_code_api_add":
            raw = parts[1].strip() if len(parts) > 1 else ""
            fields = [item.strip() for item in raw.split("::")]
            if len(fields) != 4 or not all(fields):
                return "Usage: /claude_code_api_add <label> :: <model> :: <base_url> :: <api_key>"
            try:
                provider = session_service.add_claude_code_provider(
                    label=fields[0],
                    model=fields[1],
                    base_url=fields[2],
                    api_key=fields[3],
                )
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            return (
                "Claude Code API provider saved.\n"
                f"label: {provider['label']}\n"
                f"model: {provider['model']}\n"
                f"base_url: {provider['base_url']}"
            )
        if cmd == "/codex_api_delete":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /codex_api_delete <label>"
            try:
                deleted_label = session_service.delete_codex_provider(raw)
            except KeyError:
                return f"Codex provider not found: {raw}"
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            return f"Codex API provider deleted.\nlabel: {deleted_label}"
        if cmd == "/claude_code_api_delete":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /claude_code_api_delete <label>"
            try:
                deleted_label = session_service.delete_claude_code_provider(raw)
            except KeyError:
                return f"Claude Code provider not found: {raw}"
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            return f"Claude Code API provider deleted.\nlabel: {deleted_label}"
        if cmd == "/codex_api_list":
            providers = session_service.list_codex_providers()
            session = session_service.get_or_create_chat_session(chat_id)
            current_label = "default"
            if session["integration_mode"] == "codex_cli_session":
                status = session_service.get_session_status(session["session_id"])
                current_label = status.get("backend_status", {}).get("provider_label") or "default"
            lines = [f"current_provider: {current_label}", f"providers: {len(providers)}"]
            for item in providers:
                marker = "*" if item["label"] == current_label else "-"
                lines.append(
                    f"{marker} {item['label']} | model={item['model']} | base_url={item['base_url']}"
                )
            return "\n".join(lines)
        if cmd == "/claude_code_api_list":
            providers = session_service.list_claude_code_providers()
            session = session_service.get_or_create_chat_session(chat_id)
            current_label = "default"
            if session["integration_mode"] == "claude_code_cli_session":
                status = session_service.get_session_status(session["session_id"])
                current_label = status.get("backend_status", {}).get("provider_label") or "default"
            lines = [f"current_provider: {current_label}", f"providers: {len(providers)}"]
            for item in providers:
                marker = "*" if item["label"] == current_label else "-"
                lines.append(
                    f"{marker} {item['label']} | model={item['model']} | base_url={item['base_url']}"
                )
            return "\n".join(lines)
        if cmd == "/codex_api_switch":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /codex_api_switch <label|default>"
            try:
                source_session = session_service.get_or_create_chat_session(chat_id)
                source_status = session_service.get_session_status(source_session["session_id"])
                status = session_service.switch_chat_codex_provider(chat_id, raw)
                if source_status.get("backend_status", {}).get("context_handoff_mode", "light") == "light":
                    handoff_text = _build_context_handoff(
                        source_session,
                        reason="provider_switch",
                        target_mode="codex_cli_session",
                        target_provider_label=raw.strip() or "default",
                    )
                    session_service.queue_session_context_handoff(status["session_id"], handoff_text)
                    status = session_service.get_session_status(status["session_id"])
            except KeyError:
                return f"Codex provider not found: {raw}"
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            backend_status = status.get("backend_status", {})
            return (
                "Codex provider switched.\n"
                f"provider_label: {backend_status.get('provider_label') or 'default'}\n"
                f"provider_model: {backend_status.get('provider_model') or '(codex default)'}\n"
                f"provider_base_url: {backend_status.get('provider_base_url') or '(official)'}\n"
                "thread_reset: True\n"
                f"context_handoff: {backend_status.get('context_handoff_mode')}\n"
                f"context_queued: {'yes' if backend_status.get('pending_context_handoff') else 'no'}"
            )
        if cmd == "/claude_code_api_switch":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /claude_code_api_switch <label|default>"
            try:
                source_session = session_service.get_or_create_chat_session(chat_id)
                source_status = session_service.get_session_status(source_session["session_id"])
                status = session_service.switch_chat_claude_code_provider(chat_id, raw)
                if source_status.get("backend_status", {}).get("context_handoff_mode", "light") == "light":
                    handoff_text = _build_context_handoff(
                        source_session,
                        reason="provider_switch",
                        target_mode="claude_code_cli_session",
                        target_provider_label=raw.strip() or "default",
                    )
                    session_service.queue_session_context_handoff(status["session_id"], handoff_text)
                    status = session_service.get_session_status(status["session_id"])
            except KeyError:
                return f"Claude Code provider not found: {raw}"
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            backend_status = status.get("backend_status", {})
            return (
                "Claude Code provider switched.\n"
                f"provider_label: {backend_status.get('provider_label') or 'default'}\n"
                f"provider_model: {backend_status.get('provider_model') or '(empty)'}\n"
                f"provider_base_url: {backend_status.get('provider_base_url') or '(empty)'}\n"
                "thread_reset: True\n"
                f"context_handoff: {backend_status.get('context_handoff_mode')}\n"
                f"context_queued: {'yes' if backend_status.get('pending_context_handoff') else 'no'}"
                + _provider_switch_warning("claude_code_cli_session", backend_status)
            )
        if cmd in {"/codex_proxy", "/claude_code_proxy"}:
            mode = "codex_cli_session" if cmd == "/codex_proxy" else "claude_code_cli_session"
            display = "Codex" if mode == "codex_cli_session" else "Claude Code"
            usage = "/codex proxy <on|off>" if mode == "codex_cli_session" else "/claude proxy <on|off>"
            raw = parts[1].strip().lower() if len(parts) > 1 else ""
            if not raw:
                current = _runtime_proxy_url(mode)
                return (
                    f"{display} proxy: {'on' if current else 'off'}\n"
                    f"proxy_url: {current or '(disabled)'}\n"
                    f"Usage: {usage}"
                )
            if raw not in {"on", "off"}:
                return f"Usage: {usage}"
            try:
                current = _set_runtime_proxy(mode, raw == "on")
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            return (
                f"{display} proxy {'enabled' if current else 'disabled'}.\n"
                f"proxy_url: {current or '(disabled)'}"
            )
        if cmd == "/status":
            session = session_service.get_chat(chat_id)
            shell_status = shell_service.get_status(session["session_id"]) if session else shell_service.get_status("__missing__")
            if not session:
                return (
                    "No active coder session for this chat yet.\n"
                    f"codex_proxy: {'on' if _runtime_proxy_enabled('codex_cli_session') else 'off'}\n"
                    f"claude_proxy: {'on' if _runtime_proxy_enabled('claude_code_cli_session') else 'off'}\n"
                    f"{_shell_status_text(shell_status)}"
                )
            detail = session_service.get_session_status(session["session_id"])
            provider_model_default, _ = _provider_default_values(detail["integration_mode"])
            latest_reply = conversation_history.read_latest_reply(chat_id=chat_id, session_id=session["session_id"])
            transcript_path = conversation_history.transcript_path(chat_id=chat_id, session_id=session["session_id"])
            if chat_id in app.state.active_chats:
                reply_state = "responding"
            elif detail["status"] == "error":
                reply_state = "error"
            elif latest_reply == "(No transcript reply available.)":
                reply_state = "no_reply_yet"
            else:
                reply_state = "idle"
            latest_reply_preview = latest_reply if len(latest_reply) <= 500 else f"{latest_reply[:500]}..."
            return (
                f"session_id: {detail['session_id']}\n"
                f"session_label: {detail.get('label') or '(none)'}\n"
                f"session_status: {detail['status']}\n"
                f"reply_state: {reply_state}\n"
                f"mode: {detail['integration_mode']}\n"
                f"provider_label: {detail['backend_status'].get('provider_label') or 'default'}\n"
                f"provider_model: {detail['backend_status'].get('provider_model') or provider_model_default}\n"
                f"context_handoff: {detail['backend_status'].get('context_handoff_mode') or 'light'}\n"
                f"codex_proxy: {'on' if _runtime_proxy_enabled('codex_cli_session') else 'off'}\n"
                f"claude_proxy: {'on' if _runtime_proxy_enabled('claude_code_cli_session') else 'off'}\n"
                f"workspace: {detail['workspace_path']}\n"
                f"thread_id: {detail['backend_status'].get('thread_id')}\n"
                f"active_pid: {detail['backend_status'].get('active_pid')}\n"
                f"cancel_requested: {detail['backend_status'].get('cancel_requested')}\n"
                f"last_return_code: {detail['backend_status'].get('last_return_code')}\n"
                f"{_shell_status_text(shell_status)}\n"
                f"transcript_exists: {transcript_path.exists()}\n"
                f"transcript_path: {transcript_path}\n"
                f"latest_reply:\n{latest_reply_preview}"
            )
        if cmd == "/resend":
            raw = parts[1].strip() if len(parts) > 1 else ""
            index = 1
            if raw:
                try:
                    index = int(raw)
                except ValueError:
                    return "Usage: /resend [n]"
            if index < 1:
                return "Usage: /resend [n]"
            session = session_service.get_chat(chat_id)
            if not session:
                return "No active coder session for this chat yet."
            replies = conversation_history.list_replies(chat_id=chat_id, session_id=session["session_id"])
            if not replies:
                return "No transcript replies available for the current session."
            if index > len(replies):
                return f"Only {len(replies)} reply/replies available for the current session."
            return replies[-index]
        if cmd == "/cancel":
            result = session_service.cancel_chat_reply(chat_id)
            if result.get("ok"):
                return (
                    "Cancellation requested.\n"
                    f"session_id: {result['session_id']}\n"
                    f"mode: {result['mode']}\n"
                    f"workspace: {result['workspace']}\n"
                    f"pid: {result.get('pid')}"
                )
            reason = result.get("reason", "unknown")
            if reason == "not_running":
                return (
                    "No active backend reply is running.\n"
                    f"session_id: {result['session_id']}\n"
                    f"mode: {result['mode']}\n"
                    f"workspace: {result['workspace']}"
                )
            return (
                "Unable to cancel backend reply.\n"
                f"session_id: {result['session_id']}\n"
                f"reason: {reason}"
            )
        if cmd == "/git_add":
            raw = parts[1].strip() if len(parts) > 1 else ""
            return await _run_local(shell_service.git_add, command_workspace, raw)
        if cmd == "/git_commit":
            raw = parts[1].strip() if len(parts) > 1 else ""
            return await _run_local(shell_service.git_commit, command_workspace, raw)
        if cmd == "/git_show":
            raw = parts[1].strip() if len(parts) > 1 else ""
            return await _run_local(shell_service.git_show, command_workspace, raw or "HEAD", 220)
        if cmd == "/git_push":
            raw = parts[1].strip() if len(parts) > 1 else ""
            tokens = _split_args(raw)
            remote = tokens[0] if len(tokens) >= 1 else None
            branch = tokens[1] if len(tokens) >= 2 else None
            return await _run_local(shell_service.git_push, command_workspace, remote, branch)
        if cmd == "/git_status":
            return await _run_local(shell_service.git_status, command_workspace)
        if cmd == "/git_diff":
            raw = parts[1].strip() if len(parts) > 1 else ""
            return await _run_local(shell_service.git_diff, command_workspace, raw or None, 220)
        if cmd == "/git_log":
            raw = parts[1].strip() if len(parts) > 1 else ""
            limit = 10
            if raw:
                try:
                    limit = max(1, min(int(raw), 30))
                except ValueError:
                    limit = 10
            return await _run_local(shell_service.git_log, command_workspace, limit)
        if cmd == "/git_branch":
            return await _run_local(shell_service.git_branch, command_workspace)
        if cmd == "/ls":
            raw = parts[1].strip() if len(parts) > 1 else ""
            return await _run_local(shell_service.list_directory, command_workspace, raw or None, 200)
        if cmd == "/tree":
            raw = parts[1].strip() if len(parts) > 1 else ""
            tokens = _split_args(raw)
            path_arg: str | None = None
            depth = 2
            if tokens:
                path_arg = tokens[0]
            if len(tokens) >= 2:
                try:
                    depth = max(1, min(int(tokens[1]), 6))
                except ValueError:
                    depth = 2
            return await _run_local(shell_service.render_tree, command_workspace, path_arg, depth, 200)
        if cmd == "/read":
            raw = parts[1].strip() if len(parts) > 1 else ""
            tokens = _split_args(raw)
            if not tokens:
                return "Usage: /read <path> [start_line] [lines]"
            path_arg = tokens[0]
            start_line = 1
            max_lines = 120
            if len(tokens) >= 2:
                try:
                    start_line = max(1, int(tokens[1]))
                except ValueError:
                    start_line = 1
            if len(tokens) >= 3:
                try:
                    max_lines = max(1, min(int(tokens[2]), 300))
                except ValueError:
                    max_lines = 120
            return await _run_local(
                shell_service.read_text_file,
                command_workspace,
                path_arg,
                start_line,
                max_lines,
            )
        if cmd == "/tail":
            raw = parts[1].strip() if len(parts) > 1 else ""
            tokens = _split_args(raw)
            if not tokens:
                return "Usage: /tail <path> [lines]"
            path_arg = tokens[0]
            lines_value = 50
            if len(tokens) >= 2:
                try:
                    lines_value = max(1, min(int(tokens[1]), 300))
                except ValueError:
                    lines_value = 50
            return await _run_local(shell_service.tail_text_file, command_workspace, path_arg, lines_value)
        if cmd == "/find":
            raw = parts[1].strip() if len(parts) > 1 else ""
            tokens = _split_args(raw)
            if not tokens:
                return "Usage: /find <pattern> [path]"
            pattern = tokens[0]
            path_arg = tokens[1] if len(tokens) >= 2 else None
            return await _run_local(shell_service.find_files, command_workspace, pattern, path_arg, 100)
        if cmd == "/grep":
            raw = parts[1].strip() if len(parts) > 1 else ""
            tokens = _split_args(raw)
            if not tokens:
                return "Usage: /grep <pattern> [path]"
            pattern = tokens[0]
            path_arg = tokens[1] if len(tokens) >= 2 else None
            return await _run_local(shell_service.grep_text, command_workspace, pattern, path_arg, 80)
        if cmd == "/show":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /show <path>"
            return await _send_workspace_file(raw, "show", True)
        if cmd == "/download":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /download <path>"
            return await _send_workspace_file(raw, "download", False)
        if cmd == "/cmd_top":
            return await asyncio.to_thread(shell_service.format_system_status)
        if cmd == "/gpu":
            return await asyncio.to_thread(shell_service.format_gpu_status)
        if cmd == "/cmd":
            raw_command = parts[1].strip() if len(parts) > 1 else ""
            if not raw_command:
                shell_status = shell_service.get_status(_shell_session()["session_id"])
                return f"Usage: /cmd <command>\n{_shell_status_text(shell_status)}"
            shell_session = _shell_session()
            output = await asyncio.to_thread(
                shell_service.execute,
                shell_session["session_id"],
                chat_id,
                raw_command,
                command_workspace,
            )
            return output
        if cmd == "/cmd_bg":
            raw_command = parts[1].strip() if len(parts) > 1 else ""
            if not raw_command:
                return "Usage: /cmd_bg <command> [:: label]"
            command_text, label = _split_with_label(raw_command)
            if not command_text:
                return "Usage: /cmd_bg <command> [:: label]"
            shell_session = _shell_session()
            job = await asyncio.to_thread(
                shell_service.start_background,
                shell_session["session_id"],
                chat_id,
                command_text,
                command_workspace,
                label,
            )
            return (
                f"Started background job #{job['job_id']}.\n"
                f"label: {job.get('label') or '(none)'}\n"
                f"pid: {job['pid']}\n"
                f"cwd: {job['cwd']}\n"
                f"log_path: {job['log_path']}\n"
                f"Use /log {job['job_id']} or /cmd_status"
            )
        if cmd in {"/cmd_bg_all", "/cmd_job_all"}:
            jobs = await asyncio.to_thread(shell_service.list_all_jobs)
            if not jobs:
                return "No shell background jobs across all sessions."
            sessions = {
                item["session_id"]: item
                for item in await asyncio.to_thread(session_service.list_sessions)
            }
            grouped: dict[str, dict[str, object]] = {}
            for global_index, job in enumerate(jobs, start=1):
                bucket = grouped.setdefault(
                    job["session_id"],
                    {
                        "chat_id": job["chat_id"],
                        "session_id": job["session_id"],
                        "label": (sessions.get(job["session_id"]) or {}).get("label", ""),
                        "items": [],
                    },
                )
                bucket["items"].append((global_index, job["job_id"]))

            ordered_groups = sorted(
                grouped.values(),
                key=lambda item: item["items"][0][0],
            )
            lines = [f"jobs: {len(jobs)}", f"sessions: {len(ordered_groups)}"]
            for group in ordered_groups:
                label = group["label"] or "(none)"
                lines.append("")
                lines.append(
                    f"chat={group['chat_id']} session={group['session_id']} label={label}"
                )
                for global_index, job_id in group["items"]:
                    lines.append(f"{global_index}. #{job_id}")
            return "\n".join(lines)
        if cmd == "/cmd_bg_delete":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /cmd_bg_delete <job_id>"
            try:
                job_id = int(raw.split()[0])
            except ValueError:
                return "Usage: /cmd_bg_delete <job_id>"
            result = await asyncio.to_thread(shell_service.delete_job, _shell_session()["session_id"], job_id)
            if not result["ok"]:
                return result["error"]
            job = result["job"]
            return (
                f"Deleted background job #{job['job_id']}.\n"
                f"label: {job.get('label') or '(none)'}\n"
                f"pid: {job['pid']}\n"
                f"cwd: {job['cwd']}"
            )
        if cmd == "/cmd_bg_clear":
            result = await asyncio.to_thread(shell_service.clear_jobs, _shell_session()["session_id"])
            deleted = result["deleted"]
            if not deleted:
                return "No shell background jobs for this chat."
            lines = [f"deleted_jobs: {len(deleted)}"]
            for job in deleted[:20]:
                lines.append(f"deleted #{job['job_id']} pid={job['pid']} cwd={job['cwd']}")
            if len(deleted) > 20:
                lines.append(f"... and {len(deleted) - 20} more")
            return "\n".join(lines)
        if cmd == "/conda":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                shell_status = shell_service.get_status(_shell_session()["session_id"])
                return (
                    f"conda_env: {shell_status.get('conda_env', '(default)')}\n"
                    f"shell_cwd: {shell_status.get('cwd') or command_workspace}\n"
                    "Use /conda <env>, /conda_envs, or /conda_off"
                )
            shell_session = _shell_session()
            selected = await asyncio.to_thread(shell_service.set_conda_env, shell_session["session_id"], chat_id, raw)
            return (
                "Conda environment updated.\n"
                f"conda_env: {selected['conda_env']}\n"
                f"path: {selected['path']}"
            )
        if cmd == "/conda_envs":
            envs = await asyncio.to_thread(shell_service.list_conda_envs)
            status = shell_service.get_status(_shell_session()["session_id"])
            lines = [f"selected_conda_env: {status.get('conda_env', '(default)')}"]
            for env in envs:
                marker = "*" if env["name"] == status.get("conda_env") else ("(base)" if env["active"] == "true" else "")
                suffix = f" {marker}".rstrip()
                lines.append(f"{env['name']}{suffix}: {env['path']}")
            return "\n".join(lines)
        if cmd == "/conda_off":
            shell_session = _shell_session()
            cleared = await asyncio.to_thread(shell_service.clear_conda_env, shell_session["session_id"], chat_id)
            return (
                "Conda environment cleared.\n"
                f"previous: {cleared['previous']}\n"
                f"conda_env: {cleared['conda_env']}"
            )
        if cmd == "/cmd_status":
            lines_value = 20
            if arg:
                try:
                    lines_value = max(1, min(int(arg), 200))
                except ValueError:
                    lines_value = 20
            return await asyncio.to_thread(shell_service.format_status, _shell_session()["session_id"], lines_value)
        if cmd == "/cmd_jobs":
            jobs = await asyncio.to_thread(shell_service.list_jobs, _shell_session()["session_id"])
            if not jobs:
                return "No shell background jobs for this chat."
            lines = [f"jobs: {len(jobs)}"]
            for job in jobs[-20:]:
                state_label = "running" if job["running"] else f"exit={job['return_code']}"
                label_part = f" label={job['label']}" if job.get('label') else ""
                lines.append(f"#{job['job_id']} pid={job['pid']} {state_label}{label_part} cwd={job['cwd']}")
                lines.append(f"cmd: {job['command']}")
            return "\n".join(lines)
        if cmd == "/log":
            raw = parts[1].strip() if len(parts) > 1 else ""
            job_id, lines_value = _parse_tail_and_job(raw)
            tail = await asyncio.to_thread(shell_service.tail_logs, _shell_session()["session_id"], job_id, lines_value)
            if not tail["ok"]:
                return tail["error"]
            job = tail["job"]
            state_label = "running" if job["running"] else f"exit={job['return_code']}"
            body = tail["output"] or "(log is currently empty)"
            return (
                f"job_id: {job['job_id']}\n"
                f"label: {job.get('label') or '(none)'}\n"
                f"pid: {job['pid']}\n"
                f"status: {state_label}\n"
                f"cwd: {job['cwd']}\n"
                f"log_path: {job['log_path']}\n"
                f"showing_last: {tail['shown_lines']} of {tail['line_count']} lines\n\n"
                f"{body}"
            )
        if cmd == "/watch":
            raw = parts[1].strip() if len(parts) > 1 else ""
            job_id, lines_value, keywords = _parse_watch_args(raw)
            watch = await asyncio.to_thread(
                shell_service.watch_logs,
                _shell_session()["session_id"],
                job_id,
                lines_value,
                keywords or None,
            )
            if not watch["ok"]:
                return watch["error"]
            job = watch["job"]
            state_label = "running" if job["running"] else f"exit={job['return_code']}"
            body = watch["output"] or "(no matching progress lines found)"
            return (
                f"job_id: {job['job_id']}\n"
                f"label: {job.get('label') or '(none)'}\n"
                f"pid: {job['pid']}\n"
                f"status: {state_label}\n"
                f"cwd: {job['cwd']}\n"
                f"log_path: {job['log_path']}\n"
                f"matched_lines: {watch['matched_count']}\n"
                f"keywords: {','.join(watch['keywords'])}\n"
                f"showing_last: {watch['shown_lines']} matches\n\n"
                f"{body}"
            )
        if cmd == "/cmd_stop":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /cmd_stop <job_id>"
            try:
                job_id = int(raw.split()[0])
            except ValueError:
                return "Usage: /cmd_stop <job_id>"
            result = await asyncio.to_thread(shell_service.stop_job, _shell_session()["session_id"], job_id)
            if not result["ok"]:
                return result["error"]
            job = result["job"]
            if result.get("already_stopped"):
                status_line = f"Job #{job['job_id']} was already stopped."
            else:
                status_line = f"Stop signal sent to job #{job['job_id']}."
            state_label = "running" if job["running"] else f"exit={job['return_code']}"
            return (
                f"{status_line}\n"
                f"label: {job.get('label') or '(none)'}\n"
                f"pid: {job['pid']}\n"
                f"status: {state_label}\n"
                f"cwd: {job['cwd']}\n"
                f"log_path: {job['log_path']}"
            )
        if cmd == "/cmd_stop_all":
            result = await asyncio.to_thread(shell_service.stop_all_jobs, _shell_session()["session_id"])
            stopped = result["stopped"]
            already = result["already_stopped"]
            if not stopped and not already:
                return "No shell background jobs for this chat."
            lines = [
                f"stopped_jobs: {len(stopped)}",
                f"already_stopped: {len(already)}",
            ]
            for job in stopped:
                lines.append(f"stopped #{job['job_id']} pid={job['pid']} exit={job['return_code']}")
            for job in already[-10:]:
                lines.append(f"already_stopped #{job['job_id']} pid={job['pid']} exit={job['return_code']}")
            return "\n".join(lines)
        if cmd == "/cmd_reset":
            shell_session = _shell_session()
            status = await asyncio.to_thread(shell_service.reset, shell_session["session_id"], chat_id, command_workspace)
            return (
                "Shell reset complete.\n"
                f"{_shell_status_text(status)}"
            )
        if cmd == "/pwd":
            session = session_service.get_or_create_chat_session(chat_id)
            return f"Workspace: {session['workspace_path']}"
        if cmd == "/mode":
            session = session_service.get_or_create_chat_session(chat_id)
            return (
                f"mode: {session['integration_mode']}\n"
                f"backend: {_mode_display_name(session['integration_mode'])}"
            )
        if cmd == "/timeout":
            raw = parts[1].strip() if len(parts) > 1 else ""
            session = session_service.get_or_create_chat_session(chat_id)
            status = session_service.get_session_status(session["session_id"])
            current = status.get("backend_status", {}).get("timeout_seconds", settings.codex_message_timeout_seconds)
            if not raw:
                return (
                    f"timeout_seconds: {current}\n"
                    f"default_timeout_seconds: {settings.codex_message_timeout_seconds}"
                )
            try:
                value = int(raw)
            except ValueError:
                return "Usage: /timeout <seconds|-1>"
            if value == -1:
                pass
            elif value < 1:
                return "Usage: /timeout <seconds|-1>"
            updated = session_service.set_chat_timeout(chat_id, value)
            effective = updated.get("backend_status", {}).get("timeout_seconds", value)
            return (
                "Reply timeout updated.\n"
                f"timeout_seconds: {effective}\n"
                f"default_timeout_seconds: {settings.codex_message_timeout_seconds}"
            )
        if cmd == "/context_handoff":
            raw = parts[1].strip().lower() if len(parts) > 1 else ""
            session = session_service.get_or_create_chat_session(chat_id)
            status = session_service.get_session_status(session["session_id"])
            current = status.get("backend_status", {}).get("context_handoff_mode", "light")
            if not raw:
                return (
                    f"context_handoff: {current}\n"
                    "available: off, light"
                )
            if raw not in {"off", "light"}:
                return "Usage: /context_handoff <off|light>"
            updated = session_service.set_chat_context_handoff(chat_id, raw)
            return (
                "Context handoff updated.\n"
                f"context_handoff: {updated.get('backend_status', {}).get('context_handoff_mode', raw)}"
            )
        if cmd in {"/trace", "/trace_raw", "/trace_error"}:
            raw = parts[1].strip() if len(parts) > 1 else ""
            lines_value = 20
            if raw:
                try:
                    lines_value = max(5, min(int(raw), 200))
                except ValueError:
                    return f"Usage: {cmd} [lines]"
            session = session_service.get_or_create_chat_session(chat_id)
            status = session_service.get_session_status(session["session_id"])
            backend_status = status.get("backend_status", {})
            stderr_lines = backend_status.get("stderr_lines", [])[-min(lines_value, 40):]
            if cmd == "/trace_error":
                trace_lines = backend_status.get("recent_raw_events", [])[-lines_value:]
                if backend_status.get("last_return_code") in {None, 0} and not stderr_lines and not trace_lines:
                    return "No recent backend error recorded for the current session."
            else:
                trace_key = "recent_raw_events" if cmd == "/trace_raw" else "recent_events"
                trace_lines = backend_status.get(trace_key, [])[-lines_value:]
            header = [
                f"running: {backend_status.get('running')}",
                f"thread_id: {backend_status.get('thread_id')}",
                f"event_count: {backend_status.get('event_count')}",
                f"timeout_seconds: {backend_status.get('timeout_seconds')}",
                f"last_return_code: {backend_status.get('last_return_code')}",
                f"current_started_at: {backend_status.get('current_started_at')}",
                f"current_finished_at: {backend_status.get('current_finished_at')}",
                f"current_prompt: {backend_status.get('current_prompt') or '(none)'}",
                f"latest_reply_preview: {backend_status.get('latest_reply_preview') or '(none)'}",
                f"context_handoff_mode: {backend_status.get('context_handoff_mode')}",
                f"pending_context_handoff: {'yes' if backend_status.get('pending_context_handoff') else 'no'}",
            ]
            body = trace_lines or ["(no trace events captured yet)"]
            if stderr_lines:
                body.extend(["", "[stderr]"])
                body.extend(stderr_lines)
            return "\n".join(header + [""] + body)
        if cmd == "/debug":
            diagnostics = await telegram.run_diagnostics()
            webhook_result = diagnostics.get("get_webhook_info", {}).get("result", {}) or {}
            me_result = diagnostics.get("get_me", {}).get("result", {}) or {}
            recent_events = db.recent_audit_logs(limit=5)
            pending = webhook_result.get("pending_update_count", 0)
            summary = (
                "Debug diagnostics:\n"
                f"debug_mode: {settings.telegram_debug_mode}\n"
                f"codex_debug_mode: {settings.codex_debug_mode}\n"
                f"telegram_mode: {settings.telegram_mode}\n"
                f"bot_username: @{me_result.get('username', '<unknown>')}\n"
                f"bot_id: {me_result.get('id')}\n"
                f"token_valid: {diagnostics.get('get_me', {}).get('ok')}\n"
                f"dns_ok: {diagnostics.get('dns', {}).get('ok')}\n"
                f"webhook_api_ok: {diagnostics.get('get_webhook_info', {}).get('ok')}\n"
                f"webhook_url: {webhook_result.get('url') or '<empty>'}\n"
                f"pending_updates: {pending}\n"
                f"last_error_date: {webhook_result.get('last_error_date')}\n"
                f"last_error_message: {webhook_result.get('last_error_message')}\n"
                f"last_sync_error_date: {webhook_result.get('last_synchronization_error_date')}\n"
                f"poll_offset: {app.state.telegram_offset}\n"
                f"poll_task_alive: {bool(app.state.poll_task and not app.state.poll_task.done())}\n"
                f"recent_audit_events: {[item['event_type'] for item in recent_events]}"
            )
            if arg in {"verbose", "full", "detail", "详细"}:
                detail = json.dumps(diagnostics, ensure_ascii=False, indent=2)
                return f"{summary}\n\nDetailed diagnostics JSON:\n{detail}"
            return summary
        return "Unknown command. Use /help"

    async def polling_loop() -> None:
        logger.info("telegram polling loop started (interval=%.1fs)", settings.telegram_poll_interval_seconds)
        poll_count = 0
        while True:
            poll_count += 1
            try:
                updates = await telegram.get_updates(offset=app.state.telegram_offset)
                if updates:
                    app.state.telegram_offset = updates[-1]["update_id"] + 1
                    logger.debug(
                        "[POLL #%d] processing %d update(s), next offset=%s",
                        poll_count, len(updates), app.state.telegram_offset,
                    )
                    await process_updates(updates, source="polling")
                elif settings.telegram_debug_mode and poll_count <= 3:
                    # Log first few empty polls so user sees polling is alive
                    logger.debug("[POLL #%d] no new updates (offset=%s)", poll_count, app.state.telegram_offset)
            except Exception:  # noqa: BLE001
                logger.exception("[POLL #%d] iteration failed", poll_count)
                await asyncio.sleep(settings.telegram_poll_interval_seconds)

    @app.on_event("startup")
    async def startup() -> None:
        # --- Debug banner ---
        if settings.telegram_debug_mode:
            logger.info("=" * 60)
            logger.info("REMOTECODER DEBUG MODE ENABLED")
            logger.info("=" * 60)
            logger.info("Config: telegram_mode=%s", settings.telegram_mode)
            logger.info("Config: poll_interval=%.1fs", settings.telegram_poll_interval_seconds)
            logger.info("Config: chunk_size=%d", settings.telegram_long_message_chunk)
            logger.info("Config: auto_clear_webhook=%s", settings.telegram_auto_clear_webhook)
            logger.info("Config: codex_mode=%s", settings.default_codex_mode)
            logger.info("Config: workspace=%s", settings.default_workspace)
            logger.info("-" * 60)

        # --- Startup connection test ---
        logger.info("Running Telegram startup connection test ...")
        test_result = await telegram.startup_connection_test(
            auto_clear_webhook=(
                settings.telegram_auto_clear_webhook and settings.telegram_mode == "polling"
            ),
        )
        if test_result.get("get_me"):
            logger.info("Telegram connection OK - bot ready")
        else:
            logger.error("Telegram connection FAILED - check token/network. Details above.")

        if settings.telegram_debug_mode:
            logger.info("-" * 60)

        restored_sessions = await asyncio.to_thread(session_service.rehydrate_persisted_sessions)
        logger.info("rehydrated %d persisted chat session(s)", len(restored_sessions))
        restored_shells = await asyncio.to_thread(shell_service.restore_persisted_state)
        logger.info("rehydrated %d persisted shell session(s)", len(restored_shells))
        proxy_started = await asyncio.to_thread(claude_proxy_service.start)
        if proxy_started:
            logger.info("claude code proxy enabled at %s", claude_proxy_service.public_base_url)

        # --- Start polling or webhook ---
        if settings.telegram_mode == "polling":
            app.state.poll_task = asyncio.create_task(polling_loop())
            app.state.shell_notify_task = asyncio.create_task(shell_job_notify_loop())
            logger.info("telegram polling enabled debug_mode=%s", settings.telegram_debug_mode)
        else:
            logger.info("telegram webhook mode enabled debug_mode=%s", settings.telegram_debug_mode)

    @app.on_event("shutdown")
    async def shutdown() -> None:
        task = app.state.poll_task
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        notify_task = getattr(app.state, "shell_notify_task", None)
        if notify_task:
            notify_task.cancel()
            with suppress(asyncio.CancelledError):
                await notify_task
        restart_task = getattr(app.state, "service_restart_task", None)
        if restart_task:
            restart_task.cancel()
            with suppress(asyncio.CancelledError):
                await restart_task
        await asyncio.to_thread(claude_proxy_service.stop)
        shell_service.close_all()

    app.include_router(router)
    return app


app = build_app()
