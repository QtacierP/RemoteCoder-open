#!/usr/bin/env bash
set -eo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT_DIR/logs"
RUNTIME_LOG="$LOG_DIR/autostart.log"

mkdir -p "$LOG_DIR"
touch "$RUNTIME_LOG"

exec >>"$RUNTIME_LOG" 2>&1

echo "[$(date '+%F %T')] starting RemoteCoder launcher"

export ROOT_DIR
export USER="${USER:-$(id -un)}"
export SUDO_USER="${SUDO_USER:-$USER}"

exec /usr/bin/env bash -lc '
set -eo pipefail

set +e
source "$HOME/.bashrc"
BASHRC_STATUS=$?
set -e

if [[ $BASHRC_STATUS -ne 0 ]]; then
  echo "[$(date "+%F %T")] warning: ~/.bashrc returned status $BASHRC_STATUS"
fi

echo "[$(date "+%F %T")] enabling clash proxy"
if ! clash on; then
  echo "[$(date "+%F %T")] warning: clash on returned non-zero, continuing"
fi

echo "[$(date "+%F %T")] activating conda env: coder"
conda activate coder

cd "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.env" ]]; then
  read -r APP_HOST_VALUE APP_PORT_VALUE < <(
    python -c "import os; from dotenv import dotenv_values; values = dotenv_values(os.path.join(os.environ[\"ROOT_DIR\"], \".env\")); print(values.get(\"APP_HOST\", \"0.0.0.0\"), values.get(\"APP_PORT\", \"8000\"))"
  )
else
  APP_HOST_VALUE="0.0.0.0"
  APP_PORT_VALUE="8000"
fi

PYTHON_BIN="$(command -v python)"

echo "[$(date "+%F %T")] using python: $PYTHON_BIN"
echo "[$(date "+%F %T")] binding to ${APP_HOST_VALUE}:${APP_PORT_VALUE}"

exec python -m uvicorn app.main:app --host "$APP_HOST_VALUE" --port "$APP_PORT_VALUE"
'
