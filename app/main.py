"""Entrypoint for Telegram-to-Codex bridge MVP."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI

from app.adapters.telegram import TelegramAdapter
from app.api.routes import router
from app.codex.cli_session import CodexCliSessionBackend
from app.codex.sdk_mode import CodexSdkBackend
from app.config import settings
from app.db import Database
from app.logging import configure_logging
from app.schemas import TelegramInboundMessage
from app.services.audit_service import AuditService
from app.services.conversation_history import ConversationHistoryService
from app.services.session_service import SessionService
from app.services.shell_service import ShellService
from app.services.workspace_guard import WorkspaceGuard

configure_logging(
    settings.log_dir,
    debug=settings.telegram_debug_mode or settings.codex_debug_mode,
)
logger = logging.getLogger(__name__)


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
        ),
        "codex_sdk": CodexSdkBackend(),
    }

    session_service = SessionService(
        db=db,
        audit_service=audit,
        workspace_guard=workspace_guard,
        backends=backends,
        default_mode=settings.default_codex_mode,
    )
    conversation_history = ConversationHistoryService(settings.conversation_history_dir)
    shell_service = ShellService(settings.default_workspace, timeout_seconds=settings.codex_message_timeout_seconds)
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
    app.state.telegram_offset = None
    app.state.poll_task = None
    app.state.shell_notify_task = None
    app.state.active_chats = set()
    app.state.chat_locks = {}
    app.state.update_tasks = set()

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
        command = text.split(maxsplit=1)[0].lower()
        return command in {
            "/status",
            "/help",
            "/pwd",
            "/mode",
            "/debug",
            "/workspace",
            "/workspaces",
            "/session_label",
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
            "/cmd_jobs",
            "/cmd_stop",
            "/cmd_stop_all",
            "/cmd_logs",
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
            reply = await handle_chat_text(normalized.chat_id, normalized.text)
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
            if _is_local_bypass_command(normalized.text):
                markdown = _render_local_markdown(normalized.text, reply)
                if markdown is not None:
                    await telegram.send_markdown(normalized.chat_id, markdown)
                else:
                    await telegram.send_markdown_card(normalized.chat_id, normalized.text[:80], reply)
            else:
                await telegram.send_text(normalized.chat_id, reply)
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

        if _is_local_bypass_command(normalized.text):
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
                ("Git", []),
                ("Files", []),
                ("Media", []),
                ("Shell", []),
                ("System", []),
            ]
            def _bucket(name: str) -> list[tuple[str, str]]:
                if name in {"/new", "/reset", "/status", "/workspace", "/workspaces", "/session_label", "/pwd", "/mode"}:
                    return categories[0][1]
                if name.startswith("/git_"):
                    return categories[1][1]
                if name in {"/ls", "/tree", "/read", "/tail", "/find", "/grep"}:
                    return categories[2][1]
                if name in {"/show", "/download"}:
                    return categories[3][1]
                if name.startswith("/cmd"):
                    return categories[4][1]
                return categories[5][1]
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
            if headline_bits:
                blocks.append(" ".join(headline_bits))
            meta_lines: list[str] = []
            if "session_label" in kv and kv["session_label"] not in {"", "(none)"}:
                meta_lines.append(_inline_kv("label", kv["session_label"]))
            if "thread_id" in kv and kv["thread_id"] not in {"None", ""}:
                meta_lines.append(_inline_kv("thread", kv["thread_id"]))
            if "session_id" in kv:
                meta_lines.append(_inline_kv("session", kv["session_id"]))
            if meta_lines:
                blocks.append("")
                blocks.extend(meta_lines)
            for key in ["session_label", "reply_state", "session_status", "mode"]:
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
            if "current_workspace" in kv:
                blocks.append(_status_chip("project", _project_name(kv["current_workspace"])))
            for key in ["current_workspace", "session_label", "default_workspace"]:
                if key in kv:
                    blocks.append(_inline_kv(key, kv[key]))
            allowed = []
            if "allowed_roots" in extra:
                lines = [line.strip() for line in extra.splitlines() if line.strip() and line.strip() != "allowed_roots:"]
                allowed.extend(lines)
            elif extra:
                allowed.extend([line.strip() for line in extra.splitlines() if line.strip()])
            if allowed:
                blocks.append("")
                blocks.append(_section("Roots"))
                for item in allowed:
                    blocks.append(_bullet_code(item))
            return "\n".join(blocks)

        if cmd in {"/cmd_status", "/cmd_jobs"}:
            title = _title("Shell Jobs") if cmd == "/cmd_jobs" else _title("Shell Status")
            blocks = [title]
            chips = []
            for key, short in [
                ("active_jobs", "jobs"),
                ("latest_job_id", "latest"),
                ("shell_busy", "busy"),
                ("shell_last_exit_code", "last_exit"),
            ]:
                if key in kv:
                    chips.append(_status_chip(short, kv[key]))
            if chips:
                blocks.append(" ".join(chips))
            if "shell_cwd" in kv:
                blocks.append("")
                blocks.append(_section("Current Directory"))
                blocks.append(_bullet_code(kv["shell_cwd"]))
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
            return await handle_command(chat_id, text)
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
                codex_raw_stream=output,
                telegram_reply=reply,
            )
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed handling chat message")
            return f"Error talking to Codex backend: {exc}"
        finally:
            app.state.active_chats.discard(chat_id)

    async def handle_command(chat_id: int, text: str) -> str:
        parts = text.split(maxsplit=1)
        cmd = parts[0]
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        session = session_service.get_chat(chat_id)
        command_workspace = settings.default_workspace if session is None else session["workspace_path"]

        def _shell_status_text(status: dict) -> str:
            return (
                f"shell_exists: {status['exists']}\n"
                f"shell_busy: {status['busy']}\n"
                f"shell_cwd: {status.get('cwd') or status['workspace']}\n"
                f"shell_last_exit_code: {status['last_exit_code']}\n"
                f"active_jobs: {len(status.get('active_job_ids', []))}\n"
                f"latest_job_id: {status.get('latest_job_id')}"
            )

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

        if cmd == "/help":
            return (
                "Available commands:\n"
                "/new - create a new session\n"
                "/reset - reset current session\n"
                "/status [verbose] - show current session status\n"
                "/workspace [path] [:: label] - show or switch the current Codex workspace\n"
                "/workspaces - list allowed workspace roots\n"
                "/session_label <label> - update the current Codex session label\n"
                "/git_add <path> - stage a file or directory\n"
                "/git_commit <message> - create a commit from staged changes\n"
                "/git_show [ref] - show a commit with stats\n"
                "/git_push [remote] [branch] - push current branch to remote\n"
                "/git_status - show git branch and working tree status\n"
                "/git_diff [path] - show git diff, optionally for one path\n"
                "/git_log [n] - show recent commits\n"
                "/git_branch - show local branches\n"
                "/ls [path] - list files in the current workspace\n"
                "/tree [path] [depth] - show a truncated directory tree\n"
                "/read <path> [start_line] [lines] - read part of a text file\n"
                "/tail <path> [lines] - tail a text file\n"
                "/find <pattern> [path] - find files by name under the workspace\n"
                "/grep <pattern> [path] - search text in workspace files\n"
                "/show <path> - send an image or file back to Telegram\n"
                "/download <path> - send a file back to Telegram as a document\n"
                "/cmd_top - show CPU, memory, disk, and hot processes\n"
                "/gpu - show GPU status from nvidia-smi\n"
                "/cmd <command> - run a direct shell command in a persistent per-chat shell\n"
                "/cmd_bg <command> - start a long-running shell job in the background\n"
                "/cmd_jobs - list shell background jobs for this chat\n"
                "/cmd_status [lines] - show shell status and latest job tail\n"
                "/cmd_logs [job_id] [lines] - tail a shell job log\n"
                "/cmd_stop <job_id> - stop a background shell job\n"
                "/cmd_stop_all - stop all background shell jobs for this chat\n"
                "/cmd_reset - reset the per-chat shell session\n"
                "/pwd - show current workspace\n"
                "/mode - show integration mode\n"
                "/debug - show Telegram diagnostics summary\n"
                "/debug verbose - show detailed Telegram diagnostics\n"
                "/help - this help"
            )
        if cmd == "/new":
            raw = parts[1].strip() if len(parts) > 1 else ""
            _, label = _split_with_label(raw)
            session = session_service.new_session(chat_id=chat_id, label=label)
            return (
                f"Created new session {session['session_id']} in {session['workspace_path']}\n"
                f"label: {session.get('label') or '(none)'}"
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
                await asyncio.to_thread(shell_service.reset, chat_id, session["workspace_path"])
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
        if cmd == "/session_label":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /session_label <label>"
            session = session_service.set_session_label(chat_id, raw)
            return (
                f"Updated session label.\n"
                f"session_id: {session['session_id']}\n"
                f"label: {session.get('label') or '(none)'}"
            )
        if cmd == "/status":
            session = session_service.get_chat(chat_id)
            shell_status = shell_service.get_status(chat_id)
            if not session:
                return (
                    "No active Codex session for this chat yet.\n"
                    f"{_shell_status_text(shell_status)}"
                )
            detail = session_service.get_session_status(session["session_id"])
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
                f"workspace: {detail['workspace_path']}\n"
                f"thread_id: {detail['backend_status'].get('thread_id')}\n"
                f"last_return_code: {detail['backend_status'].get('last_return_code')}\n"
                f"{_shell_status_text(shell_status)}\n"
                f"transcript_exists: {transcript_path.exists()}\n"
                f"transcript_path: {transcript_path}\n"
                f"latest_reply:\n{latest_reply_preview}"
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
                shell_status = shell_service.get_status(chat_id)
                return f"Usage: /cmd <command>\n{_shell_status_text(shell_status)}"
            output = await asyncio.to_thread(shell_service.execute, chat_id, raw_command, command_workspace)
            return output
        if cmd == "/cmd_bg":
            raw_command = parts[1].strip() if len(parts) > 1 else ""
            if not raw_command:
                return "Usage: /cmd_bg <command> [:: label]"
            command_text, label = _split_with_label(raw_command)
            if not command_text:
                return "Usage: /cmd_bg <command> [:: label]"
            job = await asyncio.to_thread(shell_service.start_background, chat_id, command_text, command_workspace, label)
            return (
                f"Started background job #{job['job_id']}.\n"
                f"label: {job.get('label') or '(none)'}\n"
                f"pid: {job['pid']}\n"
                f"cwd: {job['cwd']}\n"
                f"log_path: {job['log_path']}\n"
                f"Use /cmd_logs {job['job_id']} or /cmd_status"
            )
        if cmd == "/cmd_status":
            lines_value = 20
            if arg:
                try:
                    lines_value = max(1, min(int(arg), 200))
                except ValueError:
                    lines_value = 20
            return await asyncio.to_thread(shell_service.format_status, chat_id, lines_value)
        if cmd == "/cmd_jobs":
            jobs = await asyncio.to_thread(shell_service.list_jobs, chat_id)
            if not jobs:
                return "No shell background jobs for this chat."
            lines = [f"jobs: {len(jobs)}"]
            for job in jobs[-20:]:
                state_label = "running" if job["running"] else f"exit={job['return_code']}"
                label_part = f" label={job['label']}" if job.get('label') else ""
                lines.append(f"#{job['job_id']} pid={job['pid']} {state_label}{label_part} cwd={job['cwd']}")
                lines.append(f"cmd: {job['command']}")
            return "\n".join(lines)
        if cmd == "/cmd_logs":
            raw = parts[1].strip() if len(parts) > 1 else ""
            job_id, lines_value = _parse_tail_and_job(raw)
            tail = await asyncio.to_thread(shell_service.tail_logs, chat_id, job_id, lines_value)
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
        if cmd == "/cmd_stop":
            raw = parts[1].strip() if len(parts) > 1 else ""
            if not raw:
                return "Usage: /cmd_stop <job_id>"
            try:
                job_id = int(raw.split()[0])
            except ValueError:
                return "Usage: /cmd_stop <job_id>"
            result = await asyncio.to_thread(shell_service.stop_job, chat_id, job_id)
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
            result = await asyncio.to_thread(shell_service.stop_all_jobs, chat_id)
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
            status = await asyncio.to_thread(shell_service.reset, chat_id, command_workspace)
            return (
                "Shell reset complete.\n"
                f"{_shell_status_text(status)}"
            )
        if cmd == "/pwd":
            session = session_service.get_or_create_chat_session(chat_id)
            return f"Workspace: {session['workspace_path']}"
        if cmd == "/mode":
            session = session_service.get_or_create_chat_session(chat_id)
            return f"Mode: {session['integration_mode']}"
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
        shell_service.close_all()

    app.include_router(router)
    return app


app = build_app()
