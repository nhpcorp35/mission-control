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

Protected endpoints: `POST /run`, `POST /execute`, `POST /runs`, `POST /runs/structured`, `POST /runs/submit-and-wait`, `POST /repository-commands`, `GET /runs/{run_id}`, `POST /runs/{run_id}/retry`, `POST /runs/{run_id}/wait`.

Public endpoints (no API key): `GET /health`, `POST /validate`, `GET /openapi.json`, `GET /openapi-actions.json`.

Do not log, print, or return the API key value. The MCP connector reads the same `MISSION_CONTROL_API_KEY` and sends it on Mission Control API requests.

## Custom GPT Actions OpenAPI import

Allen should import **this** schema URL into the Custom GPT Actions editor (not `/openapi.json`):

```text
https://mission-control-production-76ff.up.railway.app/openapi-actions.json
```

| Item | Value |
| --- | --- |
| Actions import URL | `https://mission-control-production-76ff.up.railway.app/openapi-actions.json` |
| Standard OpenAPI (clients / Swagger) | `https://mission-control-production-76ff.up.railway.app/openapi.json` |
| Auth | HTTP Bearer (`MISSION_CONTROL_API_KEY`) |
| Core operation IDs | `submit_run`, `get_run`, `wait_for_run`, `submit_and_wait` |

`/openapi.json` remains OpenAPI 3.1 for normal clients. `/openapi-actions.json` is a documentation-only OpenAPI **3.1.0** view for Custom GPT Actions. It keeps the importer-required `openapi` version (`3.1.0` / `3.1.1` only — `3.0.x` fails with `('openapi',): Input should be '3.1.1' or '3.1.0'`) and strips other importer-rejected constructs (nullable `anyOf`/`null`, `$ref`+`oneOf` siblings, empty `items`, unconstrained schemas, operation descriptions ≥ 300 characters, and the inline `/health` map schema). Runtime API behavior is unchanged.

### Import verification (Custom GPT Actions)

1. In ChatGPT, open the Custom GPT → **Actions** → **Import from URL**.
2. Paste: `https://mission-control-production-76ff.up.railway.app/openapi-actions.json`
3. Confirm import succeeds (no `('openapi',): Input should be '3.1.1' or '3.1.0'`, no `servers` parse error, no description-length or `/health` schema rejection). Document `openapi` must be `3.1.0` or `3.1.1`.
4. Confirm authentication is **API Key → Bearer**, and that discoverable operations include `submit_run`, `get_run`, `wait_for_run`, and `submit_and_wait`.
5. Optionally hit `GET /health` via the imported action and expect `{"status":"ok"}`.

Do **not** import `/openapi.json` into Actions; that document remains OpenAPI 3.1 for normal clients.

## Endpoints

### GET /health

Liveness check. No authentication required (Railway health checks).

**Response** `200 OK`

```json
{
  "status": "ok"
}
```

### GET /openapi-actions.json

Custom GPT Actions–compatible OpenAPI document. No authentication required.

Import this URL in the ChatGPT Custom GPT editor under **Actions → Import from URL**.

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

### POST /runs/structured

Requires authentication.

Accept structured Mission Spec fields, render Mission Spec v1.0 YAML through the mission builder (safe execute defaults), then validate and queue the run through the same asynchronous pipeline as `POST /runs` (`_accept_async_run`). The rendered YAML text is stored on the run record so retries remain exact. Prefer this endpoint for routine execute missions; raw YAML via `POST /runs` remains fully supported.

**Request body** `application/json`

Required fields:

| Field | Type | Description |
| --- | --- | --- |
| `mission_id` | string | Mission identifier |
| `title` | string | Mission title |
| `instructions` | string | Agent instructions |
| `deliverables` | array | Declared deliverables (empty list allowed) |
| `create_files` | boolean | Agent may create files |
| `modify_files` | boolean | Agent may modify existing files |

Optional fields and defaults:

| Field | Type | Default |
| --- | --- | --- |
| `persistence_mode` | string or null | inferred (see below) |
| `repository_name` | string | `Mission-Control` |
| `repository_path` | string | `.` |
| `base_branch` | string | `main` |
| `run_commands` | boolean | `true` |
| `platform_push_approved` | boolean | `false` |
| `allow_automatic_platform_push` | boolean | `false` |

**Structured `persistence_mode` resolution** (mission builder; not raw YAML):

