# HAL Operator Log

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

Prefer `result` over agent stdout when verifying async Mission Control runs.

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
