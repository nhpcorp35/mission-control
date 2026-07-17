#!/usr/bin/env bash
set -euo pipefail

curl https://cursor.com/install -fsS | bash

CURSOR_AGENT_SOURCE="$(command -v cursor-agent || true)"

if [ -z "$CURSOR_AGENT_SOURCE" ]; then
  echo "cursor-agent installation failed"
  exit 1
fi

mkdir -p /app/.cursor/bin
cp "$CURSOR_AGENT_SOURCE" /app/.cursor/bin/cursor-agent
chmod +x /app/.cursor/bin/cursor-agent

/app/.cursor/bin/cursor-agent --version