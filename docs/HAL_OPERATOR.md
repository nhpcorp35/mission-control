# HAL Operator Procedure

HAL is the Mission Control operator: it runs missions, interprets results, verifies
claims against repository state, and submits corrective follow-up missions.

## Source of truth

- **Repository state is the source of truth.**
- Mission summaries alone are not proof.
- For async Mission Control runs, prefer `summary`, `result.persistence`, and
  `commit_sha` over agent `stdout` when judging persistence: platform
  persistence runs after the agent completes.
- For `POST /repository-commands` / MCP `run_repository_command`, persistence is
  always `none`. Executor `artifact_paths` under `--candidate-output-root` are
  ephemeral local scratch only. Durable Case-00 candidate handoff is the
  wrapper's verified B2 `durable_artifacts` / object keys in command stdout —
  not those local paths.
- Significant claims must be independently verified through tests, source
  inspection, repository state, or equivalent direct evidence.

## Operator log (mandatory)

- Every significant Mission Control objective must end by updating
  `docs/HAL_OPERATOR_LOG.md` with verified results.
- A Mission Control objective is not complete until the operator log update is
  verified and published when persistence is required.
- Repository-changing missions should include `docs/HAL_OPERATOR_LOG.md` as a
  declared file deliverable.

Operating procedure detail lives in this document; durable verified outcomes live
in `docs/HAL_OPERATOR_LOG.md`.

## Mission submission

- **Prefer structured submission** for routine execute missions:
  MCP `submit_structured_run` or HTTP `POST /runs/structured`.
- Structured fields are rendered into Mission Spec v1.0 YAML with safe execute
  defaults; the rendered YAML is stored on the run record so retries stay exact.
- **Structured persistence defaults:** when `persistence_mode` is omitted,
  create/modify (repository-mutating) missions resolve to `persistence.mode:
  push`; read-only inspection missions resolve to `none`. Explicit
  `persistence_mode` values (`none`, `commit`, `push`) are never overridden.
  Raw YAML omitted `persistence` blocks still default to `none`.
- **Raw YAML remains fully supported** via MCP `submit_run` / HTTP `POST /runs`
  when exact document control is required (or when fields outside the structured
  v1 surface must be set by hand).
- **Exact YAML end-to-end:** Prefer `submit_and_wait` — HTTP
  `POST /runs/submit-and-wait` (OpenAPI operation ID `submit_and_wait`) or MCP
  `submit_and_wait` — which submits via `submit_run` / `POST /runs` and waits via
  the shared wait path in one call. Prefer it when HAL / Custom GPT Actions
  already have the full mission document and should only involve Allen for
  genuine approval, decision, or unrecoverable failure.
- **Custom GPT Actions import URL** (use this, not `/openapi.json`):
  `https://mission-control-production-76ff.up.railway.app/openapi-actions.json`
  Discoverable operation IDs include `submit_run`, `get_run`, `wait_for_run`,
  and `submit_and_wait`. Auth is HTTP Bearer.
- **Import check:** After deploy, import the Actions URL above. Expect a clean
  import (`openapi` is `3.1.0`, operation descriptions under 300 characters;
  `/health` uses named `HealthResponse`). Do not import `/openapi.json` into
  Actions.
- Do not weaken platform-push approval: `persistence_mode=push` (including when
  inferred for create/modify structured missions) still requires explicit
  platform-push approval fields.

## Autonomy

HAL should continue operating runs, interpreting results, and submitting
corrective follow-up missions without requiring the user to ask for status,
except when a real approval, product decision, destructive action, or unresolved
ambiguity requires user input.

## Waiting for async runs

- Prefer `submit_and_wait` (`POST /runs/submit-and-wait` or MCP) for exact YAML
  when a single call should cover submit + wait; resume with `wait_for_run` /
  `mission.wait` on `wait_expired` using the same `run_id` and returned `cursor`.
- REST `POST /runs/{run_id}/wait` (OpenAPI operation ID `wait_for_run`) performs
  a server-side wait and returns `run_id`, `timeout_seconds`, `wait_expired`,
  `reached_terminal`, Phase 2B monitoring fields (`heartbeat_health`,
  `stale_heartbeat`, `monitoring_history`, `cursor`, `stale_threshold_seconds`),
  and the latest run payload. Wait expiry never mutates or cancels the run.
