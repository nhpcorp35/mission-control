#!/usr/bin/env bash
# HAL Sync Service installer — user-level launchd agent (no sudo).
# Workflows: install | status | restart | uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.nhpcorp.hal-sync"
PLIST_NAME="${LABEL}.plist"
PLIST_TEMPLATE="${SCRIPT_DIR}/launchd/${PLIST_NAME}.template"
CONFIG_EXAMPLE="${SCRIPT_DIR}/config.env.example"
CONFIG_PATH="${HAL_SYNC_CONFIG:-${SCRIPT_DIR}/config.env}"
LOG_DIR="${SCRIPT_DIR}/logs"
SYNC_SCRIPT="${SCRIPT_DIR}/sync.sh"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
INSTALLED_PLIST="${LAUNCH_AGENTS_DIR}/${PLIST_NAME}"

die() {
  printf 'HAL Sync install: %s\n' "$*" >&2
  exit 1
}

require_no_sudo() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    die "Refusing to run as root. Run as your normal macOS user (no sudo)."
  fi
}

uid_domain() {
  printf 'gui/%s' "$(id -u)"
}

load_config_for_install() {
  if [[ ! -f "$CONFIG_PATH" ]]; then
    return 1
  fi
  # shellcheck disable=SC1090
  set -a
  # shellcheck source=/dev/null
  source "$CONFIG_PATH"
  set +a
  HAL_SYNC_INTERVAL_SECONDS="${HAL_SYNC_INTERVAL_SECONDS:-60}"
  HAL_SYNC_REPOS="${HAL_SYNC_REPOS:-}"
}

ensure_config() {
  if [[ -f "$CONFIG_PATH" ]]; then
    printf 'Using existing config: %s\n' "$CONFIG_PATH"
    return 0
  fi
  [[ -f "$CONFIG_EXAMPLE" ]] || die "Missing example config: ${CONFIG_EXAMPLE}"
  cp "$CONFIG_EXAMPLE" "$CONFIG_PATH"
  printf 'Created config from example: %s\n' "$CONFIG_PATH"
  printf 'Edit HAL_SYNC_REPOS before relying on sync.\n'
}

validate_commands() {
  local cmd
  for cmd in git bash launchctl sed date mkdir; do
    command -v "$cmd" >/dev/null 2>&1 || die "Required command not found: ${cmd}"
  done
  [[ -x "$SYNC_SCRIPT" || -f "$SYNC_SCRIPT" ]] || die "Missing sync script: ${SYNC_SCRIPT}"
  [[ -f "$PLIST_TEMPLATE" ]] || die "Missing plist template: ${PLIST_TEMPLATE}"
}

