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

Protected endpoints: `POST /run`, `POST /execute`, `POST /runs`, `GET /runs/{run_id}`, `POST /runs/{run_id}/retry`, `POST /runs/{run_id}/wait`.

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
| `stdout` | string | Agent stdout when available (diagnostic; not verified evidence) |
| `stderr` | string | Agent stderr when available (diagnostic; not verified evidence) |
| `error` | string or null | Failure detail when unsuccessful |
| `return_code` | integer or null | Process exit code when available |
| `commit_sha` | string or null | Commit SHA after successful platform persistence (`persistence.mode` of `commit` or `push`); null when mode is `none` or there were no changes |
| `result` | object or null | Structured objective evidence collected by Mission Control (see below). Null for non-terminal runs that have not stored evidence yet; present on terminal runs when Mission Control recorded evidence |
| `retried_from` | string or null | Source `run_id` when this run was created via `POST /runs/{run_id}/retry`; otherwise null |

#### Trust boundary: `result` vs `stdout` / `stderr`

- **`result`** is objective evidence Mission Control collected from its own execution records and repository state (Git status, process exit codes, declared file-deliverable checks, platform persistence). HAL and automation should prefer `result` for verification.
- **`stdout` / `stderr`** are agent-authored diagnostic text. Do **not** treat natural-language claims in stdout as verified structured evidence.

#### `result` object

| Field | Type | Description |
| --- | --- | --- |
| `files_changed` | string[] | Repository-relative paths changed in the isolated workspace (from Git status). Empty when none or unavailable |
| `commands` | object[] | Commands Mission Control executed (for example the Cursor agent subprocess), each with `argv`, `exit_code`, `passed`, and `kind` |
| `test_counts` | object or null | Aggregate pass/fail/skip counts when reliably available without fragile parsing; otherwise `null` |
| `deliverables` | object or null | Declared file-deliverable verification: `verified`, `passed`, `checked_paths`, `missing` |
| `persistence` | object or null | Platform persistence outcome: `mode`, `attempted`, `ok`, `commit_sha` |
| `warnings` | string[] | Limitations explaining unavailable evidence (never fabricated values) |

Failed and timed-out runs retain any partial evidence Mission Control actually collected.

**Example completed response**

```json
{
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "completed",
  "created_at": "2026-07-23T17:00:00+00:00",
  "started_at": "2026-07-23T17:00:01+00:00",
  "completed_at": "2026-07-23T17:01:30+00:00",
  "elapsed_seconds": 89.0,
  "stdout": "Agent prose summary (diagnostic only)\n",
  "stderr": "",
  "error": null,
  "return_code": 0,
  "commit_sha": "abc123def456",
  "result": {
    "files_changed": [
      "docs/HAL_OPERATOR_LOG.md",
      "mission_control/run_result.py"
    ],
    "commands": [
      {
        "argv": [
          "cursor-agent",
          "--print",
          "--force",
          "--output-format",
          "text",
          "--workspace",
          "/tmp/mission-control-run-xyz",
          "--trust",
          "<instruction>"
        ],
        "exit_code": 0,
        "passed": true,
        "kind": "cursor_agent"
      }
    ],
    "test_counts": null,
    "deliverables": {
      "verified": true,
      "passed": true,
      "checked_paths": ["docs/HAL_OPERATOR_LOG.md"],
      "missing": []
    },
    "persistence": {
      "mode": "commit",
      "attempted": true,
      "ok": true,
      "commit_sha": "abc123def456"
    },
    "warnings": [
      "Aggregate test counts are unavailable; Mission Control does not parse agent stdout for test results.",
      "No separate Mission Control verification shell commands were executed; only the Cursor agent subprocess and platform checks are recorded."
    ]
  },
  "retried_from": null
}
```

**Response** `404 Not Found` only when the `run_id` was never accepted by this process. Completed and failed runs are retained and keep returning `200` with their terminal status and failure details.

