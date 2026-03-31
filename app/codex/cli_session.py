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
from dataclasses import dataclass
from pathlib import Path

from app.codex.base import CodexBackend, CodexSessionInfo

logger = logging.getLogger(__name__)


@dataclass
class _SessionState:
    workspace: Path
    thread_id: str | None = None
    last_return_code: int | None = None


class CodexCliSessionBackend(CodexBackend):
    def __init__(
        self,
        codex_bin: str,
        codex_args: str,
        timeout_seconds: int = 120,
        proxy_url: str | None = None,
        debug_mode: bool = False,
    ) -> None:
        self.codex_bin = codex_bin
        self.codex_args = self._normalize_cli_args_string(codex_args)
        self.timeout_seconds = timeout_seconds
        self.proxy_url = proxy_url
        self.debug_mode = debug_mode
        self.sessions: dict[str, _SessionState] = {}

    def _build_process_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.proxy_url:
            env["HTTP_PROXY"] = self.proxy_url
            env["HTTPS_PROXY"] = self.proxy_url
            env["ALL_PROXY"] = self.proxy_url
            env["http_proxy"] = self.proxy_url
            env["https_proxy"] = self.proxy_url
            env["all_proxy"] = self.proxy_url
        return env

    def _normalize_cli_args_string(self, raw_args: str) -> str:
        normalized = self._normalize_cli_args(shlex.split(raw_args))
        return shlex.join(normalized)

    def _normalize_cli_args(self, args: list[str]) -> list[str]:
        normalized: list[str] = []
        removed: list[str] = []
        skip_next = False
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
            normalized.append(arg)

        if removed:
            logger.warning(
                "normalizing codex cli args for exec backend",
                extra={"removed_args": removed, "normalized_args": normalized},
            )
        return normalized

    def _run_codex_command(self, workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        cmd = [self.codex_bin, *args]
        logger.debug("running codex command", extra={"cmd": cmd, "workspace": str(workspace)})
        return subprocess.run(
            cmd,
            cwd=workspace,
            env=self._build_process_env(),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
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
        self.sessions[session_id] = _SessionState(workspace=workspace)
        logger.info(
            "created codex exec-backed session",
            extra={"session_id": session_id, "workspace": str(workspace), "mode": "codex_cli_session"},
        )
        return CodexSessionInfo(session_id=session_id, workspace=workspace, mode="codex_cli_session")

    def send_message(self, session_id: str, message: str) -> str:
        state = self.sessions.get(session_id)
        if state is None:
            raise RuntimeError(f"Session {session_id} not found in CLI backend")

        output_file = tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt", delete=False)
        output_file.close()
        try:
            proc = self._run_codex_command(state.workspace, self._build_exec_command(state, message, output_file.name))
            state.last_return_code = proc.returncode
            if not state.thread_id:
                state.thread_id = self._extract_thread_id(proc.stdout)

            last_message = Path(output_file.name).read_text(encoding="utf-8").strip()
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
        return {
            "exists": True,
            "alive": True,
            "workspace": str(state.workspace),
            "thread_id": state.thread_id,
            "last_return_code": state.last_return_code,
        }

    def reset_session(self, session_id: str, workspace: Path) -> CodexSessionInfo:
        self.close_session(session_id)
        return self.create_session(session_id=session_id, workspace=workspace)

    def close_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
