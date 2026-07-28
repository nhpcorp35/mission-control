#!/usr/bin/env bash
# HAL Sync Service — safe fast-forward-only repository auto-sync.
# Never uses sudo, tokens, inbound ports, stash, merge, force-reset, or conflict resolution.
# Does not execute repository content after pulling.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CONFIG="${SCRIPT_DIR}/config.env"
CONFIG_PATH="${HAL_SYNC_CONFIG:-${DEFAULT_CONFIG}}"

HAL_SYNC_LABEL="${HAL_SYNC_LABEL:-com.nhpcorp.hal-sync}"
HAL_SYNC_INTERVAL_SECONDS="${HAL_SYNC_INTERVAL_SECONDS:-60}"
HAL_SYNC_LOG_MAX_BYTES="${HAL_SYNC_LOG_MAX_BYTES:-1048576}"
HAL_SYNC_REPOS="${HAL_SYNC_REPOS:-}"

_LOCK_HELD=0

hal_sync_log() {
  local level="$1"
  shift
  local msg="$*"
  local ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  local line="[${ts}] [${level}] ${msg}"
  if [[ -n "${HAL_SYNC_LOG_FILE:-}" ]]; then
    printf '%s\n' "$line" >>"${HAL_SYNC_LOG_FILE}"
  fi
  printf '%s\n' "$line"
}

hal_sync_rotate_logs_if_needed() {
  local log_file="${1:-}"
  local max_bytes="${HAL_SYNC_LOG_MAX_BYTES:-1048576}"
  [[ -n "$log_file" && -f "$log_file" ]] || return 0
  local size
  size="$(wc -c <"$log_file" | tr -d '[:space:]')"
  if [[ "${size:-0}" -ge "$max_bytes" ]]; then
    mv -f "$log_file" "${log_file}.1"
  fi
}

hal_sync_load_config() {
  if [[ ! -f "$CONFIG_PATH" ]]; then
    printf 'HAL Sync: config not found: %s\n' "$CONFIG_PATH" >&2
    return 1
  fi
  # shellcheck disable=SC1090
  set -a
  # shellcheck source=/dev/null
  source "$CONFIG_PATH"
  set +a

  HAL_SYNC_INTERVAL_SECONDS="${HAL_SYNC_INTERVAL_SECONDS:-60}"
  HAL_SYNC_LOG_MAX_BYTES="${HAL_SYNC_LOG_MAX_BYTES:-1048576}"
  HAL_SYNC_LOG_DIR="${HAL_SYNC_LOG_DIR:-${SCRIPT_DIR}/logs}"
  HAL_SYNC_LOCK_DIR="${HAL_SYNC_LOCK_DIR:-${HAL_SYNC_LOG_DIR}/hal-sync.lock}"
  HAL_SYNC_LOG_FILE="${HAL_SYNC_LOG_FILE:-${HAL_SYNC_LOG_DIR}/hal-sync.log}"

  mkdir -p "${HAL_SYNC_LOG_DIR}"
  hal_sync_rotate_logs_if_needed "${HAL_SYNC_LOG_FILE}"
}