| Caller `persistence_mode` | `create_files` / `modify_files` | Resolved `persistence.mode` |
| --- | --- | --- |
| omitted / `null` | either true | `push` |
| omitted / `null` | both false (read-only) | `none` |
| explicit `none` / `commit` / `push` | any | that explicit value (never overridden) |

Raw Mission Spec YAML still defaults an omitted `persistence` block to `mode: none` via `resolve_persistence_mode`. Only the structured / natural-language submission path applies the mutation→`push` inference above.

Builder-controlled fields (callers cannot override in v1): `version: 1.0`, `execution.agent: cursor`, `execution.mode: execute`, `execution.sandbox: true`, `execution.worktree: false`, `permissions.read: true`, `permissions.delete_files: false`, `permissions.stage_changes: false`, `permissions.commit: false`, `permissions.push: false`, `approval.execute_without_approval: true`, `approval.commit_requires_approval: true`, `approval.push_requires_approval: true`.

Platform-push approval rules are unchanged: `persistence_mode=push` (including when inferred for create/modify missions) still requires `platform_push_approved=true` or `allow_automatic_platform_push=true`.

**Response** `202 Accepted` when the run is queued — same shape as `POST /runs` (`run_id`, `status: queued`).

Validation, eligibility, preflight, and recursive-submission failures return `200 OK` with a `RunResponse` body (`ok: false`) instead of queueing a run (same shapes as `POST /runs`).

### GET /runs/{run_id}

Requires authentication.

Return lifecycle status and retained output for a previously accepted run.

**Response** `200 OK`

| Field | Type | Description |
| --- | --- | --- |
| `run_id` | string | Run identifier |
| `status` | string | `queued`, `running`, `completed`, `failed`, or `timed_out` |
| `created_at` | string | ISO timestamp when the run was accepted (queue time) |
| `queued_at` | string | Same instant as `created_at` (explicit queue-time alias; backward compatible) |
| `started_at` | string or null | Set when execution begins |
| `completed_at` | string or null | Set when the run reaches a terminal status |
| `elapsed_seconds` | number or null | Duration from start to completion |
| `phase` | string | Authoritative platform phase: `queued`, `workspace_preparation`, `agent_execution`, `verification`, `persistence`, `cleanup`, `completed`, or `failed` |
| `phase_started_at` | string or null | ISO timestamp when the current `phase` began |
| `heartbeat_at` | string or null | ISO timestamp refreshed periodically while long agent execution is active; also updated on phase transitions |
| `progress` | object or null | Small platform-authored progress object with `step` and `detail` only (bounded/redacted; never secrets, prompts, command payloads, or raw agent output) |
| `stdout` | string | Agent stdout when available (diagnostic; not verified evidence) |
| `stderr` | string | Agent stderr when available (diagnostic; not verified evidence) |
| `error` | string or null | Failure detail when unsuccessful |
| `return_code` | integer or null | Process exit code when available |
| `commit_sha` | string or null | Commit SHA after successful platform persistence (`persistence.mode` of `commit` or `push`); null when mode is `none` or there were no changes |
| `result` | object or null | Structured objective evidence collected by Mission Control (see below). Null for non-terminal runs that have not stored evidence yet; present on terminal runs when Mission Control recorded evidence |
| `summary` | string or null | Authoritative Mission Control-authored run summary, aligned with platform persistence outcome. Prefer this over agent `stdout` for persistence claims. Mirrors `result.summary` when present; null when no structured evidence has been stored yet |
| `retried_from` | string or null | Source `run_id` when this run was created via `POST /runs/{run_id}/retry`; otherwise null |

Live `phase` / `heartbeat_at` / `progress` describe what the platform is doing without relying on empty stdout or agent prose. Terminal `status` and terminal `phase` values are monotonic: a completed/failed/timed_out run cannot regress to a running phase, and a stale worker cannot overwrite a newer terminal state. `stdout` / `stderr` semantics are unchanged.

#### Trust boundary: `summary` / `result` vs `stdout` / `stderr`

- **`summary`** and **`result`** are objective Mission Control evidence. Platform persistence runs **after** the Cursor agent completes, so agent stdout may correctly report that no agent commit/push occurred while Mission Control still records a successful platform persistence outcome. Prefer `summary`, `result.persistence`, and `commit_sha` for persistence claims.
- **`stdout` / `stderr`** are agent-authored diagnostic text captured before platform persistence. Do **not** treat natural-language claims in stdout as verified structured evidence.

