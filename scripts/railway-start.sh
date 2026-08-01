#!/usr/bin/env bash
set -euo pipefail

export PATH="/app/.venv/bin:/app/.cursor-runtime:$HOME/.local/bin:$PATH"

mkdir -p /app/tmp

# Cursor agent shells receive a sanitized PATH (/usr/local/bin, /usr/bin)
# without /app/.venv/bin. Symlinks into /usr/local/bin break venv sys.prefix
# (Python resolves the base interpreter). Install tiny exec wrappers instead
# so agents keep /app/.venv packages (e.g. pypdf) when running python3.
VENV_BIN="${MC_VENV_BIN:-/app/.venv/bin}"
LOCAL_BIN="${MC_LOCAL_BIN:-/usr/local/bin}"
if [ -d "${VENV_BIN}" ]; then
  # Absolute VENV_BIN so wrappers exec a stable path and never LOCAL_BIN.
  VENV_BIN="$(cd "${VENV_BIN}" && pwd)"
  mkdir -p "${LOCAL_BIN}"
  for exe in python3 python pip3 pip; do
    target="${VENV_BIN}/${exe}"
    # Only wrap real venv executables; never point at LOCAL_BIN (no recursion).
    if [ -x "${target}" ] && [ ! -d "${target}" ]; then
      dest="${LOCAL_BIN}/${exe}"
      tmp="${dest}.tmp.$$"
      # shellcheck disable=SC2016
      printf '#!/bin/sh\nexec %s "$@"\n' "'${target}'" >"${tmp}"
      chmod a+x "${tmp}"
      mv -f "${tmp}" "${dest}"
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
