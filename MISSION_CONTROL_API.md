# Mission Control API

Minimal cloud HTTP wrapper around the Mission Control validator and read-only executor.

## Base URL

The service listens on the host and port configured at deploy time. On Railway, the public URL is assigned by the platform.

## Authentication

Protected endpoints require a Mission Control API key as an HTTP Bearer token:

```http
Authorization: Bearer <MISSION_CONTROL_API_KEY>
```

| Item | Value |
| --- | --- |
| Environment variable | `MISSION_CONTROL_API_KEY` |
| Header | `Authorization: Bearer <key>` |
| Missing or invalid credentials | `401 Unauthorized` with `WWW-Authenticate: Bearer` |
| Server key unset / empty | `503 Service Unavailable` |

Protected endpoints: `POST /run`, `POST /execute`, `POST /runs`, `GET /runs/{run_id}`, `POST /runs/{run_id}/wait`.

Public endpoints (no API key): `GET /health`, `POST /validate`.

Do not log, print, or return the API key value. The MCP connector reads the same `MISSION_CONTROL_API_KEY` and sends it on Mission Control API requests.

## Endpoints

### GET /health

Liveness check. No authentication required (Railway health checks).

**Response** `200 OK`

```json
{
  "status": "ok"
}
```

### POST /validate

Validate mission YAML against Mission Specification v1.0. This performs structural validation only; it does not check run eligibility or execute a mission.

**Request body** `application/json`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `mission_yaml` | string | yes | Full mission document as YAML text |

**Example request**

```json
{
  "mission_yaml": "version: 1.0\nmission_id: example\n..."
}
```

**Response** `200 OK`

| Field | Type | Description |
| --- | --- | --- |
| `ok` | boolean | `true` when the mission is structurally valid |
| `error` | string or null | Validation error message when `ok` is `false` |

**Example success**

```json
{
  "ok": true,
  "error": null
}
```

**Example failure**

```json
{
  "ok": false,
  "error": "Missing required keys: permissions"
}
```

### POST /run

Requires authentication.

Validate a mission, confirm it is eligible for Phase 2 read-only execution, then invoke the existing Cursor Agent executor.

Validation order:

1. Structural validation (`load_mission_yaml`)
2. Run-eligibility validation (`validate_mission_for_run`)
3. Read-only execution (`run_cursor_agent`)

**Request body** `application/json`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `mission_yaml` | string | yes | Full mission document as YAML text |

**Response** `200 OK`

| Field | Type | Description |
| --- | --- | --- |
| `ok` | boolean | `true` when execution completed successfully |
| `stdout` | string | Agent stdout on success or partial failure |
| `stderr` | string | Agent stderr when available |
| `error` | string or null | Error message when `ok` is `false` |

**Example success**

```json
{
  "ok": true,
  "stdout": "agent response\n",
  "stderr": "",
  "error": null
}
```

**Example validation failure**

```json
{
  "ok": false,
  "stdout": "",
  "stderr": "",
  "error": "Unsupported version: 2.0 (expected 1.0)"
}
```

**Example execution failure**

```json
{
  "ok": false,
  "stdout": "",
  "stderr": "agent failed",
  "error": "agent failed",
  "error_detail": null
}
```

**Example Cursor CLI preflight failure**

Returned before execution when `cursor-agent` is unavailable or `CURSOR_API_KEY` is not configured.

```json
{
  "ok": false,
  "stdout": "",
  "stderr": "",
  "error": "CURSOR_API_KEY environment variable is not set. Create a key at https://cursor.com/dashboard/api and configure it as a Railway service variable.",
  "error_detail": {
    "code": "CURSOR_API_KEY_MISSING",
    "message": "CURSOR_API_KEY environment variable is not set. Create a key at https://cursor.com/dashboard/api and configure it as a Railway service variable.",
    "stage": "preflight"
  }
}
```

Preflight error codes:

| Code | Meaning |
| --- | --- |
| `CURSOR_AGENT_UNAVAILABLE` | `cursor-agent` is not installed or not on `PATH` |
| `CURSOR_API_KEY_MISSING` | `CURSOR_API_KEY` is unset or empty |
| `PYTHON_UNAVAILABLE` | Python 3 interpreter is not installed or not on `PATH` |

### POST /runs

Requires authentication.

Validate an execute-mode mission and accept it for asynchronous execution in an isolated workspace. Only one Cursor execution is active at a time; additional accepted runs wait in FIFO order. Poll `GET /runs/{run_id}` for lifecycle status.

**Request body** `application/json`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `mission_yaml` | string | yes | Full mission document as YAML text |

**Response** `202 Accepted` when the run is queued

| Field | Type | Description |
| --- | --- | --- |
| `run_id` | string | Opaque run identifier |
| `status` | string | Always `queued` on acceptance |

Validation, eligibility, preflight, and recursive-submission failures return `200 OK` with a `RunResponse` body (`ok: false`) instead of queueing a run.

