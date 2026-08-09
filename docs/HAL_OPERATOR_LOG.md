# HAL Operator Log

## 2026-08-09 — Unified Gateway cutover (primary) + rollback hold

### Objective

Record the verified live cutover state for **HAL LegalAI Gateway Unified** as
the primary ChatGPT connection, with explicit rollback/removal criteria and
tomorrow's acceptance workflow. Do **not** revise Question 1, contact John
Cuomo, or send anything externally.

### Verified state (2026-08-09)

- **Primary ChatGPT connection:** HAL LegalAI Gateway Unified.
- **Live Gateway/Bridge code SHA:**
  `a7a9cad952844973c16c4fb937d7c84ad55dc87e`.
- **Gateway→Bridge service auth:** dedicated `/mcp/service` surface; FastMCP
  `StreamableHttpTransport(auth=…)` with synchronized dedicated service
  credentials (`GATEWAY_BRIDGE_AUTHORIZATION` ↔ `BRIDGE_SERVICE_TOKEN`).
  Inbound user OAuth is **never** forwarded.
- **Mission namespace URL:** `GATEWAY_MISSION_CONTROL_URL` must target
  `https://mission-control-mcp-production.up.railway.app` (Mission Control
  **MCP**), **not** the Mission Control REST service.
- **Storage packet:** `packet-q1-20260809-e88c963fdee4` contains
  `LegalAI-Case00-Q1-Benchmark-Review-Packet.docx` (40,089 bytes) and
  `review-packet-preservation-manifest.json` (615 bytes).
- **Canonical private storage:** Backblaze B2 `legalai-corpus`. GitHub remains
  code/docs only — no private benchmark artifacts or secrets in the repo.

### Read-only Unified checks

| Check | Result |
| --- | --- |
| `storage.verify_archive` | passed |
| `mission.status` | passed after correcting `GATEWAY_MISSION_CONTROL_URL` to the MCP production base |
| `case.get_artifact` | reached the artifacts service; returned expected `run_not_found` (test used a non-Case mission ID) |

### Rollback / plugin removal criteria

Keep legacy **Bridge**, **Storage**, **Mission Control test**, and older
**Gateway** ChatGPT plugins installed but unused as rollback paths. **Do not
remove** them until **one real end-to-end LegalAI workflow** succeeds using
**only** HAL LegalAI Gateway Unified.

### Next Objective (acceptance workflow)

1. Retrieve a real Case-00 artifact with a **valid Case mission ID**.
2. Submit and monitor a real Mission Control run via Unified Gateway.
3. Verify resulting archive/inventory.
4. Confirm request IDs and failure-stage reporting on the forwarded path.

## 2026-08-09 — FastMCP Gateway→Bridge bearer delivery (`auth=`)

### Objective

Gateway→Bridge `POST /mcp/service` returned 401 after synchronized
`GATEWAY_BRIDGE_AUTHORIZATION` / `BRIDGE_SERVICE_TOKEN` rotation (same 96-hex
value on both services). Evidence: request IDs
`98037d1d-a575-4cad-976b-99e309250fe1` and
`cd343573-ec88-4725-8cfd-9af7b77c50be`. Live commit before fix:
`5ecedd8aee4b5b7ee232799ffa63c10c81feedde`.

### Root cause

Gateway forwarding injected `Authorization` manually into
`StreamableHttpTransport(headers=...)`. FastMCP 2.x merges inbound request
headers (including user OAuth `authorization`) into the outbound client and
supports `auth=` → `BearerAuth(raw_token)`. Manual header injection can be
overwritten or mishandled; do not blame credential caching after a synchronized
redeploy.

### Implementation

- `hal_legalai_gateway/forwarding.py`: pass raw normalized service token via
  `StreamableHttpTransport(auth=...)`; keep `X-Request-ID` /
  `X-Correlation-ID` (and Accept) only — never forward inbound user OAuth.
- `github_actions_bridge/service_auth.py`: secret-safe verifier diagnostics
  (`missing_bearer`, lengths, short SHA fingerprint).
- Production-path integration tests drive real `StreamableHttpTransport` against
  the composed Bridge ASGI app (matching token initialize/list/call; invalid /
  missing → 401).

### Tests executed (this mission; no-git constraints)

```text
PYTHONPATH=. python -m unittest \
  tests.test_github_actions_bridge_service_auth \
  tests.test_hal_legalai_gateway \
  -v
# Ran 62 tests — OK
```

Commit/push to `main` was **not** performed here (mission constraints forbid Git).
Exact production SHA will be whatever lands on `main` after an operator push.

### Deployment verification (after commit/push + Railway redeploy)

1. Do **not** rotate service credentials again if both env vars already match.
2. Confirm Gateway + Bridge redeploy to the fix SHA (`RAILWAY_GIT_COMMIT_SHA`).
3. Call Gateway `storage.list_inventory` → Bridge `/mcp/service` must succeed.
4. Missing/invalid bearer must still 401; verifier logs show `missing_bearer` vs
   fingerprint mismatch without token values.
5. Public `/mcp` GitHub OAuth discovery unchanged.

### Next Objective

Operator commit/push to `main` (blocked in this mission run by no-git
constraints), then Railway redeploy and live `storage.list_inventory` check.

## 2026-08-08 — Explicit repository routing (LegalAI vs Mission Control)

### Objective

