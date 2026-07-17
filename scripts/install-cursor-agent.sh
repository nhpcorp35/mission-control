#!/usr/bin/env bash
set -euo pipefail

curl https://cursor.com/install -fsS | bash

CURSOR_INSTALL_DIR="$HOME/.cursor/bin"

if [ ! -x "$CURSOR_INSTALL_DIR/cursor-agent" ]; then
  echo "Cursor Agent installation failed: executable not found"
  echo "Installed files:"
  find "$HOME/.cursor" "$HOME/.local" -maxdepth 4 \
    \( -type f -o -type l \) 2>/dev/null || true
  exit 1
fi

mkdir -p /app/.cursor-runtime
cp -a "$CURSOR_INSTALL_DIR/." /app/.cursor-runtime/

chmod +x /app/.cursor-runtime/cursor-agent

/app/.cursor-runtime/cursor-agent --version