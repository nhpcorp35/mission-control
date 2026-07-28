#!/usr/bin/env bash
# HAL Sync Service uninstaller — thin wrapper (no sudo).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/install.sh" uninstall "$@"
