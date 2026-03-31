"""Codex CLI backend built on non-interactive exec/resume commands.

This avoids scraping TUI/PTTY redraw output. Instead, each turn writes the
assistant's final message to a temp file via `--output-last-message`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from app.codex.base import CodexBackend, CodexReplyCancelled, CodexSessionInfo

logger = logging.getLogger(__name__)


@dataclass
class _SessionState:
    workspace: Path
    thread_id: str | None = None
    last_return_code: int | None = None
    timeout_seconds: int | None = None
    trace_lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    current_prompt: str = ""
    current_started_at: float | None = None
    current_finished_at: float | None = None
    event_count: int = 0
    recent_events: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    recent_raw_events: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=120))
    latest_reply_preview: str = ""
    active_process: subprocess.Popen[str] | None = None
    cancel_requested: bool = False
    active_pid: int | None = None


class CodexCliSessionBackend(CodexBackend):
    _ENV_PASSTHROUGH_KEYS = {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NO_PROXY",
        "no_proxy",
    }
    _PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

    def __init__(
        self,
        codex_bin: str,
        codex_args: str,
        timeout_seconds: int = 120,
        proxy_url: str | None = None,
        debug_mode: bool = False,
        web_search_enabled: bool = False,
    ) -> None:
        self.codex_bin = codex_bin
        self.web_search_enabled = web_search_enabled
        self.codex_args = self._normalize_cli_args_string(codex_args)
        self.timeout_seconds = timeout_seconds
        self.proxy_url = proxy_url
        self.debug_mode = debug_mode
        self.sessions: dict[str, _SessionState] = {}

    def _build_process_env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if key in self._ENV_PASSTHROUGH_KEYS}
        if self.proxy_url:
            for key in self._PROXY_ENV_KEYS:
                env[key] = self.proxy_url
        else:
            for key in self._PROXY_ENV_KEYS:
                env.pop(key, None)
        return env

    def _normalize_cli_args_string(self, raw_args: str) -> str:
        normalized = self._normalize_cli_args(shlex.split(raw_args))
        return shlex.join(normalized)

    def _normalize_cli_args(self, args: list[str]) -> list[str]:
        normalized: list[str] = []
        removed: list[str] = []
        skip_next = False
        has_search = False
        for index, arg in enumerate(args):
            if skip_next:
                skip_next = False
                removed.append(arg)
                continue
            if arg == "--stdio":
                removed.append(arg)
                continue
            if index == 0 and arg == "chat":
                removed.append(arg)
                continue
            if arg in {"--no-alt-screen"}:
                removed.append(arg)
                continue
            if arg in {"--json", "--output-last-message", "-o"}:
                removed.append(arg)
                if arg in {"--output-last-message", "-o"}:
                    skip_next = True
                continue
            if arg == "--search":
                has_search = True
            normalized.append(arg)

        if self.web_search_enabled and not has_search:
            normalized.append("--search")

        if removed:
            logger.warning(
                "normalizing codex cli args for exec backend",
                extra={"removed_args": removed, "normalized_args": normalized},
            )
        return normalized

    def _run_codex_command(self, workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self._run_codex_command_with_timeout(workspace, args, self.timeout_seconds)

    def _run_codex_command_with_timeout(
        self, workspace: Path, args: list[str], timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.codex_bin, *args]
        logger.debug("running codex command", extra={"cmd": cmd, "workspace": str(workspace)})
        return subprocess.run(
            cmd,
            cwd=workspace,
            env=self._build_process_env(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )

    def _build_exec_command(self, state: _SessionState, message: str, output_path: str) -> list[str]:
        extra = shlex.split(self.codex_args)
        if state.thread_id:
            return [*extra, "exec", "resume", "--skip-git-repo-check", "--json", "-o", output_path, state.thread_id, message]
        return [*extra, "exec", "--skip-git-repo-check", "--json", "-o", output_path, message]

    def _extract_thread_id(self, stdout: str) -> str | None:
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    return thread_id
        return None

    @staticmethod
    def _preview_text(value: object, limit: int = 160) -> str:
        text = str(value).strip()
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)].rstrip()}..."

    def _summarize_event(self, raw_line: str) -> str:
        line = raw_line.strip()
        if not line:
            return ""
        if not line.startswith("{"):
            return self._preview_text(line)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return self._preview_text(line)

        event_type = str(event.get("type", "event"))
        details: list[str] = []
        for key in ("status", "subtype", "name", "command", "cwd", "thread_id"):
            value = event.get(key)
            if value not in {None, ""}:
                details.append(f"{key}={self._preview_text(value, 80)}")
        for key in ("delta", "text", "message", "content"):
            value = event.get(key)
            if value not in {None, ""}:
                details.append(self._preview_text(value))
                break
        if not details:
            payload = {k: v for k, v in event.items() if k != "type"}
            if payload:
                details.append(self._preview_text(json.dumps(payload, ensure_ascii=False), 180))
        return f"{event_type}: {' | '.join(details)}" if details else event_type

    def _mark_trace_started(self, state: _SessionState, message: str) -> None:
        with state.trace_lock:
            state.running = True
            state.cancel_requested = False
            state.active_process = None
            state.active_pid = None
            state.current_prompt = self._preview_text(message, 500)
            state.current_started_at = time.time()
            state.current_finished_at = None
            state.event_count = 0
            state.recent_events.clear()
            state.recent_raw_events.clear()
            state.stderr_lines.clear()
            state.latest_reply_preview = ""

    def _record_stdout_line(self, state: _SessionState, raw_line: str) -> None:
        stripped = raw_line.rstrip("\n")
        if not stripped:
            return
        summary = self._summarize_event(stripped)
        with state.trace_lock:
            state.event_count += 1
            state.recent_raw_events.append(stripped)
            if summary:
                state.recent_events.append(summary)
        if state.thread_id is None:
            maybe_thread = self._extract_thread_id(stripped)
            if maybe_thread:
                state.thread_id = maybe_thread

    def _record_stderr_line(self, state: _SessionState, raw_line: str) -> None:
        stripped = raw_line.rstrip("\n")
        if not stripped:
            return
        with state.trace_lock:
            state.stderr_lines.append(stripped)

    def _mark_trace_finished(self, state: _SessionState, reply: str, return_code: int | None) -> None:
        with state.trace_lock:
            state.running = False
            state.active_process = None
            state.active_pid = None
            state.current_finished_at = time.time()
            state.last_return_code = return_code
            state.latest_reply_preview = self._preview_text(reply, 1200)

    @staticmethod
    def _drain_stream(stream, callback) -> None:
        try:
            for line in iter(stream.readline, ""):
                if line == "":
                    break
                callback(line)
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    def _format_exec_error(self, proc: subprocess.CompletedProcess[str], last_message: str) -> RuntimeError:
        detail = f"Codex CLI exec failed with code {proc.returncode}"
        stderr = proc.stderr.strip()
        stdout = proc.stdout.strip()
        if last_message:
            detail += f". Partial assistant reply:\n{last_message[-1200:]}"
        if stderr:
            detail += f"\nStderr:\n{stderr[-1200:]}"
        elif stdout:
            detail += f"\nStdout:\n{stdout[-1200:]}"
        return RuntimeError(detail)

    def create_session(self, session_id: str, workspace: Path) -> CodexSessionInfo:
        self.sessions[session_id] = _SessionState(workspace=workspace, timeout_seconds=self.timeout_seconds)
        logger.info(
            "created codex exec-backed session",
            extra={"session_id": session_id, "workspace": str(workspace), "mode": "codex_cli_session"},
        )
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode="codex_cli_session")

    def restore_session(self, session_id: str, workspace: Path, backend_state: dict | None = None) -> CodexSessionInfo:
        state = _SessionState(workspace=workspace, timeout_seconds=self.timeout_seconds)
        if backend_state:
            thread_id = backend_state.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                state.thread_id = thread_id.strip()

            timeout_seconds = backend_state.get("timeout_seconds")
            if isinstance(timeout_seconds, int) and timeout_seconds > 0:
                state.timeout_seconds = timeout_seconds

            last_return_code = backend_state.get("last_return_code")
            if isinstance(last_return_code, int):
                state.last_return_code = last_return_code

            latest_reply_preview = backend_state.get("latest_reply_preview")
            if isinstance(latest_reply_preview, str) and latest_reply_preview.strip():
                state.latest_reply_preview = self._preview_text(latest_reply_preview, 1200)

        self.sessions[session_id] = state
        logger.info(
            "restored codex exec-backed session",
            extra={
                "session_id": session_id,
                "workspace": str(workspace),
                "mode": "codex_cli_session",
                "thread_id": state.thread_id,
            },
        )
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode="codex_cli_session")

    def send_message(self, session_id: str, message: str) -> str:
        state = self.sessions.get(session_id)
        if state is None:
            raise RuntimeError(f"Session {session_id} not found in CLI backend")
        timeout_seconds = state.timeout_seconds or self.timeout_seconds
        self._mark_trace_started(state, message)

        output_file = tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt", delete=False)
        output_file.close()
        try:
            try:
                cmd = [self.codex_bin, *self._build_exec_command(state, message, output_file.name)]
                proc = subprocess.Popen(
                    cmd,
                    cwd=state.workspace,
                    env=self._build_process_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                with state.trace_lock:
                    state.active_process = proc
                    state.active_pid = proc.pid
                stdout_thread = threading.Thread(
                    target=self._drain_stream,
                    args=(proc.stdout, lambda line: self._record_stdout_line(state, line)),
                    daemon=True,
                )
                stderr_thread = threading.Thread(
                    target=self._drain_stream,
                    args=(proc.stderr, lambda line: self._record_stderr_line(state, line)),
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                try:
                    return_code = proc.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(Exception):
                        proc.kill()
                    stdout_thread.join(timeout=1)
                    stderr_thread.join(timeout=1)
                    raise
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
                proc = subprocess.CompletedProcess(cmd, return_code, "", "")
            except subprocess.TimeoutExpired as exc:
                partial = Path(output_file.name).read_text(encoding="utf-8").strip()
                detail = (
                    f"Codex reply timed out after {timeout_seconds} seconds."
                    " Try a shorter request, split the task, or increase CODEX_MESSAGE_TIMEOUT_SECONDS."
                )
                if partial:
                    detail += f"\n\nPartial assistant reply:\n{partial[-2000:]}"
                self._mark_trace_finished(state, partial, None)
                raise RuntimeError(detail) from exc
            state.last_return_code = proc.returncode

            last_message = Path(output_file.name).read_text(encoding="utf-8").strip()
            self._mark_trace_finished(state, last_message, proc.returncode)
            if state.cancel_requested:
                raise CodexReplyCancelled("Codex reply cancelled by user.")
            if proc.returncode != 0:
                if last_message:
                    logger.warning(
                        "codex exec returned non-zero with partial assistant output",
                        extra={"session_id": session_id, "return_code": proc.returncode},
                    )
                    return last_message
                raise self._format_exec_error(proc, last_message)

            if not last_message:
                raise self._format_exec_error(proc, last_message)

            return last_message
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(output_file.name)

    def get_status(self, session_id: str) -> dict:
        state = self.sessions.get(session_id)
        if state is None:
            return {"exists": False}
        with state.trace_lock:
            running = state.running
            current_prompt = state.current_prompt
            current_started_at = state.current_started_at
            current_finished_at = state.current_finished_at
            event_count = state.event_count
            recent_events = list(state.recent_events)
            recent_raw_events = list(state.recent_raw_events)
            stderr_lines = list(state.stderr_lines)
            latest_reply_preview = state.latest_reply_preview
        return {
            "exists": True,
            "alive": True,
            "workspace": str(state.workspace),
            "thread_id": state.thread_id,
            "last_return_code": state.last_return_code,
            "timeout_seconds": state.timeout_seconds or self.timeout_seconds,
            "running": running,
            "current_prompt": current_prompt,
            "current_started_at": current_started_at,
            "current_finished_at": current_finished_at,
            "event_count": event_count,
            "recent_events": recent_events,
            "recent_raw_events": recent_raw_events,
            "stderr_lines": stderr_lines,
            "latest_reply_preview": latest_reply_preview,
            "cancel_requested": state.cancel_requested,
            "active_pid": state.active_pid,
        }

    def set_session_timeout(self, session_id: str, timeout_seconds: int) -> dict:
        state = self.sessions.get(session_id)
        if state is None:
            raise RuntimeError(f"Session {session_id} not found in CLI backend")
        state.timeout_seconds = timeout_seconds
        return self.get_status(session_id)

    def reset_session(self, session_id: str, workspace: Path) -> CodexSessionInfo:
        self.close_session(session_id)
        return self.create_session(session_id=session_id, workspace=workspace)

    def cancel_running_reply(self, session_id: str) -> dict:
        state = self.sessions.get(session_id)
        if state is None:
            return {"ok": False, "reason": "missing_session"}
        with state.trace_lock:
            proc = state.active_process
            pid = state.active_pid
            running = state.running
            if not running or proc is None or proc.poll() is not None:
                return {"ok": False, "reason": "not_running", "pid": pid}
            state.cancel_requested = True
        with contextlib.suppress(Exception):
            proc.terminate()
        return {"ok": True, "reason": "cancelled", "pid": pid}

    def close_session(self, session_id: str) -> None:
        state = self.sessions.pop(session_id, None)
        if state is None:
            return
        with state.trace_lock:
            proc = state.active_process
            state.cancel_requested = True
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.terminate()
