from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.codex.claude_code import ClaudeCodeSessionBackend


def _write_fake_claude(tmp_path: Path) -> Path:
    script = tmp_path / "fake_claude.py"
    script.write_text(
        """#!/usr/bin/env python3
import sys

message = sys.argv[-1]
sys.stderr.write("stderr: boot\\n")
sys.stderr.flush()
if message == "fail":
    sys.stderr.write("stderr: failed\\n")
    sys.stderr.flush()
    raise SystemExit(7)
print(f"reply:{message}")
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _write_slow_claude(tmp_path: Path) -> Path:
    script = tmp_path / "slow_claude.py"
    script.write_text(
        """#!/usr/bin/env python3
import sys
import time

time.sleep(5.3)
print("reply:slow")
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_claude_code_backend_returns_reply_and_captures_stderr(tmp_path: Path) -> None:
    backend = ClaudeCodeSessionBackend(claude_bin=str(_write_fake_claude(tmp_path)), claude_args="", timeout_seconds=5)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    session = backend.create_session("session-1", workspace)
    backend.set_session_provider(
        session.session_id,
        {
            "label": "provider-a",
            "model": "demo-model",
            "base_url": "https://example.invalid",
            "api_key": "secret",
        },
    )

    reply = backend.send_message(session.session_id, "hello")
    status = backend.get_status(session.session_id)
    settings_json = Path(status["config_dir"]) / "settings.json"
    onboarding_json = Path(status["config_dir"]) / ".claude.json"

    assert reply == "reply:hello"
    assert status["last_return_code"] == 0
    assert "stderr: boot" in status["stderr_lines"]
    assert status["event_count"] >= 4
    assert any("started:" in item for item in status["recent_events"])
    assert any("spawn:" in item for item in status["recent_events"])
    assert any("stderr:" in item for item in status["recent_events"])
    assert any(item.startswith("stderr: ") for item in status["recent_raw_events"])
    assert settings_json.exists()
    assert onboarding_json.exists()
    assert '"ANTHROPIC_AUTH_TOKEN": "secret"' in settings_json.read_text(encoding="utf-8")
    assert '"ANTHROPIC_MODEL": "demo-model"' in settings_json.read_text(encoding="utf-8")
    assert '"ANTHROPIC_DEFAULT_OPUS_MODEL": "demo-model"' in settings_json.read_text(encoding="utf-8")
    assert '"ANTHROPIC_DEFAULT_SONNET_MODEL": "demo-model"' in settings_json.read_text(encoding="utf-8")
    assert '"ANTHROPIC_DEFAULT_HAIKU_MODEL": "demo-model"' in settings_json.read_text(encoding="utf-8")
    assert '"CLAUDE_CODE_SUBAGENT_MODEL": "demo-model"' in settings_json.read_text(encoding="utf-8")
    assert '"ENABLE_TOOL_SEARCH": "false"' in settings_json.read_text(encoding="utf-8")

    env = backend._build_session_env(backend.sessions[session.session_id])
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "demo-model"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "demo-model"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "demo-model"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "demo-model"
    assert env["ENABLE_TOOL_SEARCH"] == "false"


def test_claude_code_backend_surfaces_nonzero_exit_with_stderr(tmp_path: Path) -> None:
    backend = ClaudeCodeSessionBackend(claude_bin=str(_write_fake_claude(tmp_path)), claude_args="", timeout_seconds=5)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    session = backend.create_session("session-2", workspace)
    backend.set_session_provider(
        session.session_id,
        {
            "label": "provider-a",
            "model": "demo-model",
            "base_url": "https://example.invalid",
            "api_key": "secret",
        },
    )

    try:
        backend.send_message(session.session_id, "fail")
    except RuntimeError as exc:
        text = str(exc)
        assert "Claude Code failed with code 7" in text
        assert "stderr: failed" in text
    else:
        raise AssertionError("expected RuntimeError")


def test_claude_code_backend_includes_thinking_flag(tmp_path: Path) -> None:
    backend = ClaudeCodeSessionBackend(
        claude_bin=str(_write_fake_claude(tmp_path)),
        claude_args="",
        thinking_mode="enabled",
        timeout_seconds=5,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    session = backend.create_session("session-3", workspace)
    backend.set_session_provider(
        session.session_id,
        {
            "label": "provider-a",
            "model": "demo-model",
            "base_url": "https://example.invalid",
            "api_key": "secret",
        },
    )
    state = backend.sessions[session.session_id]
    cmd = backend._build_command(state, "hello")

    assert "--thinking" in cmd
    assert "enabled" in cmd


def test_claude_code_backend_records_progress_heartbeat_for_slow_tasks(tmp_path: Path) -> None:
    backend = ClaudeCodeSessionBackend(claude_bin=str(_write_slow_claude(tmp_path)), claude_args="", timeout_seconds=15)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    session = backend.create_session("session-4", workspace)
    backend.set_session_provider(
        session.session_id,
        {
            "label": "provider-b",
            "model": "demo-model",
            "base_url": "https://example.invalid",
            "api_key": "secret",
        },
    )

    reply = backend.send_message(session.session_id, "slow")
    status = backend.get_status(session.session_id)

    assert reply == "reply:slow"
    assert any("progress: waiting elapsed=" in item for item in status["recent_events"])