#### `result` object

| Field | Type | Description |
| --- | --- | --- |
| `files_changed` | string[] | Repository-relative paths changed in the isolated workspace (from Git status). Empty when none or unavailable |
| `commands` | object[] | Commands Mission Control executed (for example the Cursor agent subprocess), each with `argv`, `exit_code`, `passed`, and `kind` |
| `test_counts` | object or null | Aggregate pass/fail/skip counts when reliably available without fragile parsing; otherwise `null` |
| `deliverables` | object or null | Declared file-deliverable verification: `verified`, `passed`, `checked_paths`, `missing` |
| `persistence` | object or null | Platform persistence outcome: `mode` (authoritative completed/attempted persistence level from validated mission config and the persistence execution result — never inferred from agent stdout), `attempted`, `ok`, `commit_sha`, `pushed` (true only after a successful platform push) |
| `warnings` | string[] | Limitations explaining unavailable evidence (never fabricated values) |
| `summary` | string or null | Authoritative Mission Control-authored summary consistent with `persistence` (same text as top-level `summary`) |

Failed and timed-out runs retain any partial evidence Mission Control actually collected.

**Example completed response**

```json
{
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "completed",
  "created_at": "2026-07-23T17:00:00+00:00",
  "queued_at": "2026-07-23T17:00:00+00:00",
  "started_at": "2026-07-23T17:00:01+00:00",
  "completed_at": "2026-07-23T17:01:30+00:00",
  "elapsed_seconds": 89.0,
  "phase": "completed",
  "phase_started_at": "2026-07-23T17:01:30+00:00",
  "heartbeat_at": "2026-07-23T17:01:30+00:00",
  "progress": {
    "step": "completed",
    "detail": "Run completed"
  },
  "stdout": "Agent prose summary (diagnostic only)\n",
  "stderr": "",
  "error": null,
  "return_code": 0,
  "commit_sha": "abc123def456",
  "summary": "Platform persistence succeeded (mode=commit, commit_sha=abc123def456). Agent stdout is diagnostic only and was captured before platform persistence when persistence ran; prefer this summary, result.persistence, and commit_sha for persistence claims.",
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
      "commit_sha": "abc123def456",
      "pushed": false
    },
    "warnings": [
      "Aggregate test counts are unavailable; Mission Control does not parse agent stdout for test results.",
      "No separate Mission Control verification shell commands were executed; only the Cursor agent subprocess and platform checks are recorded.",
      "Agent stdout was captured before platform persistence; prefer result.summary, result.persistence, and commit_sha for the persistence outcome."
    ],
    "summary": "Platform persistence succeeded (mode=commit, commit_sha=abc123def456). Agent stdout is diagnostic only and was captured before platform persistence when persistence ran; prefer this summary, result.persistence, and commit_sha for persistence claims."
  },
  "retried_from": null
}
```

**Response** `404 Not Found` only when the `run_id` was never accepted by this process. Completed and failed runs are retained and keep returning `200` with their terminal status and failure details.

### GET /runs/{run_id}/notifications

Requires authentication.

**OpenAPI operation ID:** `list_run_notifications`

Bounded, redacted Phase 2C durable notification inspection for a run
(`phase_change`, `stale`, `recovery`, `terminal`). Opt-in webhook delivery is
independent: inspection works even when webhooks are disabled.

| Query | Type | Default | Description |
| --- | --- | --- | --- |
| `limit` | integer | `64` | Max events to return; clamped to `1..64` |

**Response** `200 OK` fields: `run_id`, `notifications_enabled`, `events[]`
(allowlisted inspection fields only, with redacted `last_error`), `truncated`,
`max_events`. Never returns webhook URL/secret, claim owner, raw request
headers/body, mission YAML, or raw stdout/stderr.

MCP `list_run_notifications` and Unified/Unified1 `mission.list_notifications`
forward to this endpoint with the same allowlist and limit bounds.

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

**OpenAPI operation ID:** `wait_for_run`