Structured missions that set `repository_name=nhpcorp35/legal-ai` (path `.`,
branch `main`) were still preparing a Mission Control checkout. Agents edited
nested `.legalai_work/*` trees and platform persistence could push to
`nhpcorp35/mission-control` while reporting success.

### Root cause

`resolve_mission_clone_url` treated every non–Mission-Control `repository.name`
as the legacy single-repo `MISSION_CONTROL_REPOSITORY_URL`. After Mission
Control self-routing landed, that env commonly pointed at Mission Control, so
explicit LegalAI missions silently cloned the wrong remote.

### Implementation

- `mission_control/workspace.py`: Legal AI aliases and explicit `owner/repo`
  names resolve to their own clone URLs (map / `MISSION_CONTROL_LEGAL_AI_REPOSITORY_URL`
  / GitHub default) and never fall back to Mission Control. Prep and
  persistence fail closed on origin/target mismatch; `.legalai_work` nesting
  cannot legitimize persistence; agent workspace honors `.` as checkout root.
- Regression coverage in workspace, structured API, mission builder, and MCP
  structured client field mapping.

### Next Objective

Confirm Railway redeploy; LegalAI structured missions must clone
`nhpcorp35/legal-ai` and never persist nested LegalAI edits into Mission Control.

## 2026-08-05 — Workspace persistence handoff (repository.name → clone URL)

### Objective

Approved structured missions targeting `nhpcorp35/mission-control` completed with
`files_changed: []` and `persistence` reporting “no repository changes” even
when the coding agent had modified Mission Control sources.

### Root cause

`prepare_isolated_workspace` always cloned `MISSION_CONTROL_REPOSITORY_URL`
(Legal AI on this deployment), ignoring `repository.name`.

- Agent `--workspace` / cwd: isolated Legal AI checkout under
  `/tmp/mission-control-run-*`
- Agent edits for Mission Control missions: discovered checkout at
  `/tmp/mission-control` (or `/app`)
- Platform persistence: `git status` / commit / push on the Legal AI clone

Same process, different trees → clean porcelain → no commit/push.

### Implementation

- `mission_control/workspace.py`: `resolve_mission_clone_url` selects the clone
  URL from `repository.name` (optional `MISSION_CONTROL_REPOSITORY_URL_MAP`,
  Mission Control aliases → `nhpcorp35/mission-control`, else legacy
  `MISSION_CONTROL_REPOSITORY_URL`). Workspace paths are `realpath`-canonicalized
  so agent and persistence share one checkout. Isolation model unchanged
  (temp clone; agent `stage_changes`/`commit`/`push` remain false).
- Executor timeout/lifecycle fix (process-group kill + bounded cleanup
  communicate + worker terminal-status guarantee) included in the same ship
  because it was verified but not yet on `main`.

### Tests executed

```text
/tmp/mc-venv/bin/python -m unittest \
  tests.test_workspace \
  tests.test_executor \
  tests.test_execution_lifecycle \
  tests.test_lifecycle_instrumentation \
  tests.test_run_persistence \
  tests.test_runs_api \
  tests.test_api \
  -v
# Ran 126 tests — OK
```

### Next Objective

Confirm Railway redeploy of `mission-control` picks up the push; Mission Control
named missions must clone Mission Control and persist agent edits.

## 2026-08-05 — Stuck `running` runs after agent timeout cleanup hang

### Objective

Mission Control runs could remain in `running` indefinitely with empty
stdout/stderr/summary/commit after the Cursor Agent wall-clock timeout,
because timeout cleanup blocked forever on open stdio pipes.

### Root cause

On `subprocess.TimeoutExpired`, the executor called `proc.kill()` then
**unbounded** `proc.communicate()`. Cursor Agent grandchildren that still held
stdout/stderr prevented EOF, so the queue worker never returned, never stored
results, and never moved the run to `timed_out` / `failed` / `completed`.

### Implementation

- `mission_control/executor.py`: start the agent in a new session; on timeout
  SIGKILL the process group; bound cleanup `communicate()` to
  `CLEANUP_TIMEOUT_SECONDS`; if cleanup still times out, return a timed-out
  `ExecutionResult` with any partial output instead of hanging.
- `app/api.py` `_execute_queued_run`: if the worker exits while the run is
  still non-terminal, force `failed` with a stored error (does not weaken
  approvals or execution permissions).

### Tests executed

```text
/tmp/mc-venv/bin/python -m unittest \
  tests.test_executor \
  tests.test_execution_lifecycle \
  tests.test_lifecycle_instrumentation \
  tests.test_workspace \
  tests.test_run_persistence \
  tests.test_runs_api \
  tests.test_api \
  -v
# Ran 118 tests — OK
```

### Resulting commit

Not committed / not pushed (mission constraints forbid git staging, commits,
and pushes). Working tree holds the focused fix only on:
`mission_control/executor.py`, `app/api.py`, `tests/test_executor.py`,
`tests/test_execution_lifecycle.py`, and this log entry.

### Railway / production

Cannot verify Railway deployment until the fix is committed and pushed to
`nhpcorp35/mission-control` `main` under a mission that permits git + deploy.

### Next Objective

Commit and push the executor/queue terminal-status fix to production Mission
Control, then confirm previously stuck timeout paths surface as `timed_out`
with observable lifecycle logs (`subprocess_completed` /
`subprocess_cleanup_timeout`).

