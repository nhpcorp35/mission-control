#!/usr/bin/env bash
set -euo pipefail

export PATH="/app/.venv/bin:/app/.cursor-runtime:$HOME/.local/bin:$PATH"

mkdir -p /app/tmp

case "${SERVICE_MODE:-api}" in
  mcp)
    exec python -m mcp_connector.server
    ;;
  api)
    exec uvicorn app.api:app \
      --host 0.0.0.0 \
      --port "${PORT:-8080}"
    ;;
  *)
    echo "Unknown SERVICE_MODE: ${SERVICE_MODE}" >&2
    exit 1
    ;;
esac
