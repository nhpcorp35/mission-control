#!/usr/bin/env bash
set -euo pipefail

export PATH="/app/.venv/bin:/app/.cursor-runtime:$HOME/.local/bin:$PATH"

mkdir -p /app/tmp

exec uvicorn app.api:app \
  --host 0.0.0.0 \
  --port "${PORT:-8080}"