# shellcheck source=sync.sh
validate_repos() {
  # Source parse helpers from sync.sh without running main.
  # shellcheck disable=SC1091
  source "$SYNC_SCRIPT"
  local repos repo
  if ! repos="$(hal_sync_parse_repos "${HAL_SYNC_REPOS:-}")"; then
    die "HAL_SYNC_REPOS is empty in ${CONFIG_PATH}"
  fi
  while IFS= read -r repo || [[ -n "$repo" ]]; do
    [[ -z "$repo" ]] && continue
    [[ "$repo" == /* ]] || die "Repository path must be absolute: ${repo}"
    [[ -d "$repo" ]] || die "Repository path does not exist: ${repo}"
    git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
      || die "Not a Git worktree: ${repo}"
    git -C "$repo" remote get-url origin >/dev/null 2>&1 \
      || die "Remote 'origin' not configured: ${repo}"
    local branch
    branch="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null || true)"
    [[ "$branch" == "main" ]] || die "Expected branch main (found '${branch:-detached}'): ${repo}"
    printf 'Validated repository: %s (main, origin ok)\n' "$repo"
  done <<<"$repos"
}

render_plist() {
  local interval="${HAL_SYNC_INTERVAL_SECONDS:-60}"
  local stdout_log="${LOG_DIR}/launchd.stdout.log"
  local stderr_log="${LOG_DIR}/launchd.stderr.log"
  mkdir -p "$LOG_DIR" "$LAUNCH_AGENTS_DIR"

  sed \
    -e "s|__HAL_SYNC_LABEL__|${LABEL}|g" \
    -e "s|__HAL_SYNC_SCRIPT__|${SYNC_SCRIPT}|g" \
    -e "s|__HAL_SYNC_WORKING_DIRECTORY__|${SCRIPT_DIR}|g" \
    -e "s|__HAL_SYNC_CONFIG__|${CONFIG_PATH}|g" \
    -e "s|__HAL_SYNC_INTERVAL_SECONDS__|${interval}|g" \
    -e "s|__HAL_SYNC_STDOUT_LOG__|${stdout_log}|g" \
    -e "s|__HAL_SYNC_STDERR_LOG__|${stderr_log}|g" \
    "$PLIST_TEMPLATE" >"${INSTALLED_PLIST}.tmp"
  mv -f "${INSTALLED_PLIST}.tmp" "$INSTALLED_PLIST"
  printf 'Installed plist: %s\n' "$INSTALLED_PLIST"
}

launchctl_bootout() {
  local domain
  domain="$(uid_domain)"
  if launchctl bootout "${domain}/${LABEL}" 2>/dev/null; then
    return 0
  fi
  # Older macOS fallback
  if [[ -f "$INSTALLED_PLIST" ]]; then
    launchctl unload "$INSTALLED_PLIST" 2>/dev/null || true
  fi
}

launchctl_bootstrap() {
  local domain
  domain="$(uid_domain)"
  if launchctl bootstrap "$domain" "$INSTALLED_PLIST" 2>/dev/null; then
    launchctl enable "${domain}/${LABEL}" 2>/dev/null || true
    return 0
  fi
  # Older macOS fallback
  launchctl load "$INSTALLED_PLIST"
}

launchctl_kickstart() {
  local domain
  domain="$(uid_domain)"
  if launchctl kickstart -k "${domain}/${LABEL}" 2>/dev/null; then
    return 0
  fi
  # Best-effort older fallback: unload/load already done by restart callers.
  return 0
}

cmd_install() {
  require_no_sudo
  validate_commands
  ensure_config
  load_config_for_install || die "Failed to load config: ${CONFIG_PATH}"
  validate_repos
  chmod +x "$SYNC_SCRIPT" "${SCRIPT_DIR}/install.sh" "${SCRIPT_DIR}/uninstall.sh" 2>/dev/null || true
  render_plist
  launchctl_bootout || true
  launchctl_bootstrap
  printf 'HAL Sync launch agent installed and loaded (%s).\n' "$LABEL"
  printf 'Interval: %ss. Logs: %s\n' "${HAL_SYNC_INTERVAL_SECONDS:-60}" "$LOG_DIR"
}

cmd_status() {
  require_no_sudo
  printf 'Label: %s\n' "$LABEL"
  printf 'Plist: %s\n' "$INSTALLED_PLIST"
  if [[ -f "$INSTALLED_PLIST" ]]; then
    printf 'Plist present: yes\n'
  else
    printf 'Plist present: no\n'
  fi
  if [[ -f "$CONFIG_PATH" ]]; then
    printf 'Config: %s\n' "$CONFIG_PATH"
  else
    printf 'Config: missing (%s)\n' "$CONFIG_PATH"
  fi
  local domain
  domain="$(uid_domain)"
  if launchctl print "${domain}/${LABEL}" 2>/dev/null; then
    return 0
  fi
  # Older fallback
  if launchctl list 2>/dev/null | grep -F "$LABEL"; then
    return 0
  fi
  printf 'launchctl: agent not loaded (or not queryable on this OS).\n'
  return 0
}

cmd_restart() {
  require_no_sudo
  [[ -f "$INSTALLED_PLIST" ]] || die "Plist not installed. Run: ${SCRIPT_DIR}/install.sh install"
  validate_commands
  load_config_for_install || die "Failed to load config: ${CONFIG_PATH}"
  validate_repos
  render_plist
  launchctl_bootout || true
  launchctl_bootstrap
  launchctl_kickstart
  printf 'HAL Sync launch agent restarted (%s).\n' "$LABEL"
}

cmd_uninstall() {
  require_no_sudo
  launchctl_bootout || true
  if [[ -f "$INSTALLED_PLIST" ]]; then
    rm -f "$INSTALLED_PLIST"
    printf 'Removed plist: %s\n' "$INSTALLED_PLIST"
  else
    printf 'Plist already absent: %s\n' "$INSTALLED_PLIST"
  fi
  printf 'Config and logs left in place under %s\n' "$SCRIPT_DIR"
  printf 'HAL Sync launch agent uninstalled (%s).\n' "$LABEL"
}

usage() {
  cat <<EOF
Usage: $(basename "$0") [install|status|restart|uninstall]

  install     Create config (if missing), validate repos, install & load LaunchAgent
  status      Show plist/config and launchctl state
  restart     Re-render plist and reload the agent
  uninstall   Unload and remove the LaunchAgent plist

No sudo. No GitHub tokens. User LaunchAgent only (~/Library/LaunchAgents).
EOF
}

main() {
  local action="${1:-install}"
  case "$action" in
    install) cmd_install ;;
    status) cmd_status ;;
    restart) cmd_restart ;;
    uninstall) cmd_uninstall ;;
    -h|--help|help) usage ;;
    *)
      usage >&2
      die "Unknown action: ${action}"
      ;;
  esac
}

main "$@"