- MCP `wait_for_run` / Unified `mission.wait` forward to that REST wait path
  (Mission Control remains the monitoring source of truth). Optional `cursor`
  is accepted; oversized cursors are rejected before forward.
- MCP `wait_for_run` (and `submit_and_wait`) honor the requested
  `timeout_seconds` up to **3600** (aligned with `POST /runs/{run_id}/wait`).
  There is no artificial ~25s connector cutoff.
- Default wait window is **20s** for MCP tools and **300s** for REST wait
  endpoints; pass a larger budget (for example `900`) when a single call should
  stay active until terminal or that budget expires.
- When `wait_expired` is `true`, call `wait_for_run` / `mission.wait` again with
  the same `run_id` and `cursor` (do not treat expiry as run failure).
- Railway’s edge proxy may still close a silent HTTP/MCP tool response after
  **5 minutes idle** or **15 minutes** absolute — see `MISSION_CONTROL_API.md`
  (`wait_for_run` timeout layers). On a transport interrupt, resume with the
  same `run_id` and last `cursor`.

## Phase 2C/2D durable notifications (opt-in webhooks + Pushover)

Opt-in delivery for `phase_change`, `stale`, `recovery`, and `terminal`
events only (never heartbeats). Delivery failures never mutate mission/run
status. With no backend fully configured, notifications stay disabled (safe
default). Supported backends:

1. **HMAC webhook** — generic HTTPS receiver (`WEBHOOK_URL` + `WEBHOOK_SECRET`)
2. **Native Pushover** — official `https://api.pushover.net/1/messages.json`
   (`PUSHOVER_USER_KEY` + `PUSHOVER_APP_TOKEN`)

**Dual-backend policy:** when both backends are fully configured, Mission
Control delivers via the **webhook only** (no duplicate user alerts). To use
Pushover exclusively, leave webhook URL/secret unset.

### Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `MISSION_CONTROL_NOTIFICATIONS_WEBHOOK_URL` | for webhook delivery | HTTPS webhook endpoint (HTTP only when `ALLOW_HTTP` is explicitly true). |
| `MISSION_CONTROL_NOTIFICATIONS_WEBHOOK_SECRET` | for webhook delivery | Shared HMAC secret. Never log or commit the value. |
| `MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_USER_KEY` | for Pushover delivery | Pushover user/group key from the Pushover dashboard. Placeholder: `YOUR_PUSHOVER_USER_KEY`. |
| `MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_APP_TOKEN` | for Pushover delivery | Application API token from a Pushover application. Placeholder: `YOUR_PUSHOVER_APP_TOKEN`. |
| `MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_DEVICE` | no | Optional device name filter. |
| `MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_PRIORITY` | no | `-2`..`1` (default `0`). Emergency priority `2` is rejected. |
| `MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_SOUND` | no | Optional Pushover sound name. |
| `MISSION_CONTROL_NOTIFICATIONS_ENABLED` | no | Soft enable flag; delivery still requires a fully configured backend. |
| `MISSION_CONTROL_NOTIFICATIONS_TIMEOUT_SECONDS` | no | Per-attempt HTTP timeout (default 5s). |
| `MISSION_CONTROL_NOTIFICATIONS_MAX_ATTEMPTS` | no | Attempts before `dead` (default 8). |
| `MISSION_CONTROL_NOTIFICATIONS_BACKOFF_BASE_SECONDS` | no | Exponential backoff base (default 1s). |
| `MISSION_CONTROL_NOTIFICATIONS_BACKOFF_MAX_SECONDS` | no | Backoff cap (default 300s). |
| `MISSION_CONTROL_NOTIFICATIONS_ALLOW_HTTP` | no | When true, permits `http://` webhook URLs (dev only). |

### Pushover setup (Railway)

1. Create a Pushover application at https://pushover.net/apps/build and copy the
   **API token** (use placeholder `YOUR_PUSHOVER_APP_TOKEN` in docs/examples).
