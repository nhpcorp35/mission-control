#!/usr/bin/env bash
set -euo pipefail

curl https://cursor.com/install -fsS | bash

CURSOR_AGENT_SOURCE="$HOME/.local/bin/cursor-agent"

if [ ! -x "$CURSOR_AGENT_SOURCE" ]; then
  echo "cursor-agent installation failed: $CURSOR_AGENT_SOURCE not found"
  echo "Installed files:"
  find "$HOME/.local" -maxdepth 3 -type f -o -type l 2>/dev/null || true
  exit 1
fi

mkdir -p /app/.cursor/bin
cp -L "$CURSOR_AGENT_SOURCE" /app/.cursor/bin/cursor-agent
chmod +x /app/.cursor/bin/cursor-agent

/app/.cursor/bin/cursor-agent --version