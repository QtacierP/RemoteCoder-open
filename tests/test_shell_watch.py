from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.shell_service import ShellService, _JobState, _PidProcessHandle

WORKSPACE = Path("/tmp/project")


class _RunningProc:
    pid = 4567

    def poll(self):
        return None


def test_watch_logs_filters_training_progress_lines() -> None:
    service = ShellService(WORKSPACE)
    chat_id = 7
    session_id = "session-7"
    state = service._get_or_create(session_id, chat_id, WORKSPACE)

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "job.log"
        log_path.write_text(
            "\n".join(
                [
                    "booting environment",
                    "epoch 1/10",
                    "step 10 loss=1.234 lr=1e-4",
                    "random debug line",
                    "val_accuracy=0.91",
                ]
            ),
            encoding="utf-8",
        )
        job = _JobState(
            job_id=1,
            label="train",
            command="python train.py",
            cwd=WORKSPACE,
            log_path=log_path,
            process=_PidProcessHandle(1234, return_code=None),
            started_at=0.0,
        )
        state.jobs[1] = job

        result = service.watch_logs(session_id, 1, 10)

    assert result["ok"] is True
    assert result["matched_count"] == 3
    assert "epoch 1/10" in result["output"]
    assert "loss=1.234" in result["output"]
    assert "val_accuracy=0.91" in result["output"]
    assert "booting environment" not in result["output"]


def test_watch_logs_supports_custom_keywords() -> None:
    service = ShellService(WORKSPACE)
    chat_id = 8
    session_id = "session-8"
    state = service._get_or_create(session_id, chat_id, WORKSPACE)

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "job.log"
        log_path.write_text(
            "\n".join(
                [
                    "booting environment",
                    "bleu=31.4",
                    "rougeL=0.44",
                    "loss=0.98",
                ]
            ),
            encoding="utf-8",
        )
        job = _JobState(
            job_id=2,
            label="eval",
            command="python eval.py",
            cwd=WORKSPACE,
            log_path=log_path,
            process=_PidProcessHandle(1235, return_code=None),
            started_at=0.0,
        )
        state.jobs[2] = job

        result = service.watch_logs(session_id, 2, 10, ["bleu", "rouge"])

    assert result["ok"] is True
    assert result["matched_count"] == 2
    assert result["keywords"] == ["bleu", "rouge"]
    assert "bleu=31.4" in result["output"]
    assert "rougeL=0.44" in result["output"]
    assert "loss=0.98" not in result["output"]


def test_build_shell_script_supports_unbuffered_mode() -> None:
    script = ShellService._build_shell_script("python train.py", force_unbuffered=True)

    assert "export PYTHONUNBUFFERED=1" in script
    assert "export PYTHONIOENCODING=UTF-8" in script
    assert "python train.py" in script


def test_build_background_command_prefers_stdbuf(monkeypatch) -> None:
    monkeypatch.setattr("app.services.shell_service.shutil.which", lambda name: "/usr/bin/stdbuf" if name == "stdbuf" else None)

    command = ShellService._build_background_command("echo hi")

    assert command == ["stdbuf", "-oL", "-eL", "bash", "-lc", "echo hi"]


def test_delete_job_removes_job_and_log(tmp_path: Path) -> None:
    service = ShellService(tmp_path, log_root=tmp_path)
    chat_id = 9
    session_id = "session-9"
    state = service._get_or_create(session_id, chat_id, tmp_path)

    log_path = tmp_path / "job-delete.log"
    log_path.write_text("done\n", encoding="utf-8")
    job = _JobState(
        job_id=1,
        label="done",
        command="echo done",
        cwd=tmp_path,
        log_path=log_path,
        process=_PidProcessHandle(1236, return_code=0),
        started_at=0.0,
    )
    state.jobs[1] = job

    result = service.delete_job(session_id, 1)

    assert result["ok"] is True
    assert service.get_job(session_id, 1) is None
    assert not log_path.exists()


def test_clear_jobs_removes_all_jobs(tmp_path: Path) -> None:
    service = ShellService(tmp_path, log_root=tmp_path)
    chat_id = 10
    session_id = "session-10"
    state = service._get_or_create(session_id, chat_id, tmp_path)

    for job_id in (1, 2):
        log_path = tmp_path / f"job-{job_id}.log"
        log_path.write_text("done\n", encoding="utf-8")
        state.jobs[job_id] = _JobState(
            job_id=job_id,
            label=f"job-{job_id}",
            command="echo done",
            cwd=tmp_path,
            log_path=log_path,
            process=_PidProcessHandle(2000 + job_id, return_code=0),
            started_at=0.0,
        )

    result = service.clear_jobs(session_id)

    assert result["ok"] is True
    assert len(result["deleted"]) == 2
    assert service.list_jobs(session_id) == []