Bounded server-side wait for an asynchronous run (Phase 2B monitoring contract). Polls the existing run lookup path (`GET /runs/{run_id}` / registry `get_run`) until the run reaches a terminal status or `timeout_seconds` elapses. Returns immediately when the run is already terminal. Uses Phase 2A `phase`, `heartbeat_at`, and `progress` fields to build a bounded, deduplicated `monitoring_history`. Does **not** mutate, fail, or cancel the run when the wait expires (a caller/edge wait timeout is distinct from mission execution status `timed_out`).

HTTP clients, Custom GPT Actions, MCP `wait_for_run`, and Unified
`mission.wait` all use this server-side wait path. Phase 2B monitoring fields
are authored only by Mission Control and forwarded unchanged through MCP /
gateway wrappers.

**Intended HAL / Custom GPT flow**

1. Prefer `submit_and_wait` (`POST /runs/submit-and-wait`, or the MCP tool) for exact YAML end-to-end in one call — or `submit_run` (`POST /runs`) then `wait_for_run`
2. When using separate operations: `wait_for_run` (`POST /runs/{run_id}/wait` or MCP) — poll until terminal or wait budget exhausted; when `wait_expired` is true, call again with the same `run_id` and optional `cursor` from the prior response
3. Inspect `status`, `heartbeat_health`, `monitoring_history`, authoritative `summary`, `result.persistence`, `commit_sha`, then diagnostic `stdout` / `stderr` / `error` (prefer `summary` over agent stdout for persistence claims; never treat monitoring events as raw agent I/O)

**Request body** `application/json` (all fields optional; defaults shown)

| Field | Type | Default | Bounds | Description |
| --- | --- | --- | --- | --- |
| `timeout_seconds` | number | `300` | `0.1` … `3600` | Maximum time to wait for a terminal status (caller/edge budget only) |
| `poll_interval_seconds` | number | `25` | `0.05` … `60` | Delay between registry lookups while the run is non-terminal |
| `cursor` | string or null | `null` | opaque, max `16384` chars | Resumable monitor cursor from a prior `wait_expired` response; preserves bounded `monitoring_history` across waits. Oversized cursors are rejected before decode. |

Out-of-bounds values return `422 Unprocessable Entity`.

**Response** `200 OK`

Includes the same fields as `GET /runs/{run_id}`, plus:

| Field | Type | Description |
| --- | --- | --- |
| `reached_terminal` | boolean | `true` when the run status is terminal (`completed`, `failed`, `timed_out`, or `cancelled`) |
| `wait_expired` | boolean | `true` when the wait budget elapsed while the run was still non-terminal |
| `timeout_seconds` | number | Effective wait budget used for this call |
| `heartbeat_health` | string | `healthy`, `stale`, `absent`, `not_applicable`, or `terminal` |
| `stale_heartbeat` | boolean | `true` when `heartbeat_health` is `stale` (reported only; does not cancel or mutate the run) |
| `stale_threshold_seconds` | number | Documented stale threshold (`30`, six times the 5s heartbeat cadence) |
| `monitoring_history` | array | Bounded (max 32) deduplicated phase/progress/health events; excludes prompts, commands, secrets, stdout, and stderr |
| `cursor` | string | Opaque resumable cursor encoding the current bounded history |

| Outcome | `reached_terminal` | `wait_expired` | Run state mutated? |
| --- | --- | --- | --- |
| Already terminal / becomes terminal during wait | `true` | `false` | No (wait only observes) |
| Wait budget exhausted while non-terminal | `false` | `true` | No (latest status + `cursor` / `run_id` returned; resume with the same `run_id`) |

**Heartbeat health.** While a run is `queued`, health is `not_applicable` (agent heartbeat cadence is not active). Active non-terminal runs with a recent `heartbeat_at` are `healthy`; when `heartbeat_at` is older than `stale_threshold_seconds` the wait reports `stale` / `stale_heartbeat: true` without cancelling. Missing `heartbeat_at` on an active run is `absent`. Terminal statuses classify as `terminal`.

**Monitoring history.** Events are appended only on meaningful `status` / `phase` / sanitized `progress` / `heartbeat_health` changes (repeated heartbeat refreshes alone do not duplicate events). Progress is platform-authored and redacted via the same sanitizer as live status.

Terminal statuses for this wait contract include `completed`, `failed`, `timed_out`, and `cancelled` (monitoring recognizes `cancelled` even when older registry rows only use the first three).

**Response** `404 Not Found` when the `run_id` is unknown.

### POST /runs/submit-and-wait

