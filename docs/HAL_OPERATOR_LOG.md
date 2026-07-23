# HAL Operator Log

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

Pending commit SHA on `main` after this log entry is included in the commit.

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