## 2026-07-28 — Structured mission persistence defaults (mutation → push)

### Objective

Natural-language / structured repository-mutating missions were resolving to
`persistence.mode: none` when the caller omitted `persistence_mode`, so
successful file-creation runs never reached GitHub / HAL Sync / Obsidian.

### Implementation

- `mission_control/mission_builder.py`:
  `resolve_structured_persistence_mode` — omitted mode → `push` when
  create/modify/delete flags are set; otherwise `none`. Explicit modes are
  never overridden. Raw YAML omitted-`persistence` default remains `none`.
- `POST /runs/structured` and MCP `submit_structured_run` take optional
  `persistence_mode` (`null`/omitted → infer). Platform-push approval checks
  are unchanged.
- Docs: `MISSION_CONTROL_API.md`, `docs/HAL_OPERATOR.md`,
  `docs/CANONICAL_MISSION_SCHEMA.md`.

### Tests executed

```text
/app/.venv/bin/python -m unittest \
  tests.test_mission_builder \
  tests.test_structured_runs_api \
  tests.test_mcp_wait_for_run \
  tests.test_mcp_transport_discovery \
  tests.test_canonical_mission_schema_docs \
  tests.test_runs_api \
  tests.test_api \
  tests.test_validate_regression \
  -v
# Ran 141 tests — OK
```

### Authoritative persistence outcome

Structured create/modify missions without an explicit mode now report
`persistence.mode: push` in the generated Mission Spec (and therefore in the
final authoritative run result after a successful approved push). Explicit
`none` / `commit` / `push` remain as submitted. Read-only structured missions
still default to `none`.

### Next Objective

Ensure HAL / ChatGPT pairs inferred-push mutations with platform-push approval
on routine repository changes.

## 2026-07-28 — HAL Sync Service (macOS launchd auto-sync)

### Objective

Build a safe, maintainable macOS repository auto-sync utility under
`tools/hal-sync-service/` so Allen’s local clones (starting with
`/Users/allenk/Desktop/Mission-Control`) stay fast-forwarded from `origin/main`
via launchd — without sudo, tokens, inbound ports, merges, or destructive Git.

### Implementation

- `sync.sh`: validates absolute Git worktrees on `main` with `origin`; skips
  dirty trees; `git fetch origin`; updates only with `git pull --ff-only origin
  main` when `origin/main` is ahead; directory lock; bounded log rotation.
- `install.sh` / `uninstall.sh`: user LaunchAgent install, status, restart,
  uninstall; creates `config.env` from example only when missing; validates
  commands and repos; `bootstrap`/`bootout` with `load`/`unload` fallback.
- `config.env.example`, `launchd/com.nhpcorp.hal-sync.plist.template`,
  service `README.md`.
- `.gitignore`: `tools/hal-sync-service/logs/` and `config.env`.
- Operator pointer in `docs/HAL_OPERATOR.md`.
- Focused tests: `tests/test_hal_sync_service.py` (local Git fixtures only).

### Tests executed

```text
bash -n tools/hal-sync-service/sync.sh
bash -n tools/hal-sync-service/install.sh
bash -n tools/hal-sync-service/uninstall.sh
# syntax ok

/app/.venv/bin/python -m unittest tests.test_hal_sync_service -v
# Ran 11 tests — OK
```

Shared Mission Control Python behavior was not changed; no additional suite run
beyond the focused sync-service tests.

### Resulting commit

Not committed in this mission (constraints forbid git staging/commits/pushes).

### Limitations

- **launchd was not loaded in this Linux Mission Control environment.** Final
  install and LaunchAgent verification must occur on Allen’s Mac:
  `cd tools/hal-sync-service && ./install.sh install && ./install.sh status`.
- Paths with embedded spaces are not supported in `HAL_SYNC_REPOS` (use
  space/newline-separated absolute paths without spaces).
- Sync never contacts GitHub with embedded credentials; remotes must already
  work with the user’s existing Git auth.

### Next Objective

On Allen’s Mac: install the LaunchAgent, confirm `logs/hal-sync.log` after one
interval, and keep `config.env` local (gitignored).

## 2026-07-28 — First-class documentation policy support

### Objective

Make documentation review an explicit, validated Mission Spec policy and an
authoritative structured run-result field, instead of relying only on free-form
instructions.

### Implementation

- Optional top-level `documentation.mode`: `none` | `required` (default `none`
  when omitted / null) with machine-readable validation errors for unsupported
  modes.
- When `required`, Cursor agent instructions include an explicit Documentation
  section; when `none` / omitted, those instructions are not added.
- Async structured results expose `result.documentation.{mode,status}` with
  statuses `not_requested`, `updated`, `not_required`, and `failed`.
- `updated` vs `not_required` uses a deterministic `files_changed` path
  heuristic (`docs/` or `.md`); agent stdout is never treated as verified
  documentation evidence.
- Read-only execute missions remain valid with documentation omitted or `none`.
- Persistence / push-approval behavior unchanged.

### Tests executed

```text
/app/.venv/bin/python -m unittest tests.test_documentation_policy \
  tests.test_validate_regression tests.test_executor \
  tests.test_structured_run_results tests.test_canonical_mission_schema_docs \
  tests.test_structured_runs_api -v
# Ran 95 tests — OK

/app/.venv/bin/python -m unittest tests.test_workspace tests.test_api \
  tests.test_runs_api tests.test_mission_builder tests.test_run_persistence -v
# Ran 91 tests — OK
```

