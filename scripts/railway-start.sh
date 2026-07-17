#!/usr/bin/env bash
set -euo pipefail

export PATH="/app/.venv/bin:/app/.cursor/bin:$HOME/.local/bin:$PATH"

exec uvicorn app.api:app --host 0.0.0.0 --port "${PORT:-8080}"