Recursive local submissions (same-thread re-entrancy during an active execution, or an explicit recursive-submission header) are rejected. Cursor agent subprocesses also do not receive Mission Control API credentials, which prevents nested local `POST /runs` calls from authenticating.

### GET /runs/{run_id}

Requires authentication.

Return lifecycle status and retained output for a previously accepted run.

**Response** `200 OK`

| Field | Type | Description |
| --- | --- | --- |
| `run_id` | string | Run identifier |
| `status` | string | `queued`, `running`, `completed`, `failed`, or `timed_out` |
| `created_at` | string | ISO timestamp |
| `started_at` | string or null | Set when execution begins |
| `completed_at` | string or null | Set when the run reaches a terminal status |
| `elapsed_seconds` | number or null | Duration from start to completion |
| `stdout` | string | Agent stdout when available |
| `stderr` | string | Agent stderr when available |
| `error` | string or null | Failure detail when unsuccessful |
| `return_code` | integer or null | Process exit code when available |
| `commit_sha` | string or null | Commit SHA after successful platform persistence (`persistence.mode` of `commit` or `push`); null when mode is `none` or there were no changes |

**Response** `404 Not Found` only when the `run_id` was never accepted by this process. Completed and failed runs are retained and keep returning `200` with their terminal status and failure details.

### POST /runs/{run_id}/wait

Requires authentication.

Bounded server-side wait for an asynchronous run. Polls the existing run lookup path (`GET /runs/{run_id}` / registry `get_run`) until the run reaches a terminal status or `timeout_seconds` elapses. Returns immediately when the run is already terminal. Does **not** mutate run state when the wait expires (a wait timeout is distinct from run status `timed_out`).

This endpoint backs the MCP `wait_for_run` tool.

**Intended HAL flow**

1. `submit_run` (`POST /runs`) — queue the mission
2. `wait_for_run` (`POST /runs/{run_id}/wait`) — block until terminal or wait budget exhausted
3. Inspect `status`, `stdout` / `stderr` / `error`, and `commit_sha`

**Request body** `application/json` (all fields optional; defaults shown)

| Field | Type | Default | Bounds | Description |
| --- | --- | --- | --- | --- |
| `timeout_seconds` | number | `300` | `0.1` … `3600` | Maximum time to wait for a terminal status |
| `poll_interval_seconds` | number | `1` | `0.05` … `60` | Delay between registry lookups while the run is non-terminal |

Out-of-bounds values return `422 Unprocessable Entity`.

**Response** `200 OK`

Includes the same fields as `GET /runs/{run_id}`, plus:

| Field | Type | Description |
| --- | --- | --- |
| `reached_terminal` | boolean | `true` when the run status is terminal (`completed`, `failed`, or `timed_out`) |
| `wait_expired` | boolean | `true` when the wait budget elapsed while the run was still `queued` or `running` |

| Outcome | `reached_terminal` | `wait_expired` | Run state mutated? |
| --- | --- | --- | --- |
| Already terminal / becomes terminal during wait | `true` | `false` | No (wait only observes) |
| Wait budget exhausted while non-terminal | `false` | `true` | No |

Terminal statuses are defined by a single helper (`is_terminal_status`) covering `completed`, `failed`, and `timed_out`.

**Response** `404 Not Found` when the `run_id` is unknown.

### MCP tools

The Mission Control MCP connector exposes:

| Tool | Purpose |
| --- | --- |
| `submit_run` | Submit mission YAML (`POST /runs`) |
| `get_run` | Fetch current run status (`GET /runs/{run_id}`) |
| `wait_for_run` | Wait for terminal status (`POST /runs/{run_id}/wait`) with `run_id`, `timeout_seconds`, and `poll_interval_seconds` |

### Platform Git persistence

After a successful agent execution in an isolated workspace, Mission Control applies the mission's top-level `persistence` block:

| `persistence.mode` | Behavior |
| --- | --- |
| `none` (default when the block is omitted) | Do not stage, commit, or push |
| `commit` | Stage and create a local commit; never push |
| `push` | Stage, commit, and push to `repository.base_branch` (privileged; requires platform-push approval) |

Agent `permissions.commit` and `permissions.push` remain agent permissions only. They do not select platform persistence behavior. Unsupported `persistence.mode` values fail mission validation.

Push authorization is expressed through `persistence.mode=push` plus `approval.platform_push_approved=true` (or `approval.allow_automatic_platform_push=true`). There is no separate `permissions.push` platform gate; agent `permissions.push` must remain `false` for execute missions.

Execute missions with `persistence.mode=push` and platform-push approval may be **push-only**: they are valid even when `create_files=false` and `modify_files=false`.

#### Platform-push approval

`persistence.mode=push` is a privileged platform action (commit, GitHub push, and possible deployment). It is distinct from agent `permissions.push`.