### POST /runs/{run_id}/retry

Requires authentication.

Create a new asynchronous run from the exact stored mission YAML of an existing **failed** run. The source run is left unchanged. The new run gets a fresh `run_id`, isolated workspace lifecycle, and durable `retried_from` linkage to the source. Retry routes through the same validation, preflight, and FIFO queue pipeline as `POST /runs`.

No request body.

**Response** `202 Accepted` when the retry is queued

| Field | Type | Description |
| --- | --- | --- |
| `run_id` | string | Opaque identifier for the **new** run |
| `status` | string | Always `queued` on acceptance |

Validation, eligibility, preflight, and recursive-submission failures return `200 OK` with a `RunResponse` body (`ok: false`) instead of queueing a run (same shape as `POST /runs`).

**Response** `404 Not Found` when the source `run_id` is unknown.

**Response** `409 Conflict` when the source run is not eligible for retry:

| Condition | Detail |
| --- | --- |
| Status is `queued`, `running`, `completed`, or `timed_out` | `Only failed runs may be retried (current status: …)` |
| Failed run has no stored mission YAML (legacy row) | `Source run has no stored mission YAML to retry` |

Only terminal status `failed` may be retried. There is no automatic retry policy, mission editing, or retry counter.

### POST /runs/{run_id}/wait

Requires authentication.

Bounded server-side wait for an asynchronous run. Polls the existing run lookup path (`GET /runs/{run_id}` / registry `get_run`) until the run reaches a terminal status or `timeout_seconds` elapses. Returns immediately when the run is already terminal. Does **not** mutate run state when the wait expires (a wait timeout is distinct from run status `timed_out`).

HTTP clients may use this endpoint for a server-side wait. The MCP
`wait_for_run` tool instead polls `GET /runs/{run_id}` on the connector side
(see **MCP tools** below).

**Intended HAL flow**

1. `submit_run` (`POST /runs`) — queue the mission
2. `wait_for_run` (MCP tool, or optionally `POST /runs/{run_id}/wait`) — poll until terminal or wait budget exhausted; for MCP, repeat short waits when `wait_expired` is true
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

The Mission Control MCP connector exposes exactly these run-operation tools:

| Tool | Purpose |
| --- | --- |
| `submit_run` | Submit mission YAML (`POST /runs`) |
| `get_run` | Fetch current run status (`GET /runs/{run_id}`) |
| `wait_for_run` | Short ChatGPT-safe poll of `get_run` until terminal or wait window expires; call repeatedly until terminal |

#### ChatGPT custom MCP app

Use the Streamable HTTP endpoint (not `/sse`):

| Item | Value |
| --- | --- |
| ChatGPT MCP server URL | `https://mission-control-mcp-production.up.railway.app/mcp` |
| Transport | Streamable HTTP (`SERVICE_MODE=mcp` on Railway) |
| Authentication in ChatGPT | **No authentication** |
| Backend API auth | Connector uses server-side `MISSION_CONTROL_API_KEY` as `Authorization: Bearer …` when calling Mission Control; that key is not sent by ChatGPT |

Legacy SSE is also mounted at `https://mission-control-mcp-production.up.railway.app/sse` (with `/messages`) so older `/sse` app URLs keep discovering the same three tools. Prefer `/mcp` for new ChatGPT custom apps.

Local MCP HTTP (same routes as Railway):

```bash
export SERVICE_MODE=mcp
export PORT=8001
export MISSION_CONTROL_URL="https://mission-control-production-76ff.up.railway.app"
export MISSION_CONTROL_API_KEY="<key>"
bash scripts/railway-start.sh
```

#### `wait_for_run`

ChatGPT's MCP tool-call runtime cannot safely hold a single tool call for
long durations (a live wait that spanned ~35s failed at the transport layer
with no usable payload). The MCP tool therefore uses a **short default wait
window** and expects HAL to **call `wait_for_run` repeatedly without user
prompting** until the run is terminal.