2. Copy your **user key** from the Pushover dashboard
   (`YOUR_PUSHOVER_USER_KEY`).
3. In the Railway service **Variables** tab, set:
   - `MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_USER_KEY=YOUR_PUSHOVER_USER_KEY`
   - `MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_APP_TOKEN=YOUR_PUSHOVER_APP_TOKEN`
   - Optional: `…_DEVICE`, `…_PRIORITY` (`0` or `1`), `…_SOUND`
4. Redeploy / restart so the worker picks up env. Leave webhook URL/secret
   unset unless you intentionally want webhook delivery (webhook wins if both
   are set).
5. **Test:** enqueue a terminal/stale event (complete or stale a run) and confirm
   a Pushover notification arrives. Inspect
   `GET /runs/{run_id}/notifications` for `delivery_state=delivered`.
6. **Rotate:** create a new app token (or regenerate), update Railway variables,
   restart, then revoke the old token in Pushover.
7. **Disable:** clear user key and/or app token (or set
   `MISSION_CONTROL_NOTIFICATIONS_ENABLED=false`). Pending outbox rows remain
   durable; HTTP delivery stops.

**Privacy:** titles/bodies include only Mission Control identity, event
severity, run id, phase/status, and bounded progress step/detail. Never
stdout/stderr, mission instructions, secrets, webhook URLs, repository
credentials, or raw exception bodies. Credentials must never appear in logs,
API, MCP, Unified, or database inspection payloads.

### HMAC and retry semantics

- Signature header `X-Mission-Control-Signature`: `t=<unix>,v1=<hex>` over
  `{timestamp}.{body}` with HMAC-SHA256.
- Also sent: `X-Mission-Control-Timestamp`, `X-Mission-Control-Event-Id`,
  `X-Mission-Control-Event-Kind`.
- Transient failures retry with exponential backoff up to max attempts, then
  `dead`. Permanent URL/credential validation failures mark `dead` without
  mutating runs.
- Pushover success requires HTTP 2xx **and** JSON integer `status: 1` only
  (boolean `true`, string `"1"`, floats, missing status, lists, and
  malformed/non-JSON bodies are never success).
- Any syntactically valid Pushover JSON object whose integer `status` is not
  `1` (including HTTP 2xx with `status: 0`) is a **permanent** delivery
  failure: the outbox row goes `dead` immediately and is not retried.
  Normalized `last_error` is `pushover_rejected` (provider error text that
  mentions tokens/keys is redacted; response credential echoes are never
  stored).
- Ordinary non-retryable 4xx (for example 400/401/403) remain permanent;
  transport exceptions, timeouts, HTTP 429, and HTTP 5xx remain retryable.
- Malformed / empty / non-JSON HTTP 2xx bodies (acceptance uncertain) are
  **retryable** up to max attempts, then `dead`. They are never marked
  `delivered`.
- Optional `PUSHOVER_DEVICE` / `PUSHOVER_SOUND` values that contain ASCII
  control characters are rejected (option omitted) rather than forwarded.

### Inspection workflow (redacted)

- REST: `GET /runs/{run_id}/notifications?limit=N` (auth required; `limit`
  clamped to 1–64).
- MCP: `list_run_notifications(run_id, limit=…)`.
- Unified / Unified1: `mission.list_notifications` → same downstream tool.
- Responses include only allowlisted fields (`event_id`, `event_kind`,
  `delivery_state`, redacted `last_error`, etc.). Never webhook URL/secret,
  Pushover user key/app token, claim owner, raw request headers/body, mission
  YAML, or raw stdout/stderr.
- Backend health helper `notification_backend_health()` reports
  `active_backend`, booleans for webhook/pushover configured, and option
  flags — never whether secret values match.

## Local repository auto-sync (macOS)

Allen’s Mac can keep explicitly configured clones on `main` fast-forwarded from
`origin` via the user LaunchAgent under `tools/hal-sync-service/` (launchd,
default 60s, dirty trees skipped, `--ff-only` only). Install and operate with
`./install.sh install|status|restart|uninstall` — never sudo. Full procedure:
`tools/hal-sync-service/README.md`.