Before a queued run may perform platform push, Mission Control requires one of:

| Approval field | Meaning |
| --- | --- |
| `approval.platform_push_approved: true` | Explicit per-mission approval for platform push |
| `approval.allow_automatic_platform_push: true` | Named policy authorizing automatic platform pushes |

If neither is set, `POST /runs` rejects the mission during execute eligibility with a machine-readable error whose message begins with `PLATFORM_PUSH_APPROVAL_REQUIRED`. The same check is enforced again inside the persistence layer so a run cannot bypass the gate merely because earlier validation succeeded.

| Mode | Platform-push approval required? |
| --- | --- |
| `none` | No |
| `commit` | No (and commit never pushes) |
| `push` | Yes |

Already authorized pushes keep the existing commit-and-push behavior once approval (or the automatic policy) is present.

### Run state persistence

Asynchronous run records live in a process-local in-memory registry. They are not written to disk, Redis, or any shared store. Restarting the API process discards queued, running, completed, and failed run state. Clients must treat run history as ephemeral to the current process lifetime.

## Safety

The API exposes only mission validation and read-only / execute-mode mission execution. It does not provide shell access, arbitrary filesystem operations, Git commands, or other command endpoints. Nested Mission Control submissions from an active local execution are rejected.

## Local development

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the server:

```bash
export MISSION_CONTROL_API_KEY="local-dev-key"
uvicorn app.api:app --reload
```

Protected routes require `Authorization: Bearer $MISSION_CONTROL_API_KEY`.
Run tests:

```bash
python -m unittest discover -s tests -v
```

## Railway deployment

Mission Control is configured for Railway using Nixpacks. The runner image includes a Python 3 interpreter (via the Nixpacks Python provider and the `python3` apt package) so verification missions can run Python tests. The build also installs Cursor CLI with the official installer, and the start script puts `/app/.venv/bin`, `/app/.cursor-runtime`, and `~/.local/bin` on `PATH` before the service starts.

### Expected runtime

| Component | Location / requirement |
| --- | --- |
| Python 3 | `python3` on `PATH` (system package and/or `/app/.venv/bin/python3`) |
| Cursor CLI | `cursor-agent` on `PATH` (`/app/.cursor-runtime` or `~/.local/bin`) |
| App dependencies | Installed into `/app/.venv` from `requirements.txt` |

Execution preflight fails with `PYTHON_UNAVAILABLE` when no Python 3 interpreter can be resolved before a mission runs.

### Required environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `MISSION_CONTROL_API_KEY` | yes | Shared secret for Mission Control HTTP API authentication (`Authorization: Bearer …`). Required by the API and by the MCP connector. Do not commit this value. |
| `CURSOR_API_KEY` | yes | Cursor user API key from [cursor.com/dashboard/api](https://cursor.com/dashboard/api). Used by `cursor-agent` at runtime. Do not commit this value. |
| `PORT` | yes | Provided automatically by Railway. |

Set `MISSION_CONTROL_API_KEY` and `CURSOR_API_KEY` in the Railway service **Variables** tab. Use secret/reference variables, not hardcoded values in the repo.

### Build and start commands

Railway reads:

- `nixpacks.toml` — enables the Python provider, installs `curl` and `python3`, then runs `scripts/install-cursor-agent.sh`
- `railway.json` — starts the API with `scripts/railway-start.sh`

The install script runs:

```bash
curl -fsS https://cursor.com/install | bash
```

The start script exports `PATH="/app/.venv/bin:/app/.cursor-runtime:$HOME/.local/bin:$PATH"` and launches Uvicorn (or the MCP server).

### Startup logging

On boot, the API logs a Cursor CLI startup check:

```text
Cursor CLI startup check: installed=<true|false> authenticated=<true|false> binary=<path|not found>
```

`authenticated` means `CURSOR_API_KEY` is configured. It does not call Cursor's servers during startup.

### Smoke test on Railway

Use the Railway reference mission, which points at the deployed repo root:

```bash
curl -sS -X POST "$RAILWAY_PUBLIC_URL/run" \
  -H "Authorization: Bearer $MISSION_CONTROL_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @- <<EOF
{
  "mission_yaml": "$(sed 's/"/\\"/g' missions/reference/valid-v1.0-railway.yaml | tr '\n' '\\n')"
}
EOF
```

Or POST the contents of `missions/reference/valid-v1.0-railway.yaml` from your local machine against the deployed `/run` endpoint, including the same `Authorization: Bearer` header.

### Local development with Cursor CLI

Install Cursor CLI locally:

```bash
curl -fsS https://cursor.com/install | bash
export PATH="$HOME/.local/bin:$PATH"
export CURSOR_API_KEY="crsr_..."
```

Then run Uvicorn as usual. The API augments `PATH` at startup so `cursor-agent` resolves from `~/.local/bin` when the official installer was used.