### Resulting commit

Not committed in this mission (constraints forbid git staging/commits/pushes).

### Limitations

- Documentation path heuristic does not cover non-`.md` docs outside `docs/`.
- Sync `POST /run` / legacy `POST /execute` do not emit structured documentation
  evidence (async `POST /runs` lifecycle only).

### Next Objective

Optional follow-up: richer documentation path classification or builder field
for `documentation.mode` on structured mission submission.

## 2026-07-24 — ChatGPT Actions openapi version must be 3.1.0

### Objective

Eliminate the final Custom GPT Actions importer validation error after
operations already imported successfully:

```text
('openapi',): Input should be '3.1.1' or '3.1.0'
```

### Root cause

The precise offending construct was the **top-level** document field set by
`ACTIONS_OPENAPI_VERSION = "3.0.3"` in `mission_control/openapi_actions.py`
(served by `GET /openapi-actions.json`).

ChatGPT Actions validates `openapi` with a pydantic constraint that only
accepts `"3.1.0"` or `"3.1.1"`. Declaring `"3.0.3"` (from the earlier
3.0-oriented transform) fails that check even when:

- operations are discovered and listed
- `servers`, bearer auth, and operationIds are otherwise fine
- `/openapi.json` already shows `"openapi": "3.1.0"` (that endpoint is not
  what Actions imports)

There was **no** nested object with an `openapi` key; a walk of the Actions
schema finds a single `openapi` declaration at the document root.

### Implementation

- Set `ACTIONS_OPENAPI_VERSION` to `"3.1.0"`.
- Keep existing Actions sanitizations (nullable form, `$ref`/composition
  cleanup, description length, `HealthResponse`, etc.).
- Regression in `tests/test_openapi_actions.py` asserts the version is in
  `{3.1.0, 3.1.1}`, not `3.0.x`, and that `openapi` appears only at the root.

### Import URL for Allen

```text
https://mission-control-production-76ff.up.railway.app/openapi-actions.json
```

Verify: Actions → Import from URL → no `('openapi',): Input should be…`
error; Bearer auth; operations `submit_run`, `get_run`, `wait_for_run`,
`submit_and_wait` present.

### Tests executed

```text
/app/.venv/bin/python -m unittest tests.test_openapi_actions -v
# Ran 16 tests — OK
```

### Resulting commit

Not committed in this mission (constraints forbid git staging/commits/pushes).
Suggested message: Fix ChatGPT Actions openapi version to 3.1.0

### Limitations

- Version string and other sanitizations apply only to `/openapi-actions.json`.
- Production serves the fix only after deploy.

### Next Objective

After deploy, Allen re-imports the Actions URL and confirms a fully clean
import (no remaining validation errors).

## 2026-07-24 — ChatGPT Actions importer description + health schema fixes

### Objective

Clear remaining Custom GPT Actions importer failures for
`/openapi-actions.json`: operation descriptions ≥ 300 characters, and the
inline `/health` response schema.

### Finding

After the OpenAPI 3.0 transform landed, Actions still rejected:

- Long FastAPI `description` strings on `submit_run`, `submit_and_wait`,
  `get_run`, `retry_run`, and `wait_for_run` (Actions limit: under 300 chars).
- The FastAPI-generated inline `/health` schema
  (`type: object` + `additionalProperties: {type: string}`).

Runtime handlers and `/openapi.json` were already correct.

### Implementation

- `mission_control/openapi_actions.py` now shortens Actions operation
  descriptions (curated text under 300 chars, with a clamp safety net) and
  replaces `/health`’s inline schema with named component `HealthResponse`.
- Regression coverage in `tests/test_openapi_actions.py` for the length limit
  and health `$ref` / component shape.
- Import verification steps in `MISSION_CONTROL_API.md` and
  `docs/HAL_OPERATOR.md`.

### Import URL for Allen

```text
https://mission-control-production-76ff.up.railway.app/openapi-actions.json
```

Verify: Actions → Import from URL → clean import; Bearer auth; operations
`submit_run`, `get_run`, `wait_for_run`, `submit_and_wait` present. Do not
import `/openapi.json`.

### Tests executed

```text
/app/.venv/bin/python -m unittest tests.test_openapi_actions -v
# Ran 16 tests — OK
```

### Resulting commit

Not committed in this mission (constraints forbid git staging/commits/pushes).
Suggested message: Fix ChatGPT Actions description and health schema import

### Limitations

- Description shortening and `HealthResponse` apply only to the documentation
  `/openapi-actions.json` view; `/openapi.json` keeps full FastAPI text and the
  original inline health schema.
- Production serves the fix only after deploy.

### Next Objective

After deploy, Allen re-imports the Actions URL and confirms a clean import.

## 2026-07-24 — Custom GPT Actions–compatible OpenAPI schema

### Objective

Make the Mission Control OpenAPI document importable by ChatGPT Custom GPT
Actions without changing runtime API behavior.

### Finding

`GET /openapi.json` is valid OpenAPI **3.1.0** with a correct absolute HTTPS
`servers` entry, but the Custom GPT Actions importer is OpenAPI 3.0-oriented.
Importer-sensitive constructs in the live schema caused parse failure; the
editor then reported the misleading error
`Could not find a valid URL in \`servers\``.

