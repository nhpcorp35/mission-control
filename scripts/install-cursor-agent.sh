#!/usr/bin/env bash
set -euo pipefail

curl https://cursor.com/install -fsS | bash

CURSOR_BIN_DIR="$HOME/.local/bin"
CURSOR_INSTALL_DIR="$HOME/.cursor"

if [ ! -x "$CURSOR_BIN_DIR/cursor-agent" ]; then
  echo "cursor-agent installation failed"
  find "$HOME/.local" "$HOME/.cursor" -maxdepth 4 \
    \( -type f -o -type l \) 2>/dev/null || true
  exit 1
fi

mkdir -p /app/.cursor-runtime

cp -a "$CURSOR_INSTALL_DIR/." /app/.cursor-runtime/
cp -a "$CURSOR_BIN_DIR/cursor-agent" /app/.cursor-runtime/cursor-agent

chmod +x /app/.cursor-runtime/cursor-agent

PATH="/app/.cursor-runtime:$PATH" \
  /app/.cursor-runtime/cursor-agent --version