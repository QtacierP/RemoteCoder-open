from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Database
from app.services.shell_service import ShellService


def test_shell_session_and_running_job_survive_service_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subdir = workspace / "subdir"
    subdir.mkdir()

    db = Database(tmp_path / "bridge.db")
    log_root = tmp_path / "job-logs"

    service1 = ShellService(workspace, timeout_seconds=5, log_root=log_root, db=db)
    service1.execute(101, "cd subdir && pwd", workspace)
    job = service1.start_background(101, "sleep 10", workspace, label="long-run")

    service2 = ShellService(workspace, timeout_seconds=5, log_root=log_root, db=db)
    restored = service2.restore_persisted_state()

    assert len(restored) == 1
    status = service2.get_status(101)
    assert status["exists"] is True
    assert status["cwd"] == str(subdir.resolve())
    assert status["latest_job_id"] == job["job_id"]
    assert status["jobs"][0]["running"] is True

    stop_result = service2.stop_job(101, job["job_id"], force=True)
    assert stop_result["ok"] is True


def test_restored_shell_job_can_be_reported_finished(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(tmp_path / "bridge.db")
    log_root = tmp_path / "job-logs"
    log_path = log_root / "job_1.log"
    log_root.mkdir(exist_ok=True)
    log_path.write_text("$ echo restored-finish\nrestored-finish\n", encoding="utf-8")
    db.upsert_shell_session(chat_id=202, cwd=str(workspace), conda_env=None, last_exit_code=0)
    db.upsert_shell_job(
        chat_id=202,
        job_id=1,
        label="short",
        command="echo restored-finish",
        cwd=str(workspace),
        log_path=str(log_path),
        pid=999999,
        started_at=time.time(),
        notified_done=False,
        return_code=None,
    )

    service2 = ShellService(workspace, timeout_seconds=5, log_root=log_root, db=db)
    service2.restore_persisted_state()
    notifications = service2.collect_finished_notifications(20)

    assert len(notifications) == 1
    assert notifications[0]["chat_id"] == 202
    assert notifications[0]["job"]["job_id"] == 1
    assert "restored-finish" in notifications[0]["output"]