Rejected / fragile constructs present in the FastAPI 3.1 document included:

- `anyOf` unions with `{type: null}` (OAS 3.1 nullable style)
- Response schemas combining `$ref` with sibling `oneOf` (notably
  `submit_and_wait`)
- Empty array `items: {}` (structured deliverables)
- Title-only unconstrained schemas (e.g. `ValidationError.input`)

### Implementation

- Added `mission_control/openapi_actions.py` to transform the generated schema
  into an Actions-compatible OpenAPI **3.0.3** view (nullable → OAS 3.0
  `nullable`, collapse `oneOf` / non-null `anyOf`, fix empty items /
  unconstrained schemas, keep a single HTTPS server URL).
- Exposed documentation-only `GET /openapi-actions.json` while preserving
  `GET /openapi.json` for normal clients.
- Regression tests in `tests/test_openapi_actions.py`.
- Documented the import URL in `MISSION_CONTROL_API.md` and
  `docs/HAL_OPERATOR.md`.

### Import URL for Allen

```text
https://mission-control-production-76ff.up.railway.app/openapi-actions.json
```

Operation IDs preserved: `submit_run`, `get_run`, `wait_for_run`,
`submit_and_wait` (plus existing Mission Control operations). Auth: HTTP Bearer.

### Tests executed

```text
/app/.venv/bin/python -m unittest tests.test_openapi_actions -v
# Ran 14 tests — OK
```

### Resulting commit

Not committed in this mission (constraints forbid git staging/commits/pushes).
Suggested message: Add Custom GPT compatible OpenAPI schema

### Limitations

- `/openapi-actions.json` is documentation-only; it does not change runtime
  handlers. Union response shapes (e.g. `submit_and_wait` rejection vs wait
  payload) are collapsed to the primary branch for importer compatibility.
- Production will serve the new endpoint only after this change is deployed.

### Next Objective

After deploy, Allen imports the Actions URL above into the Hal-Cursor Custom GPT
and verifies `submit_and_wait` / `wait_for_run` discovery.

## 2026-07-24 — Expose wait operations through REST API

### Objective

Expose `wait_for_run` and `submit_and_wait` through the Mission Control REST
OpenAPI schema so the Hal-Cursor Custom GPT Action can discover and invoke
them (alongside the existing MCP tools).

### Finding

`POST /runs/{run_id}/wait` (`wait_for_run`) already existed. Custom GPT Actions
that import REST OpenAPI still lacked a one-shot submit-and-wait HTTP operation
equivalent to MCP `submit_and_wait`.

### Implementation

- Kept `POST /runs/{run_id}/wait` with OpenAPI operation ID `wait_for_run`;
  extracted shared `_wait_for_run` helper; response includes `timeout_seconds`.
- Added `POST /runs/submit-and-wait` with OpenAPI operation ID
  `submit_and_wait`, reusing `_accept_async_run` + `_wait_for_run`.
- Submission/validation failures return immediately without waiting.
- Updated `MISSION_CONTROL_API.md` and `docs/HAL_OPERATOR.md`.
- Regression coverage for wait success/terminal/timeout, submit-and-wait
  success, immediate validation failure, authentication, and OpenAPI
  operation discovery (including transport/discovery tests).

### Tests executed

```text
/app/.venv/bin/python -m unittest \
  tests.test_wait_for_run \
  tests.test_mcp_transport_discovery \
  -v
# Ran 20 tests — OK
```

### Resulting commit

Not committed in this mission (constraints forbid git staging/commits/pushes).
Suggested message: Expose wait operations through REST API

### Limitations

- REST `submit_and_wait` covers exact YAML only; structured missions still use
  `POST /runs/structured` + `wait_for_run`.
- MCP connector continues to poll `GET /runs/{run_id}` for its own
  `wait_for_run` / `submit_and_wait` tools (REST wait endpoints are for HTTP /
  Custom GPT Actions).

### Next Objective

Point the Hal-Cursor Custom GPT Action OpenAPI import at the updated schema
and prefer `submit_and_wait` for exact-YAML end-to-end calls.

## 2026-07-24 — Add submit_and_wait MCP operation

### Objective

Add a Mission Control MCP tool that accepts exact mission YAML, submits it,
and waits for the resulting run to reach a terminal state in one tool call,
so HAL can run a normal mission end-to-end without involving Allen except for
genuine approval, decision, or unrecoverable failure.

### Finding

Submit and wait already existed as separate authenticated paths
(`submit_run` / `POST /runs` and connector-side `wait_for_run` polling
`get_run`). HAL previously needed two MCP calls for exact YAML end-to-end.

### Implementation

- Added `MissionControlClient.submit_and_wait` reusing `submit_run` +
  `wait_for_run` (same timeout/poll validation and limits; validate before
  submit).
- Added MCP tool `submit_and_wait`; updated `EXPECTED_TOOL_NAMES` and server
  instructions.
- Submission failures return the existing structured rejection without
  waiting; wait expiry returns `run_id` + latest run fields with
  `wait_expired: true`.
- Updated `MISSION_CONTROL_API.md` and `docs/HAL_OPERATOR.md`.
- Regression coverage for success, submission failure, terminal-immediate,
  and wait expiration.

### Tests executed

