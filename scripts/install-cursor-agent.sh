#!/usr/bin/env bash
set -euo pipefail

# Install Cursor CLI using the official installer during Railway/Nixpacks build.
curl -fsS https://cursor.com/install | bash

export PATH="${HOME}/.local/bin:${PATH}"

if ! command -v cursor-agent >/dev/null 2>&1; then
  echo "cursor-agent was not found on PATH after installation" >&2
  exit 1
fi

cursor-agent --version