def test_clear_jobs_resets_next_job_id(tmp_path: Path) -> None:
    service = ShellService(tmp_path, log_root=tmp_path)
    chat_id = 11
    session_id = "session-11"
    state = service._get_or_create(session_id, chat_id, tmp_path)
    state.next_job_id = 4

    for job_id in (1, 2, 3):
        log_path = tmp_path / f"job-reset-{job_id}.log"
        log_path.write_text("done\n", encoding="utf-8")
        state.jobs[job_id] = _JobState(
            job_id=job_id,
            label=f"job-{job_id}",
            command="echo done",
            cwd=tmp_path,
            log_path=log_path,
            process=_PidProcessHandle(3000 + job_id, return_code=0),
            started_at=0.0,
        )

    result = service.clear_jobs(session_id)

    assert result["ok"] is True
    assert state.next_job_id == 1


def test_sync_workspace_keeps_cwd_when_background_job_is_active(tmp_path: Path) -> None:
    service = ShellService(tmp_path, log_root=tmp_path)
    chat_id = 12
    session_id = "session-12"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    state = service._get_or_create(session_id, chat_id, first)
    log_path = tmp_path / "job-live.log"
    log_path.write_text("running\n", encoding="utf-8")
    state.jobs[1] = _JobState(
        job_id=1,
        label="live",
        command="sleep 100",
        cwd=first,
        log_path=log_path,
        process=_RunningProc(),
        started_at=0.0,
    )

    result = service.sync_workspace(session_id, chat_id, second)

    assert result["workspace_applied"] is False
    assert result["cwd"] == str(first.resolve())


def test_sync_workspace_updates_cwd_when_no_background_job_is_active(tmp_path: Path) -> None:
    service = ShellService(tmp_path, log_root=tmp_path)
    chat_id = 13
    session_id = "session-13"
    first = tmp_path / "first-idle"
    second = tmp_path / "second-idle"
    first.mkdir()
    second.mkdir()

    service._get_or_create(session_id, chat_id, first)
    result = service.sync_workspace(session_id, chat_id, second)

    assert result["workspace_applied"] is True
    assert result["cwd"] == str(second.resolve())


def test_background_jobs_are_isolated_per_session(tmp_path: Path) -> None:
    service = ShellService(tmp_path, log_root=tmp_path)
    chat_id = 14
    session_a = "session-a"
    session_b = "session-b"

    state_a = service._get_or_create(session_a, chat_id, tmp_path)
    state_b = service._get_or_create(session_b, chat_id, tmp_path)

    log_a = tmp_path / "job-a.log"
    log_b = tmp_path / "job-b.log"
    log_a.write_text("job-a\n", encoding="utf-8")
    log_b.write_text("job-b\n", encoding="utf-8")

    state_a.jobs[1] = _JobState(
        job_id=1,
        label="a",
        command="echo a",
        cwd=tmp_path,
        log_path=log_a,
        process=_PidProcessHandle(4101, return_code=0),
        started_at=0.0,
    )
    state_b.jobs[1] = _JobState(
        job_id=1,
        label="b",
        command="echo b",
        cwd=tmp_path,
        log_path=log_b,
        process=_PidProcessHandle(4102, return_code=0),
        started_at=0.0,
    )

    jobs_a = service.list_jobs(session_a)
    jobs_b = service.list_jobs(session_b)
    log_view_a = service.tail_logs(session_a, 1, 20)
    log_view_b = service.tail_logs(session_b, 1, 20)

    assert len(jobs_a) == 1
    assert len(jobs_b) == 1
    assert jobs_a[0]["label"] == "a"
    assert jobs_b[0]["label"] == "b"
    assert "job-a" in log_view_a["output"]
    assert "job-b" in log_view_b["output"]


def test_list_all_jobs_includes_chat_and_session_context(tmp_path: Path) -> None:
    service = ShellService(tmp_path, log_root=tmp_path)

    state_a = service._get_or_create("session-a", 21, tmp_path)
    state_b = service._get_or_create("session-b", 22, tmp_path)

    log_a = tmp_path / "all-a.log"
    log_b = tmp_path / "all-b.log"
    log_a.write_text("a\n", encoding="utf-8")
    log_b.write_text("b\n", encoding="utf-8")

    state_a.jobs[1] = _JobState(
        job_id=1,
        label="job-a",
        command="echo a",
        cwd=tmp_path,
        log_path=log_a,
        process=_PidProcessHandle(5101, return_code=0),
        started_at=0.0,
    )
    state_b.jobs[2] = _JobState(
        job_id=2,
        label="job-b",
        command="echo b",
        cwd=tmp_path,
        log_path=log_b,
        process=_PidProcessHandle(5102, return_code=0),
        started_at=0.0,
    )

    jobs = service.list_all_jobs()

    assert len(jobs) == 2
    assert {job["chat_id"] for job in jobs} == {21, 22}
    assert {job["session_id"] for job in jobs} == {"session-a", "session-b"}