```text
/app/.venv/bin/python -m unittest \
  tests.test_mcp_wait_for_run \
  tests.test_mcp_transport_discovery \
  -v
# Ran 33 tests — OK
```

### Resulting commit

Not committed in this mission (constraints forbid git staging/commits/pushes).
Platform persistence may commit/push after agent completion.
Suggested message: Add submit and wait MCP operation

### Limitations

- `submit_and_wait` covers exact YAML only; routine structured missions still
  use `submit_structured_run` + `wait_for_run`.
- Railway edge / upstream client deadlines still apply to long silent waits;
  resume with `wait_for_run` and the same `run_id`.

### Next Objective

Use `submit_and_wait` for exact-YAML HAL loops; involve Allen only for
approval, decision, or unrecoverable failure.

## 2026-07-24 — Remove wait_for_run 25-second cutoff

### Objective

Honor MCP `wait_for_run` caller-requested timeouts (for example 900s)
end-to-end instead of returning after ~25s while the run is still
non-terminal.

### Finding

The artificial cutoff lived in the MCP connector only:
`MCP_WAIT_MAX_TIMEOUT_SECONDS = 25.0` in `mcp_connector/client.py`, which
clamped budgets such as 900s before the poll loop. Per-request httpx timeout
(`MISSION_CONTROL_TIMEOUT_SECONDS`, default 30s) applies only to each
`get_run` call and does not bound the wait loop. Railway’s public edge closes
HTTP requests after 5 minutes with no data transferred, or 15 minutes with
keep-alive traffic — a platform constraint outside the former 25s app cap.

### Implementation

- Raised `MCP_WAIT_MAX_TIMEOUT_SECONDS` to `3600` (aligned with
  `POST /runs/{run_id}/wait`); requested values such as 900 are preserved.
- Kept authenticated `get_run` polling, immediate terminal return, structured
  `wait_expired` payloads, and `poll_interval_seconds` behavior.
- Updated MCP tool instructions, `MISSION_CONTROL_API.md`, and
  `docs/HAL_OPERATOR.md` with connector bounds and Railway edge limits.
- Added regression
  `test_timeout_above_former_25s_cap_is_honored`.

### Tests executed

```text
/app/.venv/bin/python -m unittest \
  tests.test_mcp_wait_for_run \
  tests.test_wait_for_run \
  -v
# Ran 27 tests — OK
```

### Resulting commit

Not committed in this mission (constraints forbid git staging/commits/pushes).
Platform persistence may commit/push after agent completion.

### Limitations

- A silent Streamable HTTP MCP tool response can still be cut by Railway’s
  5-minute idle / 15-minute absolute edge limits before a long application
  budget finishes; resume with the same `run_id`.
- Upstream MCP clients may impose their own tool-call deadlines.

### Next Objective

Prefer long requested `wait_for_run` budgets when appropriate; on
`wait_expired` or transport interrupt, resume the same `run_id`.

## 2026-07-24 — Reconcile persistence reporting (retry)

### Objective

Retry of persistence-reporting reconciliation: ensure the client-facing run
summary stays authoritative and consistent with platform persistence, including
wait-path responses and HAL flow docs.

### Finding

Confirmed: platform Git persistence still runs **after** agent completion in
`execute_registered_run`. The prior pass already added `summary` /
`finalize_structured_summary` and was platform-persisted as
`396762a078064ee6110f641c14030932d534b833` even though agent stdout claimed no
commit/push — illustrating the exact discrepancy this work reconciles.

### Implementation

- Kept existing `build_run_summary` / `finalize_structured_summary` behavior
  and persistence sequencing unchanged.
- Aligned remaining `MISSION_CONTROL_API.md` HAL flow / wait_for_run guidance
  to prefer `summary` / `result.persistence` / `commit_sha` over agent stdout.
- Added wait-path regression:
  `test_wait_for_run_summary_matches_platform_persistence`.

### Tests executed

```text
/mise/installs/python/3.13.14/bin/python -m unittest \
  tests.test_structured_run_results \
  tests.test_runs_api \
  tests.test_wait_for_run \
  tests.test_execution_lifecycle \
  tests.test_mcp_transport_discovery \
  -v
# Ran 52 tests — OK
```

### Resulting commit

Not committed in this mission (constraints forbid git staging/commits/pushes).
Prior platform persistence of the initial reconcile:
`396762a078064ee6110f641c14030932d534b833`.
Push confirmation: not pushed by this agent (same constraints); platform
persistence may commit/push after agent completion.

### Limitations

- Legacy terminal rows stored before `summary` may lack it until re-run.
- Agent stdout remains unmodified diagnostic text.

### Next Objective

Prefer `summary` over agent stdout when judging async-run persistence.

## 2026-07-24 — Reconcile persistence reporting

### Objective

Resolve the apparent conflict where structured run results report successful
platform persistence while Cursor agent stdout reports that no commit or push
occurred, and make the client-facing run summary authoritative.

### Finding

Not a persistence bug: platform Git persistence runs **after** successful
agent completion (and deliverable checks) inside `execute_registered_run`.
Agent constraints forbid agent-side commit/push, so stdout that says no
commit/push occurred can be true for the agent while `result.persistence`
still records a successful platform outcome.

### Implementation

- Added Mission Control-authored `result.summary` via `build_run_summary` /
  `finalize_structured_summary` in `mission_control/run_result.py`.