Requires authentication.

**OpenAPI operation ID:** `submit_and_wait`

Accept an exact Mission Control YAML document, queue it through the same asynchronous pipeline as `POST /runs` (`_accept_async_run`), then wait via the same shared wait helper as `POST /runs/{run_id}/wait` until the run reaches a terminal status or `timeout_seconds` elapses. Returns the final authoritative run payload in one request. Does **not** duplicate submit or wait execution logic.

Prefer this endpoint for Custom GPT Actions / HAL when exact YAML is already available and a single HTTP call should cover submit + wait. Routine structured missions still use `POST /runs/structured` (or MCP `submit_structured_run`) then `wait_for_run`.

**Request body** `application/json`

| Field | Type | Required | Default | Bounds | Description |
| --- | --- | --- | --- | --- | --- |
| `mission_yaml` | string | yes | — | non-empty | Exact mission YAML document (same as `POST /runs`) |
| `timeout_seconds` | number | no | `300` | `0.1` … `3600` | Maximum time to wait after acceptance (caller/edge budget; does not mark the mission `timed_out`) |
| `poll_interval_seconds` | number | no | `25` | `0.05` … `60` | Delay between registry lookups while non-terminal |
| `cursor` | string or null | no | `null` | opaque | Optional monitor cursor (normally unused on first submit) |

Out-of-bounds wait values return `422 Unprocessable Entity` **before** submission (Pydantic validation), so an invalid timeout never queues a run.

**Response** `200 OK` — wait finished

Same shape as `POST /runs/{run_id}/wait` (`WaitForRunResponse`): run fields plus `reached_terminal`, `wait_expired`, `timeout_seconds`, `heartbeat_health`, `stale_heartbeat`, `monitoring_history`, `cursor`, and `stale_threshold_seconds`.

**Response** `200 OK` — submission / validation failure

Same `RunResponse` rejection shapes as `POST /runs` (`ok: false`, no `run_id`). Returned **immediately** without entering the wait loop.

Recursive local submissions are rejected the same way as `POST /runs`.

**Wait-window expiry.** When the wait budget expires while the run is still non-terminal, returns `wait_expired: true` with the accepted `run_id`, latest status fields, and a resumable `cursor`. Resume with `POST /runs/{run_id}/wait` (`wait_for_run`) using that `run_id` and `cursor`. Wait expiry never fails or cancels the mission.

### POST /repository-commands

Requires authentication.

**OpenAPI operation ID:** `run_repository_command`

Execute one allowlisted repository command in an ephemeral checkout. argv is launched directly (`shell=False`) — no shell interpolation. Persistence is always `none` (this path never stages, commits, or pushes).

Allowlisted scripts (per-script argv/env policy):

- `python3` + `scripts/generate_attorney_feedback_candidate.py`
- `python3` + `scripts/rebuild_case00_derived.py`
- `python3` + `scripts/run_case00_b2_q1.py` (Case-00 B2 rebuild + Q1 generation)

For `scripts/run_case00_b2_q1.py`, optional `--candidate-b2-prefix` is a
non-secret B2 **object-prefix** string (not a local filesystem path). When the
flag is omitted, LegalAI's wrapper keeps its canonical durable default under
`Benchmarks/Case-00-Triborough/derived/attorney-feedback-eval/candidate-answers/`.
Durable candidate success is the wrapper's verified `durable_artifacts` / B2
object keys (and nonzero wrapper failures remain failures). Executor
`artifact_paths` under `--candidate-output-root` are ephemeral local scratch
only and are **not** durable proof.

**Request body** `application/json`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `repository` | string | yes | Allowlisted repository id (`nhpcorp35/legal-ai` or `legal-ai`) |
| `ref` | string | yes | Branch name or commit SHA to check out |
| `argv` | string[] | yes | Exact argv vector (no shell) |
| `working_directory` | string | no | Repo-relative working directory (default `.`) |
| `timeout_seconds` | number | no | Command timeout (default `300`, max `3600`) |
| `allowed_env_names` | string[] | no | Names of env vars to forward (platform allowlist enforced; values never logged) |

**Response** `200 OK`

