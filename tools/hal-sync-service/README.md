# HAL Sync Service

User-level macOS LaunchAgent that periodically fast-forward-syncs explicitly
configured local Git repositories from `origin/main`.

Safe by design: no sudo, no GitHub tokens, no inbound ports, no stash/merge/
force-reset/conflict resolution, and no execution of repository content after
pull.

## Layout

| Path | Role |
|------|------|
| `sync.sh` | Sync worker (lock, validate, fetch, `--ff-only` pull) |
| `install.sh` | `install` / `status` / `restart` / `uninstall` |
| `uninstall.sh` | Wrapper → `install.sh uninstall` |
| `config.env.example` | Example config (copied to `config.env` on first install) |
| `launchd/com.nhpcorp.hal-sync.plist.template` | launchd plist template |
| `logs/` | Local logs + lock (gitignored) |

## Requirements

- macOS with `launchctl` and Git on `PATH`
- One or more local clones on branch `main` with remote `origin`
- Network access outbound to the remotes already configured on those clones
  (SSH keys or existing credential helpers — nothing is embedded here)

## Configuration

`config.env` (created from the example when missing):

```bash
HAL_SYNC_REPOS="/Users/allenk/Desktop/Mission-Control"
HAL_SYNC_INTERVAL_SECONDS=60
HAL_SYNC_LOG_MAX_BYTES=1048576
```

- `HAL_SYNC_REPOS`: absolute path(s), separated by newlines or spaces.
- Default interval: **60 seconds**.
- Working trees that are dirty are skipped (logged); only clean trees are updated.
- Updates use `git fetch origin` then `git pull --ff-only origin main` only when
  `origin/main` is ahead.

## Commands

Run as your normal macOS user — **never with sudo**.

```bash
cd /path/to/Mission-Control/tools/hal-sync-service

# First-time install: create config.env if needed, validate repos, load LaunchAgent
./install.sh install

# Inspect plist / launchctl state
./install.sh status

# Re-render plist and reload
./install.sh restart

# Unload and remove LaunchAgent plist (config/logs kept)
./install.sh uninstall
# or
./uninstall.sh
```

Manual one-shot sync (same safety rules):

```bash
./sync.sh
```

## launchctl notes

On current macOS, install uses `launchctl bootstrap` / `bootout` /
`kickstart` under the `gui/$(id -u)` domain. If those fail, it falls back to
`launchctl load` / `unload` for older systems.

Plist destination: `~/Library/LaunchAgents/com.nhpcorp.hal-sync.plist`.

## Safety guarantees

- Overlapping runs are blocked with a directory lock under `logs/`.
- Logs are rotated when they exceed `HAL_SYNC_LOG_MAX_BYTES` (keeps `.1`).
- Never opens inbound network ports.
- Never requests or embeds GitHub tokens.
- Never auto-stash, auto-merge, force-reset, deletes files, or resolves conflicts.
- Does not execute scripts or binaries from the repository after a pull.

## Verification on Allen's Mac

This service must be installed and verified on macOS. A Linux CI / Mission Control
runner can syntax-check scripts and run local Git fixture tests, but **cannot**
load LaunchAgents. On the Mac:

1. Confirm `HAL_SYNC_REPOS` points at the real clone(s).
2. `./install.sh install`
3. `./install.sh status` — agent loaded.
4. Inspect `logs/hal-sync.log` after one interval.
5. `./install.sh uninstall` when removing.

## Tests

Focused automated tests live in `tests/test_hal_sync_service.py` (local Git
fixtures only; no GitHub network).
