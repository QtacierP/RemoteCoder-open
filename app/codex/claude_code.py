"""Claude Code CLI backend using non-interactive print mode."""

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
    config_dir: str
    has_started: bool = False
    last_return_code: int | None = None
    timeout_seconds: int | None = None
    provider_label: str = "default"
    provider_model: str | None = None
    provider_base_url: str | None = None
    provider_api_key: str | None = None
    trace_lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    current_prompt: str = ""
    current_started_at: float | None = None
    current_finished_at: float | None = None
    event_count: int = 0
    recent_events: deque[str] = field(default_factory=lambda: deque(maxlen=120))
    recent_raw_events: deque[str] = field(default_factory=lambda: deque(maxlen=120))
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=120))
    latest_reply_preview: str = ""
    active_process: subprocess.Popen[str] | None = None
    cancel_requested: bool = False
    active_pid: int | None = None


class ClaudeCodeSessionBackend(CodexBackend):
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
        claude_bin: str,
        claude_args: str,
        thinking_mode: str = "",
        timeout_seconds: int = 120,
        proxy_url: str | None = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.claude_args = self._normalize_cli_args_string(claude_args)
        self.thinking_mode = (thinking_mode or "").strip()
        self.timeout_seconds = timeout_seconds
        self.proxy_url = proxy_url
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

    def _build_session_env(self, state: _SessionState) -> dict[str, str]:
        env = self._build_process_env()
        env["CLAUDE_CONFIG_DIR"] = state.config_dir
        if state.provider_base_url:
            env["ANTHROPIC_BASE_URL"] = state.provider_base_url
        else:
            env.pop("ANTHROPIC_BASE_URL", None)
        if state.provider_api_key:
            env["ANTHROPIC_API_KEY"] = state.provider_api_key
            env["ANTHROPIC_AUTH_TOKEN"] = state.provider_api_key
        else:
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if state.provider_model:
            env["ANTHROPIC_MODEL"] = state.provider_model
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = state.provider_model
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = state.provider_model
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = state.provider_model
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = state.provider_model
        else:
            env.pop("ANTHROPIC_MODEL", None)
            env.pop("ANTHROPIC_DEFAULT_OPUS_MODEL", None)
            env.pop("ANTHROPIC_DEFAULT_SONNET_MODEL", None)
            env.pop("ANTHROPIC_DEFAULT_HAIKU_MODEL", None)
            env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
        env["ENABLE_TOOL_SEARCH"] = "false"
        return env

    def _write_claude_config(self, state: _SessionState) -> None:
        config_dir = Path(state.config_dir)
        config_dir.mkdir(parents=True, exist_ok=True)
        settings_payload = {
            "env": {
                "ANTHROPIC_BASE_URL": state.provider_base_url or "",
                "ANTHROPIC_AUTH_TOKEN": state.provider_api_key or "",
                "ANTHROPIC_MODEL": state.provider_model or "",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": state.provider_model or "",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": state.provider_model or "",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": state.provider_model or "",
                "CLAUDE_CODE_SUBAGENT_MODEL": state.provider_model or "",
                "ENABLE_TOOL_SEARCH": "false",
            }
        }
        (config_dir / "settings.json").write_text(
            json.dumps(settings_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (config_dir / ".claude.json").write_text(
            json.dumps({"hasCompletedOnboarding": True}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _normalize_cli_args_string(self, raw_args: str) -> str:
        return shlex.join(self._normalize_cli_args(shlex.split(raw_args)))

    def _normalize_cli_args(self, args: list[str]) -> list[str]:
        normalized: list[str] = []
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in {"--print", "-p", "--bare", "--continue", "--verbose"}:
                continue
            if arg in {"--output-format", "--model", "--thinking", "--max-thinking-tokens"}:
                skip_next = True
                continue
            normalized.append(arg)
        return normalized

    @staticmethod
    def _preview_text(value: object, limit: int = 160) -> str:
        text = str(value).strip()
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)].rstrip()}..."

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
        self._record_event(state, "started", f"prompt={self._preview_text(message, 120)}")

    def _record_event(self, state: _SessionState, kind: str, detail: str) -> None:
        entry = f"{kind}: {detail}".strip()
        with state.trace_lock:
            state.event_count += 1
            state.recent_events.append(entry)

    def _record_raw_event(self, state: _SessionState, source: str, raw_line: str) -> None:
        stripped = raw_line.rstrip("\n")
        if not stripped:
            return
        with state.trace_lock:
            state.event_count += 1
            state.recent_raw_events.append(f"{source}: {stripped}")

    def _record_stderr_line(self, state: _SessionState, raw_line: str) -> None:
        stripped = raw_line.rstrip("\n")
        if not stripped:
            return
        with state.trace_lock:
            state.stderr_lines.append(stripped)
        self._record_raw_event(state, "stderr", raw_line)
        self._record_event(state, "stderr", self._preview_text(stripped, 160))

    def _record_stdout_line(self, state: _SessionState, raw_line: str, chunks: list[str]) -> None:
        chunks.append(raw_line)
        self._record_raw_event(state, "stdout", raw_line)
        stripped = raw_line.rstrip("\n").strip()
        if stripped:
            self._record_event(state, "stdout", self._preview_text(stripped, 160))

    def _mark_trace_finished(self, state: _SessionState, reply: str, return_code: int | None) -> None:
        with state.trace_lock:
            state.running = False
            state.active_process = None
            state.active_pid = None
            state.current_finished_at = time.time()
            state.last_return_code = return_code
            state.latest_reply_preview = self._preview_text(reply, 1200)
        summary = f"return_code={return_code} reply={self._preview_text(reply, 160) or '(empty)'}"
        self._record_event(state, "finished", summary)

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

    def _build_command(self, state: _SessionState, message: str) -> list[str]:
        if not state.provider_model or not state.provider_base_url or not state.provider_api_key:
            raise RuntimeError(
                "Claude Code provider is not configured.\n"
                "Use /claude_code_api_add <label> :: <model> :: <base_url> :: <api_key>\n"
                "Then /claude_code_api_switch <label>"
            )
        extra = shlex.split(self.claude_args)
        cmd = [self.claude_bin, *extra, "--bare", "--print", "--output-format", "text", "--model", state.provider_model]
        if self.thinking_mode:
            cmd.extend(["--thinking", self.thinking_mode])
        if state.has_started:
            cmd.append("--continue")
        cmd.append(message)
        return cmd

    def create_session(self, session_id: str, workspace: Path) -> CodexSessionInfo:
        config_dir = tempfile.mkdtemp(prefix=f"claude-code-session-{session_id[:8]}-")
        state = _SessionState(workspace=workspace, config_dir=config_dir, timeout_seconds=self.timeout_seconds)
        self._write_claude_config(state)
        self.sessions[session_id] = state
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode="claude_code_cli_session")

    def restore_session(self, session_id: str, workspace: Path, backend_state: dict | None = None) -> CodexSessionInfo:
        config_dir = ""
        state = _SessionState(workspace=workspace, config_dir=config_dir, timeout_seconds=self.timeout_seconds)
        if backend_state:
            restored_dir = backend_state.get("config_dir")
            if isinstance(restored_dir, str) and restored_dir.strip():
                state.config_dir = restored_dir.strip()
            has_started = backend_state.get("has_started")
            if isinstance(has_started, bool):
                state.has_started = has_started
            timeout_seconds = backend_state.get("timeout_seconds")
            if isinstance(timeout_seconds, int) and (timeout_seconds > 0 or timeout_seconds == -1):
                state.timeout_seconds = timeout_seconds
            last_return_code = backend_state.get("last_return_code")
            if isinstance(last_return_code, int):
                state.last_return_code = last_return_code
            latest_reply_preview = backend_state.get("latest_reply_preview")
            if isinstance(latest_reply_preview, str) and latest_reply_preview.strip():
                state.latest_reply_preview = self._preview_text(latest_reply_preview, 1200)
            provider_label = backend_state.get("provider_label")
            provider_model = backend_state.get("provider_model")
            provider_base_url = backend_state.get("provider_base_url")
            if isinstance(provider_label, str) and provider_label.strip():
                state.provider_label = provider_label.strip()
            if isinstance(provider_model, str) and provider_model.strip():
                state.provider_model = provider_model.strip()
            if isinstance(provider_base_url, str) and provider_base_url.strip():
                state.provider_base_url = provider_base_url.strip()
        if not state.config_dir:
            state.config_dir = tempfile.mkdtemp(prefix=f"claude-code-session-{session_id[:8]}-")
        self._write_claude_config(state)
        self.sessions[session_id] = state
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode="claude_code_cli_session")

    def send_message(self, session_id: str, message: str) -> str:
        state = self.sessions.get(session_id)
        if state is None:
            raise RuntimeError(f"Session {session_id} not found in Claude Code backend")
        timeout_seconds = state.timeout_seconds if state.timeout_seconds is not None else self.timeout_seconds
        wait_timeout = None if timeout_seconds == -1 else timeout_seconds
        self._mark_trace_started(state, message)
        try:
            cmd = self._build_command(state, message)
            proc = subprocess.Popen(
                cmd,
                cwd=state.workspace,
                env=self._build_session_env(state),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            with state.trace_lock:
                state.active_process = proc
                state.active_pid = proc.pid
            self._record_event(state, "spawn", f"pid={proc.pid} cwd={state.workspace}")
            self._record_event(state, "command", self._preview_text(" ".join(cmd), 240))
            stdout_chunks: list[str] = []
            stdout_thread = threading.Thread(
                target=self._drain_stream,
                args=(proc.stdout, lambda line: self._record_stdout_line(state, line, stdout_chunks)),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=self._drain_stream,
                args=(proc.stderr, lambda line: self._record_stderr_line(state, line)),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            wait_started = time.time()
            next_progress_at = wait_started + 5.0
            try:
                while True:
                    return_code = proc.poll()
                    if return_code is not None:
                        break
                    now = time.time()
                    if now >= next_progress_at:
                        elapsed = int(now - wait_started)
                        self._record_event(state, "progress", f"waiting elapsed={elapsed}s pid={proc.pid}")
                        next_progress_at = now + 5.0
                    if wait_timeout is not None and now - wait_started >= wait_timeout:
                        raise subprocess.TimeoutExpired(cmd, wait_timeout)
                    time.sleep(0.2)
            except subprocess.TimeoutExpired as exc:
                with contextlib.suppress(Exception):
                    proc.kill()
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
                self._mark_trace_finished(state, "", None)
                self._record_event(state, "timeout", f"timeout_seconds={timeout_seconds}")
                raise RuntimeError(
                    f"Claude Code reply timed out after {timeout_seconds} seconds."
                ) from exc
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            reply = "".join(stdout_chunks).strip()
            self._mark_trace_finished(state, reply, proc.returncode)
            state.has_started = True
            if state.cancel_requested:
                self._record_event(state, "cancelled", "cancel_requested=True")
                raise CodexReplyCancelled("Claude Code reply cancelled by user.")
            if proc.returncode != 0:
                stderr = "\n".join(list(state.stderr_lines)[-40:])
                detail = f"Claude Code failed with code {proc.returncode}"
                if stderr:
                    detail += f"\nStderr:\n{stderr[-2000:]}"
                raise RuntimeError(detail)
            if not reply:
                stderr = "\n".join(list(state.stderr_lines)[-40:])
                detail = "Claude Code returned an empty reply."
                if stderr:
                    detail += f"\nStderr:\n{stderr[-2000:]}"
                raise RuntimeError(detail)
            return reply
        finally:
            with state.trace_lock:
                state.active_process = None
                state.active_pid = None

    def get_status(self, session_id: str) -> dict:
        state = self.sessions.get(session_id)
        if state is None:
            return {"exists": False}
        with state.trace_lock:
            return {
                "exists": True,
                "alive": True,
                "workspace": str(state.workspace),
                "config_dir": state.config_dir,
                "has_started": state.has_started,
                "last_return_code": state.last_return_code,
                "timeout_seconds": state.timeout_seconds if state.timeout_seconds is not None else self.timeout_seconds,
                "provider_label": state.provider_label,
                "provider_model": state.provider_model,
                "provider_base_url": state.provider_base_url,
                "running": state.running,
                "current_prompt": state.current_prompt,
                "current_started_at": state.current_started_at,
                "current_finished_at": state.current_finished_at,
                "event_count": state.event_count,
                "recent_events": list(state.recent_events),
                "recent_raw_events": list(state.recent_raw_events),
                "stderr_lines": list(state.stderr_lines),
                "latest_reply_preview": state.latest_reply_preview,
                "cancel_requested": state.cancel_requested,
                "active_pid": state.active_pid,
            }

    def set_session_timeout(self, session_id: str, timeout_seconds: int) -> dict:
        state = self.sessions.get(session_id)
        if state is None:
            raise RuntimeError(f"Session {session_id} not found in Claude Code backend")
        state.timeout_seconds = timeout_seconds
        return self.get_status(session_id)

    def set_session_provider(self, session_id: str, provider: dict | None) -> dict:
        state = self.sessions.get(session_id)
        if state is None:
            raise RuntimeError(f"Session {session_id} not found in Claude Code backend")
        with state.trace_lock:
            state.has_started = False
            state.last_return_code = None
            state.latest_reply_preview = ""
            state.current_prompt = ""
            state.current_started_at = None
            state.current_finished_at = None
            state.event_count = 0
            state.recent_events.clear()
            state.recent_raw_events.clear()
            state.stderr_lines.clear()
            if provider is None:
                state.provider_label = "default"
                state.provider_model = None
                state.provider_base_url = None
                state.provider_api_key = None
            else:
                state.provider_label = str(provider["label"]).strip() or "default"
                state.provider_model = str(provider["model"]).strip()
                state.provider_base_url = str(provider["base_url"]).strip()
                state.provider_api_key = str(provider["api_key"]).strip()
            self._write_claude_config(state)
        return self.get_status(session_id)

    def reset_session(self, session_id: str, workspace: Path) -> CodexSessionInfo:
        existing = self.sessions.get(session_id)
        provider = None
        timeout_seconds = self.timeout_seconds
        if existing is not None:
            provider = {
                "label": existing.provider_label,
                "model": existing.provider_model,
                "base_url": existing.provider_base_url,
                "api_key": existing.provider_api_key,
            } if existing.provider_model and existing.provider_base_url and existing.provider_api_key else None
            timeout_seconds = existing.timeout_seconds if existing.timeout_seconds is not None else self.timeout_seconds
        self.close_session(session_id)
        info = self.create_session(session_id=session_id, workspace=workspace)
        self.sessions[session_id].timeout_seconds = timeout_seconds
        if provider is not None:
            self.set_session_provider(session_id, provider)
        return info

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
