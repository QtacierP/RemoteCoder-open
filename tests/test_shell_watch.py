from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.shell_service import ShellService, _JobState, _PidProcessHandle


def test_watch_logs_filters_training_progress_lines(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ShellService(workspace)
    chat_id = 7
    state = service._get_or_create(chat_id, str(workspace))

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
            cwd=workspace,
            log_path=log_path,
            process=_PidProcessHandle(1234, return_code=None),
            started_at=0.0,
        )
        state.jobs[1] = job

        result = service.watch_logs(chat_id, 1, 10)

    assert result["ok"] is True
    assert result["matched_count"] == 3
    assert "epoch 1/10" in result["output"]
    assert "loss=1.234" in result["output"]
    assert "val_accuracy=0.91" in result["output"]
    assert "booting environment" not in result["output"]


def test_watch_logs_supports_custom_keywords(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ShellService(workspace)
    chat_id = 8
    state = service._get_or_create(chat_id, str(workspace))

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
            cwd=workspace,
            log_path=log_path,
            process=_PidProcessHandle(1235, return_code=None),
            started_at=0.0,
        )
        state.jobs[2] = job

        result = service.watch_logs(chat_id, 2, 10, ["bleu", "rouge"])

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