- Finalize the summary on every terminal store path in
  `execute_registered_run`; when persistence was attempted, also warn that
  agent stdout predates platform persistence.
- Expose top-level `summary` on `GET /runs/{run_id}` (mirrors
  `result.summary`); keep raw agent `stdout` unchanged for diagnostics.
- Document the trust boundary in `MISSION_CONTROL_API.md` and MCP server
  instructions.

### Tests executed

```text
/mise/installs/python/3.13.14/bin/python -m unittest \
  tests.test_structured_run_results \
  tests.test_runs_api \
  tests.test_wait_for_run \
  tests.test_execution_lifecycle \
  tests.test_mcp_transport_discovery \
  -v
# Ran 51 tests — OK
```

### Resulting commit

Agent stdout of the original pass claimed no commit; platform persistence
later recorded `396762a078064ee6110f641c14030932d534b833` (prefer
`summary` / `commit_sha` over that stdout claim).

### Limitations

- Legacy terminal rows stored before this change may lack `summary` until
  re-run.
- Agent stdout is intentionally left unmodified; reconciliation is via
  `summary` / `result.persistence` / `commit_sha`.

### Next Objective

Treat `summary` as the client-facing persistence narrative for async runs.

## 2026-07-24 — Structured mission submission (Mission Builder API)

### Objective

Let HAL submit structured mission fields instead of hand-authored raw YAML,
while preserving `submit_run` / `POST /runs` and existing validation/execution.

### Implementation

- Added `mission_control/mission_builder.py` to build Mission Spec v1.0 with
  safe execute defaults and render YAML via `yaml.safe_dump`.
- Added authenticated `POST /runs/structured` that renders YAML then calls the
  existing `_accept_async_run` path (same auth, recursive-submission gate,
  acceptance and rejection shapes as `POST /runs`).
- Added MCP client/server tool `submit_structured_run`; kept `submit_run`
  unchanged; updated discovery/`EXPECTED_TOOL_NAMES` and server instructions.
- Documented in `MISSION_CONTROL_API.md` and `docs/HAL_OPERATOR.md` (prefer
  structured for routine missions; raw YAML remains supported).

### Tests executed

```text
/mise/installs/python/3.13.14/bin/python -m unittest \
  tests.test_mission_builder \
  tests.test_structured_runs_api \
  tests.test_mcp_transport_discovery \
  tests.test_mcp_wait_for_run \
  tests.test_runs_api \
  tests.test_api \
  -v
# Ran 72 tests — OK
```

### Resulting commit

`feat: add structured mission submission`

### Limitations

- v1 structured surface does not cover plan-mode or arbitrary Mission Spec
  overrides; use raw YAML for those cases.
- Builder-controlled execute defaults cannot be overridden via structured
  fields.

### Next Objective

Use `submit_structured_run` / `POST /runs/structured` for routine HAL execute
missions; reserve raw YAML for exceptional documents.

## 2026-07-23 — Retry failed async runs

### Objective

Add a minimal `POST /runs/{run_id}/retry` operation that creates a new async
run from the exact stored mission YAML of a terminal failed run.

### Implementation

- Persist `mission_yaml` and `retried_from` on SQLite run records (ALTER TABLE
  migration for existing registries).
- `POST /runs` stores the submitted YAML; retry reuses that exact text through
  the shared `_accept_async_run` submission pipeline (validate, preflight,
  queue) with a fresh `run_id` and workspace lifecycle.
- Only status `failed` may be retried; other statuses and missing YAML return
  `409`; unknown source returns `404`; acceptance returns `202` like
  `POST /runs`.
- Expose `retried_from` on `GET /runs/{run_id}` / OpenAPI; document in
  `MISSION_CONTROL_API.md` and `docs/CANONICAL_MISSION_SCHEMA.md`.

### Tests executed

```text
/mise/installs/python/3.13.14/bin/python -m unittest \
  tests.test_retry_run \
  tests.test_runs_api \
  tests.test_run_registry \
  tests.test_run_persistence \
  tests.test_api \
  tests.test_structured_run_results \
  tests.test_execution_lifecycle \
  tests.test_workspace \
  tests.test_wait_for_run \
  tests.test_lifecycle_instrumentation \
  -v
# Ran 140 tests — OK
```

### Resulting commit

Not committed in this mission (constraints forbid git staging/commits/pushes).

### Limitations

- Legacy failed rows without stored `mission_yaml` cannot be retried (`409`).
- No automatic retry policy, mission editing, retry counters, or MCP tool yet.
- Retry re-validates and re-preflights through the same pipeline as submit.

### Next Objective

Use `POST /runs/{run_id}/retry` for manual recovery of failed async runs; add
MCP exposure only if HAL operators need it in-connector.

## 2026-07-23 — File vs descriptive deliverables

### Objective

Fix false `Missing declared file deliverable` failures when a descriptive
deliverable contains a `/` (for example `API/OpenAPI documentation updates`),
as exposed by run `f5a1e020-3131-49df-974a-0eb689f45735`.

### Implementation

- Prefer explicit typed deliverable entries for new missions:
  `file: <path>` / `kind: file` + `path:` versus `description:` /
  `kind: descriptive`.
- Keep bare-string deliverables compatible with a tightened heuristic: a string
  is path-like when it has a short alphanumeric extension, or contains `/`
  **without** whitespace (absolute forms still classified so they can be
  rejected safely). Slash-containing prose with whitespace is descriptive.