| Field | Type | Description |
| --- | --- | --- |
| `ok` | boolean | Whether the command exited 0 |
| `run_id` | string | Registry run identifier |
| `checkout_commit` | string or null | Detached HEAD commit of the ephemeral checkout |
| `argv` | string[] | Exact argv with sensitive values redacted |
| `stdout` / `stderr` | string | Captured process output |
| `exit_code` | integer or null | Process exit code (`null` on timeout) |
| `elapsed_seconds` | number | Wall time |
| `artifact_paths` | string[] | Ephemeral local files under `--candidate-output-root` when present. Not durable proof — Case-00 durable candidate keys come from verified B2 upload in wrapper stdout (`durable_artifacts`) |
| `persistence` | object | Always `{mode: "none", attempted: false, ...}` |
| `error` / `error_code` | string or null | Rejection or failure detail |

Configure clone URLs via `MISSION_CONTROL_REPOSITORY_URL_MAP` (JSON) or `MISSION_CONTROL_LEGAL_AI_REPOSITORY_URL`. Mounted artifact/data roots via `MISSION_CONTROL_MOUNTED_PATHS` (colon-separated absolute paths).

### MCP tools

The Mission Control MCP connector exposes exactly these run-operation tools:

| Tool | Purpose |
| --- | --- |
| `submit_run` | Submit mission YAML (`POST /runs`) |
| `submit_structured_run` | Submit structured mission fields (`POST /runs/structured`); prefer for routine execute missions |
| `get_run` | Fetch current run status (`GET /runs/{run_id}`) |
| `list_run_notifications` | Bounded redacted Phase 2C notification inspection (`GET /runs/{run_id}/notifications`) |
| `wait_for_run` | Poll `get_run` until terminal or caller-requested wait window expires (REST equivalent: `POST /runs/{run_id}/wait`) |
| `submit_and_wait` | Submit exact mission YAML then wait in one call (`submit_run` + `wait_for_run`; REST equivalent: `POST /runs/submit-and-wait`) |
| `run_repository_command` | Run allowlisted repository command in ephemeral checkout (`POST /repository-commands`) |

#### ChatGPT custom MCP app

Use the Streamable HTTP endpoint (not `/sse`):

| Item | Value |
| --- | --- |
| ChatGPT MCP server URL | `https://mission-control-mcp-production.up.railway.app/mcp` |
| Transport | Streamable HTTP (`SERVICE_MODE=mcp` on Railway) |
| Authentication in ChatGPT | **No authentication** |
| Backend API auth | Connector uses server-side `MISSION_CONTROL_API_KEY` as `Authorization: Bearer …` when calling Mission Control; that key is not sent by ChatGPT |

Legacy SSE is also mounted at `https://mission-control-mcp-production.up.railway.app/sse` (with `/messages`) so older `/sse` app URLs keep discovering the same tools. Prefer `/mcp` for new ChatGPT custom apps.

Local MCP HTTP (same routes as Railway):

```bash
export SERVICE_MODE=mcp
export PORT=8001
export MISSION_CONTROL_URL="https://mission-control-production-76ff.up.railway.app"
export MISSION_CONTROL_API_KEY="<key>"
bash scripts/railway-start.sh
```

#### `wait_for_run`

Forwards to authenticated `POST /runs/{run_id}/wait`. Mission Control is the
source of truth for Phase 2B monitoring; the connector validates bounds then
returns the wait payload unchanged (does not fabricate monitoring fields).

| Argument | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `run_id` | string | yes | — | Run identifier returned by `submit_run` / `submit_structured_run` / `submit_and_wait` |
| `timeout_seconds` | number | no | `20` | Maximum time to wait for this call; must be `>= 0.1`. Values above `3600` are capped to `3600` (same upper bound as `POST /runs/{run_id}/wait`). Zero/negative values are rejected. |
| `poll_interval_seconds` | number | no | `2` | Delay between registry lookups on the server; must be `>= 0.05`. Values above `10` are capped to `10`. Zero/negative values are rejected. |
| `cursor` | string or null | no | `null` | Opaque resumable monitor cursor from a prior `wait_expired` response (max `16384` chars). Omit for legacy callers. Oversized values are rejected before forwarding. |

