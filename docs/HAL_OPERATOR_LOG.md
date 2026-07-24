# HAL Operator Log

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
