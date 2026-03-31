"""Per-chat shell command runner with background job support."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _JobState:
    job_id: int
    label: str
    command: str
    cwd: Path
    log_path: Path
    process: subprocess.Popen[str]
    started_at: float
    notified_done: bool = False


@dataclass
class _ShellState:
    cwd: Path
    busy: bool = False
    last_exit_code: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    jobs: dict[int, _JobState] = field(default_factory=dict)
    next_job_id: int = 1


class ShellService:
    def __init__(
        self,
        default_workspace: Path,
        timeout_seconds: int = 60,
        log_root: str | Path | None = None,
    ) -> None:
        self.default_workspace = default_workspace.resolve()
        self.timeout_seconds = timeout_seconds
        self.log_root = Path(log_root or "/tmp/remotecoder_shell_jobs").resolve()
        self.log_root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[int, _ShellState] = {}
        self._lock = threading.Lock()

    def _resolve_workspace_path(self, workspace: str | Path, requested_path: str | Path | None = None) -> Path:
        root = Path(workspace).resolve()
        if requested_path is None or str(requested_path).strip() == "":
            candidate = root
        else:
            raw = Path(str(requested_path).strip()).expanduser()
            candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Path '{candidate}' is outside workspace '{root}'") from exc
        return candidate

    def resolve_workspace_path(self, workspace: str | Path, requested_path: str | Path | None = None) -> Path:
        return self._resolve_workspace_path(workspace, requested_path)

    @staticmethod
    def _truncate_lines(lines: list[str], limit: int) -> tuple[list[str], bool]:
        if len(lines) <= limit:
            return lines, False
        return lines[:limit], True

    def _get_or_create(self, chat_id: int, workspace: str | Path | None = None) -> _ShellState:
        target = Path(workspace or self.default_workspace).resolve()
        with self._lock:
            state = self._sessions.get(chat_id)
            if state is None:
                state = _ShellState(cwd=target)
                self._sessions[chat_id] = state
            return state

    def execute(self, chat_id: int, command: str, workspace: str | Path | None = None) -> str:
        state = self._get_or_create(chat_id, workspace)
        with state.lock:
            state.busy = True
            try:
                marker = "__RC_CWD__="
                shell_script = f"{command}\nprintf '\\n{marker}%s\\n' \"$PWD\""
                proc = subprocess.run(
                    ["bash", "-lc", shell_script],
                    cwd=state.cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
                state.last_exit_code = proc.returncode
                combined = (proc.stdout or "") + (proc.stderr or "")
                lines = combined.splitlines()
                new_cwd = state.cwd
                cleaned_lines: list[str] = []
                for line in lines:
                    if line.startswith(marker):
                        candidate = Path(line[len(marker) :].strip()).expanduser()
                        if candidate.is_absolute():
                            new_cwd = candidate.resolve()
                        continue
                    cleaned_lines.append(line)
                state.cwd = new_cwd
                output = "\n".join(cleaned_lines).strip()
                if output:
                    return output
                return f"(command finished with exit code {proc.returncode})"
            finally:
                state.busy = False

    def _git_repo_root(self, workspace: str | Path) -> Path:
        target = Path(workspace).resolve()
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=target,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError(f"Not a git repository: {target}")
        return Path((proc.stdout or "").strip()).resolve()

    def _run_git(self, workspace: str | Path, args: list[str], timeout: int = 20) -> str:
        repo_root = self._git_repo_root(workspace)
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode != 0 and not output:
            output = f"git {' '.join(args)} failed with exit code {proc.returncode}"
        return output or "(no output)"

    def start_background(
        self,
        chat_id: int,
        command: str,
        workspace: str | Path | None = None,
        label: str = "",
    ) -> dict:
        state = self._get_or_create(chat_id, workspace)
        with state.lock:
            job_id = state.next_job_id
            state.next_job_id += 1
            log_path = self.log_root / f"chat_{chat_id}_job_{job_id}.log"
            with log_path.open("w", encoding="utf-8") as log_file:
                if label.strip():
                    log_file.write(f"[label] {label.strip()}\n")
                log_file.write(f"$ {command}\n")
                log_file.write(f"[cwd] {state.cwd}\n")
                log_file.write(f"[started_at] {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}\n\n")
                log_file.flush()
                process = subprocess.Popen(
                    ["bash", "-lc", command],
                    cwd=state.cwd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            job = _JobState(
                job_id=job_id,
                label=label.strip(),
                command=command,
                cwd=state.cwd,
                log_path=log_path,
                process=process,
                started_at=time.time(),
            )
            state.jobs[job_id] = job
            return self._job_snapshot(job)

    def _job_snapshot(self, job: _JobState) -> dict:
        return {
            "job_id": job.job_id,
            "label": job.label,
            "command": job.command,
            "cwd": str(job.cwd),
            "log_path": str(job.log_path),
            "started_at": job.started_at,
            "pid": job.process.pid,
            "running": job.process.poll() is None,
            "return_code": job.process.poll(),
        }

    def _latest_job(self, state: _ShellState) -> _JobState | None:
        if not state.jobs:
            return None
        latest_id = max(state.jobs)
        return state.jobs[latest_id]

    def get_status(self, chat_id: int) -> dict:
        with self._lock:
            state = self._sessions.get(chat_id)
        if state is None:
            return {
                "exists": False,
                "workspace": str(self.default_workspace),
                "cwd": str(self.default_workspace),
                "busy": False,
                "last_exit_code": None,
                "jobs": [],
                "active_job_ids": [],
                "latest_job_id": None,
            }
        with state.lock:
            jobs = [self._job_snapshot(state.jobs[job_id]) for job_id in sorted(state.jobs)]
            active_job_ids = [job["job_id"] for job in jobs if job["running"]]
            latest_job = self._latest_job(state)
            return {
                "exists": True,
                "workspace": str(self.default_workspace),
                "cwd": str(state.cwd),
                "busy": state.busy,
                "last_exit_code": state.last_exit_code,
                "jobs": jobs,
                "active_job_ids": active_job_ids,
                "latest_job_id": latest_job.job_id if latest_job else None,
            }

    def get_job(self, chat_id: int, job_id: int | None = None) -> dict | None:
        with self._lock:
            state = self._sessions.get(chat_id)
        if state is None:
            return None
        with state.lock:
            if job_id is None:
                job = self._latest_job(state)
            else:
                job = state.jobs.get(job_id)
            if job is None:
                return None
            return self._job_snapshot(job)

    def list_jobs(self, chat_id: int) -> list[dict]:
        with self._lock:
            state = self._sessions.get(chat_id)
        if state is None:
            return []
        with state.lock:
            jobs = [self._job_snapshot(state.jobs[job_id]) for job_id in sorted(state.jobs)]
            return sorted(
                jobs,
                key=lambda job: (
                    0 if job["running"] else 1,
                    -job["job_id"],
                ),
            )

    def tail_logs(self, chat_id: int, job_id: int | None = None, lines: int = 20) -> dict:
        lines = max(1, min(lines, 400))
        with self._lock:
            state = self._sessions.get(chat_id)
        if state is None:
            return {"ok": False, "error": "No shell session for this chat yet."}
        with state.lock:
            if job_id is None:
                job = self._latest_job(state)
            else:
                job = state.jobs.get(job_id)
            if job is None:
                return {"ok": False, "error": "No matching shell job found."}
            snapshot = self._job_snapshot(job)
            text = ""
            if job.log_path.exists():
                text = job.log_path.read_text(encoding="utf-8", errors="replace")
            all_lines = text.splitlines()
            tail = all_lines[-lines:]
            return {
                "ok": True,
                "job": snapshot,
                "line_count": len(all_lines),
                "shown_lines": len(tail),
                "output": "\n".join(tail).strip(),
            }

    def stop_job(self, chat_id: int, job_id: int, force: bool = False) -> dict:
        with self._lock:
            state = self._sessions.get(chat_id)
        if state is None:
            return {"ok": False, "error": "No shell session for this chat yet."}
        with state.lock:
            job = state.jobs.get(job_id)
            if job is None:
                return {"ok": False, "error": f"No shell job #{job_id} found."}
            if job.process.poll() is not None:
                snapshot = self._job_snapshot(job)
                return {"ok": True, "job": snapshot, "already_stopped": True}

            sig = signal.SIGKILL if force else signal.SIGTERM
            with suppress(ProcessLookupError):
                os.killpg(job.process.pid, sig)

        try:
            job.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if not force:
                with suppress(ProcessLookupError):
                    os.killpg(job.process.pid, signal.SIGKILL)
                with suppress(subprocess.TimeoutExpired):
                    job.process.wait(timeout=3)

        snapshot = self.get_job(chat_id, job_id)
        if snapshot is None:
            return {"ok": False, "error": f"No shell job #{job_id} found after stop attempt."}
        return {"ok": True, "job": snapshot, "already_stopped": False}

    def stop_all_jobs(self, chat_id: int, force: bool = False) -> dict:
        jobs = self.list_jobs(chat_id)
        if not jobs:
            return {"ok": True, "stopped": [], "already_stopped": [], "missing": False}
        stopped: list[dict] = []
        already_stopped: list[dict] = []
        for job in jobs:
            result = self.stop_job(chat_id, job["job_id"], force=force)
            if not result["ok"]:
                continue
            if result.get("already_stopped"):
                already_stopped.append(result["job"])
            else:
                stopped.append(result["job"])
        return {"ok": True, "stopped": stopped, "already_stopped": already_stopped, "missing": False}

    def list_chats(self) -> list[int]:
        with self._lock:
            return sorted(self._sessions)

    def collect_finished_notifications(self, tail_lines: int = 20) -> list[dict]:
        tail_lines = max(1, min(tail_lines, 100))
        notifications: list[dict] = []
        with self._lock:
            items = list(self._sessions.items())
        for chat_id, state in items:
            with state.lock:
                for job_id in sorted(state.jobs):
                    job = state.jobs[job_id]
                    if job.notified_done:
                        continue
                    if job.process.poll() is None:
                        continue
                    job.notified_done = True
                    snapshot = self._job_snapshot(job)
                    text = ""
                    if job.log_path.exists():
                        text = job.log_path.read_text(encoding="utf-8", errors="replace")
                    all_lines = text.splitlines()
                    tail = "\n".join(all_lines[-tail_lines:]).strip()
                    notifications.append(
                        {
                            "chat_id": chat_id,
                            "job": snapshot,
                            "line_count": len(all_lines),
                            "shown_lines": min(len(all_lines), tail_lines),
                            "output": tail,
                        }
                    )
        return notifications

    def system_status(self) -> dict:
        load1, load5, load15 = os.getloadavg()
        meminfo: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            number = value.strip().split()[0]
            if number.isdigit():
                meminfo[key] = int(number)

        mem_total_kb = meminfo.get("MemTotal", 0)
        mem_available_kb = meminfo.get("MemAvailable", 0)
        mem_used_kb = max(mem_total_kb - mem_available_kb, 0)
        disk = shutil.disk_usage(self.default_workspace)

        ps_output = subprocess.run(
            ["ps", "-eo", "pid,pcpu,pmem,etime,comm", "--sort=-pcpu"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        process_lines = [line.rstrip() for line in (ps_output.stdout or "").splitlines() if line.strip()]
        top_processes = process_lines[:6]

        return {
            "cpu_count": os.cpu_count(),
            "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
            "mem_total_gb": round(mem_total_kb / 1024 / 1024, 2),
            "mem_used_gb": round(mem_used_kb / 1024 / 1024, 2),
            "mem_available_gb": round(mem_available_kb / 1024 / 1024, 2),
            "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 2),
            "disk_used_gb": round((disk.total - disk.free) / 1024 / 1024 / 1024, 2),
            "disk_free_gb": round(disk.free / 1024 / 1024 / 1024, 2),
            "top_processes": top_processes,
        }

    def format_system_status(self) -> str:
        status = self.system_status()
        lines = [
            f"cpu_count: {status['cpu_count']}",
            f"load_avg: {status['load'][0]} {status['load'][1]} {status['load'][2]}",
            f"memory_gb: used={status['mem_used_gb']} available={status['mem_available_gb']} total={status['mem_total_gb']}",
            f"disk_gb: used={status['disk_used_gb']} free={status['disk_free_gb']} total={status['disk_total_gb']}",
            "top_processes:",
        ]
        lines.extend(status["top_processes"] or ["(no process data)"])
        return "\n".join(lines)

    def gpu_status(self) -> dict:
        nvidia_smi = shutil.which("nvidia-smi")
        if not nvidia_smi:
            return {"available": False, "error": "nvidia-smi not found"}

        proc = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            return {
                "available": False,
                "error": (proc.stderr or proc.stdout or "nvidia-smi failed").strip(),
            }

        gpus: list[dict] = []
        for line in (proc.stdout or "").splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 6:
                continue
            gpus.append(
                {
                    "index": parts[0],
                    "name": parts[1],
                    "temperature_c": parts[2],
                    "utilization_gpu": parts[3],
                    "memory_used_mb": parts[4],
                    "memory_total_mb": parts[5],
                }
            )
        return {"available": True, "gpus": gpus}

    def format_gpu_status(self) -> str:
        status = self.gpu_status()
        if not status["available"]:
            return f"GPU status unavailable: {status['error']}"
        gpus = status["gpus"]
        if not gpus:
            return "GPU status unavailable: no GPUs reported by nvidia-smi"
        lines = [f"gpus: {len(gpus)}"]
        for gpu in gpus:
            lines.append(
                f"gpu#{gpu['index']}: {gpu['name']} util={gpu['utilization_gpu']}% "
                f"mem={gpu['memory_used_mb']}/{gpu['memory_total_mb']} MiB temp={gpu['temperature_c']}C"
            )
        return "\n".join(lines)

    def list_directory(self, workspace: str | Path, requested_path: str | Path | None = None, limit: int = 200) -> str:
        target = self._resolve_workspace_path(workspace, requested_path)
        if not target.exists():
            return f"Path not found: {target}"
        if target.is_file():
            stat = target.stat()
            return f"file: {target}\nsize_bytes: {stat.st_size}"

        entries = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        rendered = []
        for entry in entries:
            suffix = "/" if entry.is_dir() else ""
            rendered.append(f"{entry.name}{suffix}")
        shown, truncated = self._truncate_lines(rendered, limit)
        lines = [f"path: {target}", f"entries: {len(entries)}"]
        lines.extend(shown or ["(empty directory)"])
        if truncated:
            lines.append(f"... truncated to first {limit} entries")
        return "\n".join(lines)

    def render_tree(
        self,
        workspace: str | Path,
        requested_path: str | Path | None = None,
        max_depth: int = 2,
        limit: int = 200,
    ) -> str:
        target = self._resolve_workspace_path(workspace, requested_path)
        if not target.exists():
            return f"Path not found: {target}"
        if target.is_file():
            return f"file: {target}"

        lines = [f"tree: {target}"]
        count = 0
        truncated = False

        def walk(node: Path, depth: int) -> None:
            nonlocal count, truncated
            if truncated or depth > max_depth:
                return
            entries = sorted(node.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
            for entry in entries:
                if count >= limit:
                    truncated = True
                    return
                prefix = "  " * depth
                suffix = "/" if entry.is_dir() else ""
                lines.append(f"{prefix}{entry.name}{suffix}")
                count += 1
                if entry.is_dir():
                    walk(entry, depth + 1)

        walk(target, 0)
        if len(lines) == 1:
            lines.append("(empty directory)")
        if truncated:
            lines.append(f"... truncated to first {limit} entries")
        return "\n".join(lines)

    def read_text_file(
        self,
        workspace: str | Path,
        requested_path: str | Path,
        start_line: int = 1,
        max_lines: int = 200,
    ) -> str:
        target = self._resolve_workspace_path(workspace, requested_path)
        if not target.exists():
            return f"Path not found: {target}"
        if not target.is_file():
            return f"Path is not a file: {target}"

        text = target.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        if not all_lines:
            return f"file: {target}\n(lines 1-0 of 0)\n(empty file)"

        start = max(start_line, 1)
        start_idx = start - 1
        selected = all_lines[start_idx : start_idx + max_lines]
        end_line = start_idx + len(selected)
        lines = [f"file: {target}", f"(lines {start}-{end_line} of {len(all_lines)})"]
        lines.extend(selected or ["(no content in requested range)"])
        if end_line < len(all_lines):
            lines.append(f"... truncated, use /read {requested_path} {end_line + 1} {max_lines}")
        return "\n".join(lines)

    def tail_text_file(
        self,
        workspace: str | Path,
        requested_path: str | Path,
        lines: int = 50,
    ) -> str:
        target = self._resolve_workspace_path(workspace, requested_path)
        if not target.exists():
            return f"Path not found: {target}"
        if not target.is_file():
            return f"Path is not a file: {target}"

        text = target.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        tail = all_lines[-lines:]
        start_line = max(len(all_lines) - len(tail) + 1, 1) if all_lines else 1
        header = f"file: {target}\n(last {len(tail)} of {len(all_lines)} lines, starting at line {start_line})"
        body = "\n".join(tail).strip()
        return f"{header}\n{body or '(empty file)'}"

    def find_files(
        self,
        workspace: str | Path,
        pattern: str,
        requested_path: str | Path | None = None,
        limit: int = 100,
    ) -> str:
        if not pattern.strip():
            return "Usage: /find <pattern> [path]"
        root = self._resolve_workspace_path(workspace, requested_path)
        if root.is_file():
            candidates = [root] if pattern.lower() in root.name.lower() else []
        else:
            candidates = []
            for path in root.rglob("*"):
                if pattern.lower() in path.name.lower():
                    candidates.append(path)
                    if len(candidates) >= limit:
                        break
        lines = [f"search_root: {root}", f"pattern: {pattern}"]
        if not candidates:
            lines.append("(no matches)")
            return "\n".join(lines)
        for match in candidates:
            rel = match.relative_to(Path(workspace).resolve())
            suffix = "/" if match.is_dir() else ""
            lines.append(f"{rel}{suffix}")
        if len(candidates) >= limit:
            lines.append(f"... truncated to first {limit} matches")
        return "\n".join(lines)

    def grep_text(
        self,
        workspace: str | Path,
        pattern: str,
        requested_path: str | Path | None = None,
        limit: int = 80,
    ) -> str:
        if not pattern.strip():
            return "Usage: /grep <pattern> [path]"
        root = self._resolve_workspace_path(workspace, requested_path)
        base = Path(workspace).resolve()
        matches: list[str] = []
        paths = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for idx, line in enumerate(lines, start=1):
                if pattern.lower() in line.lower():
                    rel = path.relative_to(base)
                    matches.append(f"{rel}:{idx}: {line[:300]}")
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break
        result = [f"search_root: {root}", f"pattern: {pattern}"]
        result.extend(matches or ["(no matches)"])
        if len(matches) >= limit:
            result.append(f"... truncated to first {limit} matches")
        return "\n".join(result)

    def git_status(self, workspace: str | Path) -> str:
        repo_root = self._git_repo_root(workspace)
        branch = self._run_git(repo_root, ["branch", "--show-current"])
        status = self._run_git(repo_root, ["status", "--short", "--branch"])
        return f"repo: {repo_root}\nbranch: {branch.strip() or '(detached)'}\n\n{status}"

    def git_add(self, workspace: str | Path, pathspec: str) -> str:
        if not pathspec.strip():
            return "Usage: /git_add <path>"
        repo_root = self._git_repo_root(workspace)
        if pathspec.strip() in {".", "./"}:
            target_arg = "."
        else:
            target = self._resolve_workspace_path(workspace, pathspec)
            target_arg = str(target.relative_to(repo_root))
        output = self._run_git(repo_root, ["add", "--", target_arg])
        status = self._run_git(repo_root, ["status", "--short", "--branch"])
        return f"{output}\n\n{status}" if output != "(no output)" else status

    def git_diff(self, workspace: str | Path, pathspec: str | None = None, max_lines: int = 220) -> str:
        repo_root = self._git_repo_root(workspace)
        args = ["diff", "--", pathspec] if pathspec else ["diff"]
        output = self._run_git(repo_root, args, timeout=30)
        lines = output.splitlines()
        shown, truncated = self._truncate_lines(lines, max_lines)
        result = [f"repo: {repo_root}"]
        if pathspec:
            result.append(f"path: {pathspec}")
        result.extend(shown or ["(no diff)"])
        if truncated:
            result.append(f"... truncated to first {max_lines} lines")
        return "\n".join(result)

    def git_log(self, workspace: str | Path, limit: int = 10) -> str:
        repo_root = self._git_repo_root(workspace)
        count = max(1, min(limit, 30))
        output = self._run_git(
            repo_root,
            ["log", f"-{count}", "--pretty=format:%h %ad %an %s", "--date=short"],
        )
        return f"repo: {repo_root}\n{output}"

    def git_branch(self, workspace: str | Path) -> str:
        repo_root = self._git_repo_root(workspace)
        output = self._run_git(repo_root, ["branch", "-vv"])
        return f"repo: {repo_root}\n{output}"

    def git_commit(self, workspace: str | Path, message: str) -> str:
        if not message.strip():
            return "Usage: /git_commit <message>"
        repo_root = self._git_repo_root(workspace)
        output = self._run_git(repo_root, ["commit", "-m", message], timeout=60)
        status = self._run_git(repo_root, ["status", "--short", "--branch"])
        return f"{output}\n\n{status}"

    def git_show(self, workspace: str | Path, ref: str = "HEAD", max_lines: int = 220) -> str:
        repo_root = self._git_repo_root(workspace)
        output = self._run_git(
            repo_root,
            ["show", "--stat", "--decorate", "--format=fuller", ref],
            timeout=30,
        )
        lines = output.splitlines()
        shown, truncated = self._truncate_lines(lines, max_lines)
        result = [f"repo: {repo_root}", f"ref: {ref}"]
        result.extend(shown or ["(no output)"])
        if truncated:
            result.append(f"... truncated to first {max_lines} lines")
        return "\n".join(result)

    def git_push(self, workspace: str | Path, remote: str | None = None, branch: str | None = None) -> str:
        repo_root = self._git_repo_root(workspace)
        remote_name = remote or "origin"
        branch_name = branch or self._run_git(repo_root, ["branch", "--show-current"]).strip()
        if not branch_name:
            return "Unable to determine current branch for push."
        output = self._run_git(repo_root, ["push", remote_name, branch_name], timeout=120)
        return f"repo: {repo_root}\nremote: {remote_name}\nbranch: {branch_name}\n\n{output}"

    def format_status(self, chat_id: int, tail_lines: int = 20) -> str:
        tail_lines = max(1, min(tail_lines, 200))
        status = self.get_status(chat_id)
        if not status["exists"]:
            return (
                "No shell session for this chat yet.\n"
                f"shell_cwd: {status['cwd']}\n"
                "active_jobs: 0"
            )

        jobs = status["jobs"]
        recent_jobs = jobs[-5:]
        lines = [
            f"shell_exists: {status['exists']}",
            f"shell_busy: {status['busy']}",
            f"shell_cwd: {status['cwd']}",
            f"shell_last_exit_code: {status['last_exit_code']}",
            f"active_jobs: {len(status['active_job_ids'])}",
            f"latest_job_id: {status['latest_job_id']}",
        ]
        if recent_jobs:
            lines.append("recent_jobs:")
            for job in recent_jobs:
                state_label = "running" if job["running"] else f"exit={job['return_code']}"
                label_part = f" label={job['label']}" if job.get("label") else ""
                lines.append(f"#{job['job_id']} pid={job['pid']} {state_label}{label_part} cwd={job['cwd']}")
                lines.append(f"cmd: {job['command']}")

            tail = self.tail_logs(chat_id, status["latest_job_id"], tail_lines)
            if tail["ok"]:
                lines.append("")
                lines.append(
                    f"latest_job_tail: showing {tail['shown_lines']} of {tail['line_count']} lines for job #{tail['job']['job_id']}"
                )
                lines.append(tail["output"] or "(log is currently empty)")
        return "\n".join(lines)

    def reset(self, chat_id: int, workspace: str | Path | None = None) -> dict:
        target = Path(workspace or self.default_workspace).resolve()
        with self._lock:
            old_state = self._sessions.get(chat_id)
            if old_state is not None:
                with old_state.lock:
                    for job in old_state.jobs.values():
                        if job.process.poll() is None:
                            with suppress(ProcessLookupError):
                                os.killpg(job.process.pid, signal.SIGTERM)
            self._sessions[chat_id] = _ShellState(cwd=target)
        return self.get_status(chat_id)

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for state in sessions:
            with state.lock:
                for job in state.jobs.values():
                    if job.process.poll() is None:
                        with suppress(ProcessLookupError):
                            os.killpg(job.process.pid, signal.SIGTERM)