# Parse HAL_SYNC_REPOS into newline-separated absolute paths on stdout.
hal_sync_parse_repos() {
  local raw="${1:-}"
  if [[ -z "${raw//[[:space:]]/}" ]]; then
    return 1
  fi
  # Normalize: allow newlines or spaces as separators; drop empty tokens.
  printf '%s\n' "$raw" | tr ' ' '\n' | while IFS= read -r path || [[ -n "$path" ]]; do
    path="${path#"${path%%[![:space:]]*}"}"
    path="${path%"${path##*[![:space:]]}"}"
    [[ -z "$path" ]] && continue
    printf '%s\n' "$path"
  done
}

hal_sync_acquire_lock() {
  local lock_dir="${HAL_SYNC_LOCK_DIR}"
  if mkdir "$lock_dir" 2>/dev/null; then
    _LOCK_HELD=1
    # Best-effort ownership marker for debugging.
    printf '%s\n' "$$" >"${lock_dir}/pid" 2>/dev/null || true
    return 0
  fi
  hal_sync_log WARN "Skipping run: lock held at ${lock_dir} (overlapping instance)"
  return 1
}

hal_sync_release_lock() {
  if [[ "${_LOCK_HELD}" -eq 1 ]]; then
    rm -f "${HAL_SYNC_LOCK_DIR}/pid" 2>/dev/null || true
    rmdir "${HAL_SYNC_LOCK_DIR}" 2>/dev/null || true
    _LOCK_HELD=0
  fi
}

hal_sync_require_commands() {
  local missing=0
  local cmd
  for cmd in git bash date mkdir mv wc tr; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      printf 'HAL Sync: required command not found: %s\n' "$cmd" >&2
      missing=1
    fi
  done
  return "$missing"
}

# Returns 0 if repo is a usable Git worktree on main with origin configured.
hal_sync_validate_repo_preconditions() {
  local repo="$1"

  if [[ "$repo" != /* ]]; then
    hal_sync_log ERROR "Skip ${repo}: path must be absolute"
    return 1
  fi
  if [[ ! -d "$repo" ]]; then
    hal_sync_log ERROR "Skip ${repo}: directory does not exist"
    return 1
  fi
  if ! git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    hal_sync_log ERROR "Skip ${repo}: not a Git worktree"
    return 1
  fi
  local bare
  bare="$(git -C "$repo" rev-parse --is-bare-repository 2>/dev/null || echo false)"
  if [[ "$bare" == "true" ]]; then
    hal_sync_log ERROR "Skip ${repo}: bare repository is not a worktree"
    return 1
  fi

  local branch
  if ! branch="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null)"; then
    hal_sync_log ERROR "Skip ${repo}: detached HEAD (expected branch main)"
    return 1
  fi
  if [[ "$branch" != "main" ]]; then
    hal_sync_log ERROR "Skip ${repo}: on branch '${branch}' (expected main)"
    return 1
  fi

  if ! git -C "$repo" remote get-url origin >/dev/null 2>&1; then
    hal_sync_log ERROR "Skip ${repo}: remote 'origin' is not configured"
    return 1
  fi
  return 0
}

hal_sync_working_tree_clean() {
  local repo="$1"
  local status
  status="$(git -C "$repo" status --porcelain 2>/dev/null || true)"
  [[ -z "$status" ]]
}

# Fast-forward-only update when origin/main is ahead of local main.
# Never stash, merge, force-reset, or resolve conflicts.
hal_sync_repo() {
  local repo="$1"

  if ! hal_sync_validate_repo_preconditions "$repo"; then
    return 0
  fi

  if ! hal_sync_working_tree_clean "$repo"; then
    hal_sync_log WARN "Skip ${repo}: working tree is not clean"
    return 0
  fi

  if ! git -C "$repo" fetch origin; then
    hal_sync_log ERROR "Fetch failed for ${repo}"
    return 0
  fi

  if ! git -C "$repo" rev-parse --verify origin/main >/dev/null 2>&1; then
    hal_sync_log ERROR "Skip ${repo}: origin/main does not exist after fetch"
    return 0
  fi

  local ahead_count
  ahead_count="$(git -C "$repo" rev-list --count HEAD.."origin/main" 2>/dev/null || echo 0)"
  if [[ "${ahead_count}" -eq 0 ]]; then
    hal_sync_log INFO "Up to date: ${repo}"
    return 0
  fi

  hal_sync_log INFO "origin/main is ${ahead_count} commit(s) ahead; pulling --ff-only: ${repo}"
  if git -C "$repo" pull --ff-only origin main; then
    hal_sync_log INFO "Fast-forward complete: ${repo}"
  else
    # Do not merge, rebase, reset, or resolve — leave tree untouched beyond fetch.
    hal_sync_log ERROR "Fast-forward pull failed for ${repo}; left for manual resolution"
  fi
}

hal_sync_run() {
  hal_sync_require_commands || return 1
  hal_sync_load_config || return 1

  if ! hal_sync_acquire_lock; then
    return 0
  fi
  trap 'hal_sync_release_lock' EXIT

  local repos
  if ! repos="$(hal_sync_parse_repos "${HAL_SYNC_REPOS}")"; then
    hal_sync_log ERROR "HAL_SYNC_REPOS is empty; configure at least one absolute repository path"
    return 1
  fi

  hal_sync_log INFO "HAL Sync starting (config=${CONFIG_PATH})"
  local repo
  while IFS= read -r repo || [[ -n "$repo" ]]; do
    [[ -z "$repo" ]] && continue
    hal_sync_repo "$repo"
  done <<<"$repos"
  hal_sync_log INFO "HAL Sync finished"
}

# Allow tests to source functions without running main.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  hal_sync_run "$@"
fi
