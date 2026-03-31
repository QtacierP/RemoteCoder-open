"""Structured logging helpers."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# ANSI colour codes for terminal readability
_COLORS = {
    "DEBUG": "\033[36m",     # cyan
    "INFO": "\033[32m",      # green
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[1;31m",  # bold red
}
_RESET = "\033[0m"


def configure_logging(log_dir: Path, debug: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bridge.log"

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers = []

    # Terminal: human-readable coloured output in debug mode, JSON otherwise
    stream_handler = logging.StreamHandler()
    if debug:
        stream_handler.setFormatter(DebugTerminalFormatter())
    else:
        stream_handler.setFormatter(JsonLogFormatter())

    # File: always JSON for machine parsing
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(JsonLogFormatter())

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)


class DebugTerminalFormatter(logging.Formatter):
    """Human-readable coloured formatter for terminal debug output."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        color = _COLORS.get(record.levelname, "")
        level = record.levelname[0]  # D/I/W/E/C
        name = record.name.rsplit(".", 1)[-1]  # short module name
        msg = record.getMessage()
        extras = ""
        for key in ("chat_id", "session_id", "mode", "workspace", "event", "step"):
            value = getattr(record, key, None)
            if value is not None:
                extras += f" {key}={value}"
        return f"{color}[{ts}] {level} {name}{_RESET}:{extras} {msg}"


class JsonLogFormatter(logging.Formatter):
    """Emit JSON logs for easier audit ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("chat_id", "session_id", "mode", "workspace", "event", "step"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload)