**Intended HAL / Unified loop.** Prefer `submit_and_wait` when you already have exact
mission YAML and want one tool call end-to-end. Prefer `submit_structured_run`
for routine execute missions (or `submit_run` with exact YAML when needed) →
`wait_for_run` / `mission.wait` with an appropriate `timeout_seconds` until `wait_expired` is
`false` and `status` is terminal (retry the same `run_id` and returned `cursor` when `wait_expired`
is `true`) → inspect `heartbeat_health` / `monitoring_history` / `summary` / `result.persistence` / `commit_sha` /
`result`, then diagnostic `stdout` / `stderr` / `error`. Prefer `summary` over
agent stdout for persistence claims (platform persistence runs after the agent
completes).

**Terminal behavior.** Terminal statuses are `completed`, `failed`, `timed_out`,
and `cancelled`. Returns immediately when the run is already terminal. Payload
shape: `{"ok": true, ...}` with the authoritative wait fields from Mission
Control, including `wait_expired: false`, `reached_terminal: true`,
`timeout_seconds`, `heartbeat_health`, `stale_heartbeat`, `monitoring_history`,
`cursor`, and `stale_threshold_seconds`.

**Wait-window expiry.** When the wait window expires while the run is still non-terminal,
the tool returns a **normal usable payload** (not a transport/tool error):

| Field | Value |
| --- | --- |
| `ok` | `true` |
| `run_id` / `status` / other run fields | Latest run payload from Mission Control |
| `wait_expired` | `true` |
| `reached_terminal` | `false` |
| `timeout_seconds` | Effective wait window used for this call |
| `cursor` / monitoring fields | Resumable Phase 2B monitoring from Mission Control |

HAL / Unified should treat `wait_expired: true` as “call `wait_for_run` /
`mission.wait` again with the same `run_id` and `cursor`,” not as failure.
Unknown `run_id` (`404`) is fatal immediately. Wait expiry never mutates or
cancels the run.

