#!/usr/bin/env bash
set -euo pipefail

curl https://cursor.com/install -fsS | bash

CURSOR_LINK="$HOME/.local/bin/cursor-agent"

if [ ! -e "$CURSOR_LINK" ]; then
  echo "Cursor Agent installation failed: $CURSOR_LINK not found"
  find "$HOME/.local" -maxdepth 6 \( -type f -o -type l \) 2>/dev/null || true
  exit 1
fi

CURSOR_EXECUTABLE="$(readlink -f "$CURSOR_LINK")"
CURSOR_RUNTIME_DIR="$(dirname "$CURSOR_EXECUTABLE")"

echo "Cursor executable: $CURSOR_EXECUTABLE"
echo "Cursor runtime directory: $CURSOR_RUNTIME_DIR"

if [ ! -x "$CURSOR_EXECUTABLE" ]; then
  echo "Cursor Agent installation failed: resolved executable is not executable"
  exit 1
fi

mkdir -p /app/.cursor-runtime
cp -a "$CURSOR_RUNTIME_DIR/." /app/.cursor-runtime/

chmod +x /app/.cursor-runtime/cursor-agent

/app/.cursor-runtime/cursor-agent --version