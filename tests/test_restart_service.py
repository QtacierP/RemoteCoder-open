from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.restart_service import SERVICE_UNIT_NAME, build_restart_command, build_user_systemd_env


def test_build_user_systemd_env_fills_runtime_dir_and_bus() -> None:
    env = build_user_systemd_env({}, uid=1002)

    assert env["XDG_RUNTIME_DIR"] == "/run/user/1002"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1002/bus"


def test_build_user_systemd_env_preserves_existing_bus() -> None:
    env = build_user_systemd_env(
        {
            "XDG_RUNTIME_DIR": "/tmp/runtime",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/runtime/custom-bus",
        },
        uid=1002,
    )

    assert env["XDG_RUNTIME_DIR"] == "/tmp/runtime"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/runtime/custom-bus"


def test_build_restart_command_targets_remotecoder_service() -> None:
    assert build_restart_command() == ["systemctl", "--user", "restart", SERVICE_UNIT_NAME]