**Timeout layers (connector vs platform).** Application bounds above are the
only connector-imposed wait limits. The connector sets the httpx timeout above
the caller-selected wait budget so Mission Control can return `wait_expired`
within that duration. Railway’s public
edge proxy closes HTTP requests after **5 minutes with no data transferred**,
or after **15 minutes** even with keep-alive traffic
([Railway public networking specs](https://docs.railway.com/networking/public-networking/specs-and-limits)).
A single Streamable HTTP MCP tool response that stays silent for the whole wait
can therefore be cut by the platform before a 900s application budget finishes;
when that happens, treat it like a transport interrupt and call `wait_for_run`
again with the same `run_id` and last `cursor`. Upstream MCP clients may also impose their own
tool-call deadlines independent of these connector bounds.

#### `submit_and_wait`

Submits an exact Mission Control YAML document via the authenticated
`submit_run` path, then waits via the same `wait_for_run` forwarder to
`POST /runs/{run_id}/wait` — one MCP tool call for end-to-end execution. Does
not duplicate submit or wait logic.

| Argument | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `mission_yaml` | string | yes | — | Exact mission YAML document (same as `submit_run`) |
| `timeout_seconds` | number | no | `20` | Same validation and limits as `wait_for_run` (must be `>= 0.1`; values above `3600` capped to `3600`; zero/negative rejected). Validated **before** submission so an invalid timeout never queues a run. |
| `poll_interval_seconds` | number | no | `2` | Same validation and limits as `wait_for_run` |
| `cursor` | string or null | no | `null` | Same optional cursor validation as `wait_for_run` (normally unused on first submit) |

**Success.** Returns `{"ok": true, ...}` with the accepted `run_id` and the
final authoritative wait payload from Mission Control (including
`wait_expired`, `reached_terminal`, `timeout_seconds`, and Phase 2B
monitoring fields).

**Submission failure.** If `submit_run` returns the existing structured
rejection (`ok: false`, no `run_id`), that payload is returned immediately
without entering the wait loop.

**Wait-window expiry.** Same structured `wait_expired: true` payload as
`wait_for_run` (latest run fields plus resumable `cursor`). Resume with
`wait_for_run` using the returned `run_id` and `cursor`.

Authentication and run isolation match `submit_run` / `wait_for_run` (server-side
API key; isolated run workspaces).

### Platform Git persistence

After a successful agent execution in an isolated workspace, Mission Control
verifies declared **file** deliverables (see below), then applies the
mission's top-level `persistence` block:

| `persistence.mode` | Behavior |
| --- | --- |
| `none` (default when the block is omitted) | Do not stage, commit, or push |
| `commit` | Stage and create a local commit; never push |
| `push` | Stage, commit, and push to `repository.base_branch` (privileged; requires platform-push approval) |

Agent `permissions.stage_changes`, `permissions.commit`, and `permissions.push` are legacy agent-facing fields only. They do not select platform persistence behavior and are not required (or rejected) for execute eligibility. Use `persistence.mode` as the authoritative mechanism for staging, committing, and pushing. Unsupported `persistence.mode` values fail mission validation.

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

Push authorization is expressed through `persistence.mode=push` plus `approval.platform_push_approved=true` (or `approval.allow_automatic_platform_push=true`). There is no separate `permissions.push` platform gate; truthy agent `permissions.push` does not authorize (or block) platform push.

Execute missions with `persistence.mode=push` and platform-push approval may be **push-only**: they are valid even when `create_files=false` and `modify_files=false`.

Execute missions may also be **read-only**: when permissions are exactly
`read=true`, `create_files=false`, `modify_files=false`, `delete_files=false`,
`run_commands=true`, `stage_changes=false`, `commit=false`, and `push=false`,
the mission is accepted for `execution.mode: execute` without create/modify
writes. Read-only execute missions may inspect and analyze the repository and
run non-mutating commands; they must not create, modify, or delete files, or
stage, commit, or push.

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
| OCR (PDF pages) | `tesseract` + `pdftoppm` on `PATH` (apt: `tesseract-ocr`, `tesseract-ocr-eng`, `poppler-utils`); Python `pytesseract` and `pdf2image` in the venv |

Execution preflight fails with `PYTHON_UNAVAILABLE` when no Python 3 interpreter can be resolved before a mission runs.

### Required environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `MISSION_CONTROL_API_KEY` | yes | Shared secret for Mission Control HTTP API authentication (`Authorization: Bearer …`). Required by the API and by the MCP connector. Do not commit this value. |
| `CURSOR_API_KEY` | yes | Cursor user API key from [cursor.com/dashboard/api](https://cursor.com/dashboard/api). Used by `cursor-agent` at runtime. Do not commit this value. |
| `PORT` | yes | Provided automatically by Railway. |

Set `MISSION_CONTROL_API_KEY` and `CURSOR_API_KEY` in the Railway service **Variables** tab. Use secret/reference variables, not hardcoded values in the repo.

### Optional Phase 2C notification environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `MISSION_CONTROL_NOTIFICATIONS_WEBHOOK_URL` | for delivery | HTTPS webhook URL (default). HTTP only when `MISSION_CONTROL_NOTIFICATIONS_ALLOW_HTTP` is true. |
| `MISSION_CONTROL_NOTIFICATIONS_WEBHOOK_SECRET` | for delivery | HMAC shared secret. Never commit or log. |
| `MISSION_CONTROL_NOTIFICATIONS_ENABLED` | no | Soft enable; delivery still needs URL and secret. |
| `MISSION_CONTROL_NOTIFICATIONS_TIMEOUT_SECONDS` | no | Per-attempt timeout (default `5`). |
| `MISSION_CONTROL_NOTIFICATIONS_MAX_ATTEMPTS` | no | Attempts before `dead` (default `8`). |
| `MISSION_CONTROL_NOTIFICATIONS_BACKOFF_BASE_SECONDS` | no | Backoff base (default `1`). |
| `MISSION_CONTROL_NOTIFICATIONS_BACKOFF_MAX_SECONDS` | no | Backoff cap (default `300`). |
| `MISSION_CONTROL_NOTIFICATIONS_ALLOW_HTTP` | no | Dev-only HTTP webhook allow. Leave unset/false in production. |

Webhook HMAC header `X-Mission-Control-Signature` uses `t=<unix>,v1=<hex>` over
`{timestamp}.{body}` (HMAC-SHA256). Retries use exponential backoff; exhausted
attempts become `dead` without changing mission status. Inspect with
`GET /runs/{run_id}/notifications`, MCP `list_run_notifications`, or Unified
`mission.list_notifications`. Rotate secrets by updating receivers first, then
Mission Control; disable by clearing URL/secret. See `docs/HAL_OPERATOR.md`.

### Build and start commands

Railway reads:

- `nixpacks.toml` — enables the Python provider, installs `curl`, `python3`, `git`, and the OCR apt stack (`tesseract-ocr`, `tesseract-ocr-eng`, `poppler-utils`), then runs `scripts/install-cursor-agent.sh`
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