| Argument | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `run_id` | string | yes | — | Run identifier returned by `submit_run` |
| `timeout_seconds` | number | no | `20` | Maximum time to wait for this call; must be `>= 0.1`. Values above `25` are capped to `25` (ChatGPT-safe). Zero/negative values are rejected. |
| `poll_interval_seconds` | number | no | `2` | Delay between `get_run` polls; must be `>= 0.05`. Values above `10` are capped to `10`. Zero/negative values are rejected. |

**Intended HAL loop.** `submit_run` → repeat `wait_for_run` until
`wait_expired` is `false` and `status` is terminal → inspect
`stdout` / `stderr` / `error` / `commit_sha`.

**Terminal behavior.** Reuses Mission Control terminal statuses (`completed`,
`failed`, `timed_out`) via `is_terminal_status`. Returns immediately when the
run is already terminal. Payload shape: `{"ok": true, ...}` with the same run
fields as `get_run`, plus `wait_expired: false`, `reached_terminal: true`, and
`timeout_seconds` (effective value after any cap).

**Wait-window expiry.** Uses a monotonic clock and sleeps between polls (no
busy-wait). When the wait window expires while the run is still non-terminal,
the tool returns a **normal usable payload** (not a transport/tool error):

| Field | Value |
| --- | --- |
| `ok` | `true` |
| `run_id` / `status` / other run fields | Latest successful `get_run` payload |
| `wait_expired` | `true` |
| `reached_terminal` | `false` |
| `timeout_seconds` | Effective wait window used for this call |

HAL should treat `wait_expired: true` as “call `wait_for_run` again,” not as
failure. A single transient polling failure does not end the wait while time
remains; `404` (unknown `run_id`) is fatal immediately.

### Platform Git persistence

After a successful agent execution in an isolated workspace, Mission Control
verifies declared **file** deliverables (see below), then applies the
mission's top-level `persistence` block:

| `persistence.mode` | Behavior |
| --- | --- |
| `none` (default when the block is omitted) | Do not stage, commit, or push |
| `commit` | Stage and create a local commit; never push |
| `push` | Stage, commit, and push to `repository.base_branch` (privileged; requires platform-push approval) |

Agent `permissions.commit` and `permissions.push` remain agent permissions only. They do not select platform persistence behavior. Unsupported `persistence.mode` values fail mission validation.

#### Completed-run file deliverable verification

Before platform persistence and before a run is marked `completed`, Mission
Control checks each `deliverables` entry that declares a **file** deliverable.
Each such path must exist as a regular file inside the isolated run
workspace. A missing file fails the run (`status: failed`) with an error of
the form `Missing declared file deliverable: <path>`. Persistence is not
attempted for that run, so a missing deliverable is never recorded as a
successful completed run.

**Recommended (explicit) syntax** for new missions:

```yaml
deliverables:
  - file: docs/out.txt
  - description: API/OpenAPI documentation updates
```

Also accepted: `kind: file` + `path:`, and `kind: descriptive` (with optional
`text:`). Typed `file:` entries are always filesystem-checked when they resolve
safely inside the workspace. Typed `description:` entries are never checked on
disk.

**Bare-string compatibility:** entries with a short alphanumeric file extension
(for example `docs/out.txt`, `MISSION_SPEC.md`) or a `/` separator **without
whitespace** (for example `docs/subdir/file`) are treated as file paths.
Slash-containing descriptive prose with whitespace — notably
`API/OpenAPI documentation updates` — is **not** treated as a file path and
does not fail the gate. Other descriptive deliverables (`summary`, `report`,
`confirmation`, multi-word phrases) are unchanged and are not checked on disk.
Empty `deliverables: []` is unchanged. Absolute paths and paths that would
escape the workspace are not inspected outside the workspace (skipped, not
followed). File *content* is not validated. Unknown mapping shapes are skipped
by the filesystem gate (not silently treated as paths).

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
