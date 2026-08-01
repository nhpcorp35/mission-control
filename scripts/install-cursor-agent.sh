#!/usr/bin/env bash
set -euo pipefail

# Official installer lands under $HOME; Railway/Railpack only reliably
# persists /app/.venv from the install step. Package the resolved runtime
# into both /app/.cursor-runtime (Mission Control's expected path) and
# /app/.venv/.cursor-runtime (Railpack-persisted mirror), then expose a
# PATH launcher under /app/.venv/bin when a venv is present.

APP_ROOT="${APP_ROOT:-/app}"
CURSOR_RUNTIME_DEST="${APP_ROOT}/.cursor-runtime"
VENV_DIR="${APP_ROOT}/.venv"
VENV_RUNTIME_DEST="${VENV_DIR}/.cursor-runtime"

if [ "${CURSOR_AGENT_SKIP_DOWNLOAD:-}" != "1" ]; then
  curl https://cursor.com/install -fsS | bash
fi

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

install_runtime_tree() {
  local dest="$1"
  local staging
  staging="$(mktemp -d)"
  # Copy into a fresh staging tree so a live node binary cannot block replace.
  cp -a "$CURSOR_RUNTIME_DIR/." "$staging/"
  # Guarantee the expected launcher name is present as a real file.
  if [ ! -f "$staging/cursor-agent" ] || [ -L "$staging/cursor-agent" ]; then
    cp -L "$CURSOR_LINK" "$staging/cursor-agent"
  fi
  chmod +x "$staging/cursor-agent"
  mkdir -p "$(dirname "$dest")"
  rm -rf "$dest"
  mv "$staging" "$dest"
}

install_runtime_tree "$CURSOR_RUNTIME_DEST"

# Mirror under .venv so Railpack's install-layer filter keeps the agent.
if [ -d "$VENV_DIR" ]; then
  install_runtime_tree "$VENV_RUNTIME_DEST"
  mkdir -p "${VENV_DIR}/bin"
  ln -sfn "../.cursor-runtime/cursor-agent" "${VENV_DIR}/bin/cursor-agent"
fi

if [ ! -x "${CURSOR_RUNTIME_DEST}/cursor-agent" ]; then
  echo "Cursor Agent installation failed: ${CURSOR_RUNTIME_DEST}/cursor-agent missing or not executable"
  exit 1
fi

"${CURSOR_RUNTIME_DEST}/cursor-agent" --version