- Only file deliverables are filesystem-checked; structured run-result
  evidence (`DeliverableEvidence`) remains intact.

### Tests executed

```text
/mise/installs/python/3.13.14/bin/python -m unittest \
  tests.test_workspace.TestDeclaredFileDeliverables -v
# Ran 12 tests — OK

/mise/installs/python/3.13.14/bin/python -m unittest \
  tests.test_workspace \
  tests.test_canonical_mission_schema_docs \
  tests.test_structured_run_results \
  tests.test_runs_api \
  tests.test_wait_for_run \
  tests.test_validate_regression \
  tests.test_execution_lifecycle \
  tests.test_run_registry \
  tests.test_run_persistence \
  tests.test_api -v
# Ran 155 tests — OK
```

### Resulting commit

Not committed in this mission (constraints forbid git staging/commits/pushes).

### Limitations

- Structural validation still does not type-check deliverable list items;
  unknown mapping shapes are skipped by the filesystem gate.
- Agent prompt rendering still stringifies mapping entries via `str(item)` in
  `build_cursor_instruction` (unchanged by this mission).
- Extension-less relative paths that contain whitespace still require typed
  `file:` to be verified (bare-string heuristic will not treat them as paths).

### Next Objective

Use typed `file:` / `description:` deliverables on new missions; revisit
prompt formatting only if mapping entries become common in production YAML.

## 2026-07-23 — Structured run results


### Objective

Make `GET /runs/{run_id}` return objective, machine-readable execution and
verification evidence so HAL does not need to parse Cursor prose stdout.

### Implementation

- Added typed `StructuredRunResult` models in `mission_control/run_result.py`
  (`files_changed`, `commands`, `test_counts`, `deliverables`, `persistence`,
  `warnings`).
- Collect evidence in `execute_registered_run` from Mission Control records and
  workspace Git status; persist as `result_json` in the SQLite run registry.
- Expose `result` on `RunStatusResponse` / OpenAPI with a completed-run example;
  document the trust boundary in `MISSION_CONTROL_API.md`.
- Preserve all existing response fields; keep `stdout` / `stderr` unchanged for
  diagnostics. Unavailable evidence is `null`, empty, or warned—never fabricated.
- Failed/timed-out paths retain partial evidence actually collected.

### Tests executed

```text
mise exec python -- python -m unittest \
  tests.test_structured_run_results \
  tests.test_runs_api \
  tests.test_run_registry \
  tests.test_run_persistence \
  tests.test_workspace \
  tests.test_wait_for_run \
  tests.test_execution_lifecycle \
  tests.test_api -v
```

Outcome: **123 tests OK** (including new structured-result regressions).

### Resulting commit

`d472be59c5d56e7b4652f5d904098fa8846e9353` on `main`.

### Limitations

- Aggregate `test_counts` remain `null`; Mission Control does not parse agent
  stdout for test harness summaries.
- No separate Mission Control verification shell commands exist yet; only the
  Cursor agent subprocess and platform checks appear under `commands` /
  deliverable / persistence evidence.
- `files_changed` comes from `git status --porcelain` in the isolated workspace
  before cleanup; it is empty when status cannot be read (with a warning).

### Next Objective

Prefer `summary`, `result.persistence`, and `commit_sha` over agent stdout
when verifying async Mission Control persistence outcomes. Platform
persistence runs after the agent completes.

## 2026-07-23 — Mission Control operator baseline

### Objective

Publish durable HAL operating procedure and operator log; record verified Mission
Control execution-engine facts before returning primary product focus to LegalAI.

### Verified Outcomes

- Async runs use fresh isolated workspaces that are cleaned up after execution.
- `persistence.mode: none` is not visible to later runs.
- `persistence.mode: commit` is not visible to later runs because the temporary
  workspace is discarded.
- `persistence.mode: push` is visible to later runs.
- Canonical mission schema documentation exists at
  `docs/CANONICAL_MISSION_SCHEMA.md`.
- Deliverable verification was implemented for async `POST /runs` and verified
  on `main` at commit `16d640583e902fa2ea0008dc20457f417d6af358`.
- Missing declared file deliverables fail the run before persistence with the
  error `Missing declared file deliverable: <path>`.
- Mission Control is considered stable enough to serve as HAL's execution engine,
  with future Mission Control work limited to blocking defects or strategic work
  that removes the user from text loops.
- Primary product focus returns to LegalAI.

### Architectural Decisions

- Treat repository state as the sole source of truth for significant claims.
- Require `docs/HAL_OPERATOR_LOG.md` updates as the final verified step of
  significant Mission Control objectives (see `docs/HAL_OPERATOR.md`).
- Prefer declaring `docs/HAL_OPERATOR_LOG.md` as a file deliverable on
  repository-changing missions; do not hard-code that path in validation.

### Lessons Learned

- Platform persistence visibility is determined by whether changes reach the
  shared remote before the isolated workspace is discarded; only `push`
  survives across runs.
- Path-like deliverable checks catch missing files before persistence and before
  a run can be marked completed.

### Open Issues

- None recorded for this baseline entry.

### Next Objective

- Execute LegalAI product work; revisit Mission Control only for blocking
  defects or strategic automation that removes the user from text loops.
