#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="remotecoder.service"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SOURCE_SERVICE="$ROOT_DIR/deploy/systemd/$SERVICE_NAME"
TARGET_SERVICE="$SYSTEMD_DIR/$SERVICE_NAME"

mkdir -p "$SYSTEMD_DIR"
sed "s|__ROOT_DIR__|$ROOT_DIR|g" "$SOURCE_SERVICE" >"$TARGET_SERVICE"
chmod 0644 "$TARGET_SERVICE"

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"

echo "Installed $TARGET_SERVICE"
echo "Current status:"
systemctl --user --no-pager --full status "$SERVICE_NAME" || true
