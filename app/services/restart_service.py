"""Helpers for restarting the user systemd service hosting RemoteCoder."""

from __future__ import annotations

import os

SERVICE_RESTART_DELAY_SECONDS = 2.0
SERVICE_UNIT_NAME = "remotecoder.service"


def build_user_systemd_env(base_env: dict[str, str] | None = None, *, uid: int | None = None) -> dict[str, str]:
    env = dict(base_env or os.environ)
    user_id = os.getuid() if uid is None else uid
    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{user_id}"
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    return env


def build_restart_command(unit_name: str = SERVICE_UNIT_NAME) -> list[str]:
    return ["systemctl", "--user", "restart", unit_name]
