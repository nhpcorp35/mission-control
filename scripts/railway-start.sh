#!/usr/bin/env bash
set -euo pipefail

export PATH="/app/.venv/bin:/app/.cursor-runtime:$HOME/.local/bin:$PATH"

mkdir -p /app/tmp

echo "SERVICE_MODE=${SERVICE_MODE:-<unset>}"

case "${SERVICE_MODE:-api}" in
  mcp)
    echo "Starting MCP server"
    exec python -m mcp_connector.server
    ;;
  api)
    echo "Starting API server"
    exec uvicorn app.api:app \
      --host 0.0.0.0 \
      --port "${PORT:-8080}"
    ;;
  *)
    echo "Unknown SERVICE_MODE: ${SERVICE_MODE}" >&2
    exit 1
    ;;
esac
