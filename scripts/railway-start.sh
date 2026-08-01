#!/usr/bin/env bash
set -euo pipefail

export PATH="/app/.venv/bin:/app/.cursor-runtime:$HOME/.local/bin:$PATH"

mkdir -p /app/tmp

# Cursor agent shells receive a sanitized PATH (/usr/local/bin, /usr/bin)
# without /app/.venv/bin. Symlink venv Python/pip into /usr/local/bin when
# present so agents can run python3 and import packages from the venv.
VENV_BIN="${MC_VENV_BIN:-/app/.venv/bin}"
LOCAL_BIN="${MC_LOCAL_BIN:-/usr/local/bin}"
if [ -d "${VENV_BIN}" ]; then
  mkdir -p "${LOCAL_BIN}"
  for exe in python3 python pip3 pip; do
    target="${VENV_BIN}/${exe}"
    if [ -x "${target}" ]; then
      ln -sfn "${target}" "${LOCAL_BIN}/${exe}"
    fi
  done
fi

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
