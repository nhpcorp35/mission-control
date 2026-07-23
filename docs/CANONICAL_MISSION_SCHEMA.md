# Canonical Mission Schema

Authoritative Mission Control mission contract derived from the current
implementation (`mission_control/validator.py`, `mission_control/workspace.py`,
`mission_control/executor.py`, `app/api.py`), regression tests, and
`MISSION_SPEC.md` / `MISSION_CONTROL_API.md`.

Specification version supported by structural validation: **`1.0`**.

---

## 1. Top-level keys

### Required (structural validation)

These keys must be present. Absence fails `validate_mission` /
`POST /validate` with `Missing required keys: …`:

| Key | Required | Notes |
| --- | --- | --- |
| `version` | yes | Must normalize to `"1.0"` (string `"1.0"` or float `1.0`) |
| `mission_id` | yes | Presence only at structural layer; format not enforced |
| `title` | yes | Presence only at structural layer |
| `repository` | yes | Must be a mapping for run/execute eligibility |
| `execution` | yes | Must be a mapping for run/execute eligibility |
| `permissions` | yes | Must be a mapping for run/execute eligibility |
| `instructions` | yes | Presence only at structural layer |
| `deliverables` | yes | Presence only at structural layer; empty list `[]` is allowed |
| `approval` | yes | Presence only at structural layer |

### Optional

| Key | Required | Default / behavior |
| --- | --- | --- |
| `persistence` | no | When omitted, platform persistence mode is `none` |

Unknown top-level keys are ignored by structural validation (not rejected).

Nested field shapes (`repository.*`, `execution.*`, `permissions.*`,
`approval.*`) are **not** fully schema-checked at the structural layer. They
are enforced by run-eligibility (`validate_mission_for_run`), execute-
eligibility (`validate_mission_for_execute`), and the persistence layer as
described below.

---

## 2. `execution.mode`

### Declared in Mission Spec

`MISSION_SPEC.md` documents three modes: `ask`, `plan`, `execute`.

### Enforced by Mission Control today

| Mode | Structural validation | `POST /run` / `mc.py run` | `POST /runs` / `POST /execute` | Cursor CLI invocation |
| --- | --- | --- | --- | --- |
| `plan` | accepted (mode not checked structurally) | **required** | rejected (`expected execute`) | Eligibility requires `plan`; the sync runner calls Cursor with `--mode ask` |
| `execute` | accepted | rejected (`expected plan`) | **required** | Cursor invoked with `--force` (no `--mode`) |
| `ask` | accepted | rejected (`expected plan`) | rejected (`expected execute`) | Supported by `build_cursor_agent_command`, but no HTTP/CLI path accepts mission `mode: ask` |

Semantics in practice:

- **Inspection / planning:** submit `execution.mode: plan` to `POST /run` (or
  `mc.py run`). Agent permissions must be read-only (see permissions matrix).
- **Mutation:** submit `execution.mode: execute` to `POST /runs` (preferred) or
  legacy `POST /execute`.

### Other `execution` fields

| Field | Behavior |
| --- | --- |
| `agent` | Run/execute eligibility require `cursor` |
| `worktree` | Must be false/absent for run and execute; `true` is rejected |
| `sandbox` | Present in reference missions; **not validated** by current code |

---

## 3. Permissions matrix

Permissions are deny-by-default agent controls. They do **not** select platform
Git persistence (`persistence.mode`).

Common permission fields used in reference missions and tests:

| Field | Meaning |
| --- | --- |
| `read` | Agent may read (not enforced by validator) |
| `create_files` | Agent may create files |
| `modify_files` | Agent may modify existing files |
| `delete_files` | Agent may delete files |
| `run_commands` | Agent may run commands (not enforced by validator) |
| `stage_changes` | Agent may `git add` |
| `commit` | Agent may `git commit` |
| `push` | Agent may `git push` |

### Fields that must remain false for run (`plan` / `POST /run`)

`validate_mission_for_run` rejects a mission when any of these is truthy:

- `create_files`
- `modify_files`
- `delete_files`
- `stage_changes`
- `commit`
- `push`

### Fields that must remain false for execute (`POST /runs`, `POST /execute`)

`validate_mission_for_execute` rejects a mission when any of these is truthy:

- `delete_files`
- `stage_changes`
- `commit`
- `push`

Additionally, unless `persistence.mode` resolves to `push`, execute requires at
least one of `create_files` or `modify_files` to be true.

**Push-only exception:** when `persistence.mode` is `push` (and platform-push
approval is present), execute may have both `create_files: false` and
`modify_files: false`. Agent `permissions.push` must still remain `false`.

### Constraint text sent to the agent

On execute, Mission Control appends constraint text based on
`create_files` / `modify_files`:

| `create_files` | `modify_files` | Constraint set |
| --- | --- | --- |
| true | true | create and modify allowed; no deletes / Git / worktrees / recursive missions |
| false | true | modify only |
| true | false | create only (default when neither modify nor both) |
| false | false | create-only constraint text (only valid for push-only execute) |

On plan/run, read-only constraints are always applied.

---

## 4. Persistence

Optional top-level block:

```yaml
persistence:
  mode: none   # or commit | push
```

### Resolution

`resolve_persistence_mode`:

- omitted `persistence` → `none`
- `persistence` present without `mode`, or `mode: null` → `none`
- otherwise the string value of `mode`

Structural validation accepts only `none`, `commit`, and `push` when `mode` is
set to a non-null value. Unsupported values fail with
`Unsupported persistence.mode: …`.

### Exact platform behavior (`persist_workspace_changes`)

Applied after a **successful** agent run on the **isolated workspace** used by
`POST /runs` (`execute_registered_run`):

| Mode | Stage (`git add -A`) | Local commit | Push to `repository.base_branch` | `commit_sha` on success |
| --- | --- | --- | --- | --- |
| `none` | no | no | no | always `null` |
| `commit` | yes if dirty | yes if dirty | no | HEAD SHA if a commit was created; `null` if working tree clean |
| `push` | yes if dirty | yes if dirty | yes (`git push origin HEAD:<base_branch>`) if a commit was created | HEAD SHA if a commit was created; `null` if working tree clean |

Additional rules:

- Dirty-tree check uses `git status --porcelain`. Clean tree → success with
  `commit_sha: null` (no Git mutations).
- Commit message: `Mission Control run <run_id>`.
- `commit` and `push` require `MISSION_CONTROL_GIT_NAME` and
  `MISSION_CONTROL_GIT_EMAIL`.
- `push` additionally requires `GITHUB_TOKEN` and re-checks platform-push
  approval inside the persistence layer.
- Agent `permissions.commit` / `permissions.push` never authorize platform
  persistence.

**Legacy `POST /execute`:** runs synchronously against `repository.path` and
does **not** call `persist_workspace_changes`. Persistence modes have no
platform effect on that path.

---

## 5. Approval fields

Common fields in missions:

| Field | Enforced by code today? | Role |
| --- | --- | --- |
| `execute_without_approval` | no | Documented policy flag only |
| `commit_requires_approval` | no | Documented agent-commit policy only |
| `push_requires_approval` | no | Documented agent-push policy only; **does not** authorize platform push |
| `platform_push_approved` | **yes** | Explicit per-mission platform-push approval |
| `allow_automatic_platform_push` | **yes** | Named policy authorizing automatic platform pushes |

### `platform_push_approved` requirements

When resolved `persistence.mode` is `push`, Mission Control requires **one** of:

- `approval.platform_push_approved: true`
- `approval.allow_automatic_platform_push: true`

Otherwise execute eligibility fails with the machine-readable error:

```text
PLATFORM_PUSH_APPROVAL_REQUIRED: persistence.mode=push requires explicit approval.platform_push_approved=true (or the allow_automatic_platform_push=true policy)
```

The same check runs again in `persist_workspace_changes` so earlier validation
cannot waive the gate.

| Persistence mode | Platform-push approval required? |
| --- | --- |
| `none` | no |
| `commit` | no (and never pushes) |
| `push` | yes |

Truthy checks are strict identity (`is True`); non-boolean truthy values do not
authorize platform push.

---

## 6. Deliverables and completed-run verification

### Requirements

- `deliverables` is a **required** top-level key.
- Structural validation only checks presence.
- An empty list `[]` is structurally valid.
- Non-list / missing items are not type-checked structurally.
- The executor includes deliverable entries in the Cursor instruction prompt
  (or `- (none specified)` when the list is empty/absent at prompt-build time).
  Prefer bare strings or simple typed mappings for readable prompts.

### Entry shapes (file vs descriptive)

Each `deliverables` list item may be:

| Shape | Example | Filesystem check? |
| --- | --- | --- |
| Typed **file** | `file: docs/out.txt` or `kind: file` + `path:` | Yes (if safe relative path) |
| Typed **descriptive** | `description: …` or `kind: descriptive` | No |
| Bare string (compat) | `MISSION_SPEC.md`, `docs/out.txt`, `summary` | Only when path-like (below) |

**Recommended syntax for new missions** — declare intent explicitly:

```yaml
deliverables:
  - file: docs/HAL_OPERATOR_LOG.md
  - file: mission_control/workspace.py
  - description: API/OpenAPI documentation updates
```

Equivalent `kind` form:

```yaml
deliverables:
  - kind: file
    path: docs/HAL_OPERATOR_LOG.md
  - kind: descriptive
    text: API/OpenAPI documentation updates
```

Bare strings remain valid for backward compatibility. Do **not** rely on a
slash inside free-text prose to mean “file path.”

### Operator log deliverable (operational practice)

Repository-changing missions should declare `docs/HAL_OPERATOR_LOG.md` as a
file deliverable (typed `file:` preferred) so the HAL operator log is updated
and verified before completion. This is an operating convention
(`docs/HAL_OPERATOR.md`), not a structural validation requirement.

### Completed-run file verification guarantee (current implementation)

For asynchronous **`POST /runs`** (and the same
`execute_registered_run` lifecycle used by that path), Mission Control
verifies declared **file** deliverables after successful agent execution
and **before** platform persistence / `completed` status:

1. isolated workspace preparation succeeds,
2. `execute_cursor_agent` returns `ok`,
3. each file deliverable exists as a regular file under the workspace
   (or the gate is skipped for descriptive / unsafe entries — see below),
4. `persist_workspace_changes` returns `ok`,

then the temporary workspace is deleted.

If a required file deliverable is missing, the run is stored as
`failed` with error `Missing declared file deliverable: <path>`, persistence
is not invoked, and the run is not marked `completed`.

#### Compatibility rule for bare-string deliverables

A bare-string deliverable is treated as a file-path candidate when it is a
non-empty string without NUL and either:

- has a basename with a short alphanumeric extension matching
  `.[A-Za-z0-9]{1,16}` (for example `MISSION_SPEC.md`, `docs/out.txt`), or
- contains a `/` path separator **and** contains **no** whitespace (for
  example `docs/subdir/file`, `src/app.py`),

including absolute forms (`/…`, `~/…`, Windows drive paths) so they can be
classified as path-like and rejected from workspace checks.

Descriptive deliverables are **not** verified on disk, including:

- words without separators or extensions (`summary`, `report`, `confirmation`),
- multi-word phrases (`repository status`),
- slash-containing prose with whitespace such as
  `API/OpenAPI documentation updates` (a lone `/` in free text is not enough).

Typed `description:` / `kind: descriptive` entries are never checked on disk.
Typed `file:` / `kind: file` entries are always treated as file deliverables
(subject to workspace safety resolution), even when the path string would not
pass the bare-string heuristic.

Unsafe paths (absolute paths, home paths, or resolved paths that escape the
isolated workspace) are **not** read outside the workspace: they are skipped
by the filesystem check and do not fail this gate by themselves.

Limitations:

- Sync **`POST /run`** and legacy **`POST /execute`** do not use this isolated
  lifecycle gate.
- File *content* is not validated—only that a regular file exists at the
  relative path.
- Extension-less filenames without `/` (for example a bare `SUMMARY`) are not
  treated as path deliverables under the bare-string rule; use typed
  `file: SUMMARY` when such a path must be verified.
- Structural validation does not yet reject unknown deliverable mapping keys;
  unknown shapes are skipped by the filesystem gate.

---

## 7. Workspace lifecycle and visibility

For **`POST /runs`** (async execute):

1. Clone `MISSION_CONTROL_REPOSITORY_URL` at `repository.base_branch` into a
   temporary directory (`mission-control-run-*`).
2. Rewrite `repository.path` for that run to the temp workspace.
3. Execute the agent there.
4. Verify declared file deliverables exist as regular files in that
   workspace (fail the run before persistence if any are missing).
5. Apply platform persistence for that workspace.
6. Always attempt `cleanup_workspace` (delete the temp directory) in `finally`.

Consequences:

- Workspaces are **not** shared across runs.
- Later runs always start from a fresh clone of the configured remote/branch
  (plus whatever prior successful `push` persistence already published).
- Local-only `commit` persistence is discarded with the temp workspace unless
  a subsequent mechanism publishes it; only `push` updates the shared remote.
- Sync **`POST /run`** and legacy **`POST /execute`** use `repository.path`
  directly and do not create this isolated clone lifecycle.

---

## 8. Endpoint distinctions (mission submission)

| Endpoint | Auth | Validation | Eligible `execution.mode` | Workspace | Platform persistence |
| --- | --- | --- | --- | --- | --- |
| `POST /validate` | public | structural only | any structurally valid doc | none | none |
| `POST /run` | API key | structural + `validate_mission_for_run` | `plan` | `repository.path` in place | none |
| `POST /execute` | API key | structural + `validate_mission_for_execute` | `execute` | `repository.path` in place (legacy sync) | **not applied** |
| `POST /runs` | API key | structural + `validate_mission_for_execute` | `execute` | isolated clone | applied per `persistence.mode` |
| `GET /runs/{run_id}` | API key | n/a | n/a | n/a | returns `commit_sha` when set |
| `POST /runs/{run_id}/wait` | API key | n/a | n/a | n/a | wait-only; does not mutate run state on timeout |

CLI:

- `mc.py validate` → structural validation
- `mc.py run` → same eligibility as `POST /run` (`plan`)

Plan versus execute summary:

- **Plan / inspection:** `execution.mode: plan`, mutation permissions false,
  `POST /run`.
- **Execute:** `execution.mode: execute`, allowed create/modify (or push-only),
  forbidden delete/stage/commit/push agent flags, prefer `POST /runs` for
  isolated execution + persistence.

Recursive local submissions during an active execution are rejected
(`RECURSIVE_SUBMISSION`).

---

## 9. Minimal valid YAML examples

Paths below use `.` so examples validate when the process cwd is the Mission
Control repository root. Substitute a real absolute path in deployed
environments. All examples set `worktree: false`.

### 9.1 Inspection / planning (`plan` → `POST /run`)

```yaml
version: "1.0"
mission_id: 2026-07-23-plan-001
title: Repository Inspection
repository:
  name: Mission-Control
  path: .
  base_branch: main
execution:
  agent: cursor
  mode: plan
  sandbox: true
  worktree: false
permissions:
  read: true
  create_files: false
  modify_files: false
  delete_files: false
  run_commands: true
  stage_changes: false
  commit: false
  push: false
instructions: |
  Inspect the repository. Do not modify files.
deliverables:
  - summary
approval:
  execute_without_approval: true
  commit_requires_approval: true
  push_requires_approval: true
```

### 9.2 Execute with `persistence.mode: none`

```yaml
version: "1.0"
mission_id: 2026-07-23-exec-none-001
title: Controlled Write None
repository:
  name: Mission-Control
  path: .
  base_branch: main
execution:
  agent: cursor
  mode: execute
  sandbox: true
  worktree: false
permissions:
  read: true
  create_files: true
  modify_files: false
  delete_files: false
  run_commands: true
  stage_changes: false
  commit: false
  push: false
persistence:
  mode: none
instructions: |
  Create docs/example-none.txt with one line of text.
  Update docs/HAL_OPERATOR_LOG.md with verified results.
deliverables:
  - docs/example-none.txt
  - docs/HAL_OPERATOR_LOG.md
approval:
  execute_without_approval: true
  commit_requires_approval: true
  push_requires_approval: true
```

### 9.3 Execute with `persistence.mode: commit`

```yaml
version: "1.0"
mission_id: 2026-07-23-exec-commit-001
title: Controlled Write Commit
repository:
  name: Mission-Control
  path: .
  base_branch: main
execution:
  agent: cursor
  mode: execute
  sandbox: true
  worktree: false
permissions:
  read: true
  create_files: true
  modify_files: false
  delete_files: false
  run_commands: true
  stage_changes: false
  commit: false
  push: false
persistence:
  mode: commit
instructions: |
  Create docs/example-commit.txt with one line of text.
  Update docs/HAL_OPERATOR_LOG.md with verified results.
deliverables:
  - docs/example-commit.txt
  - docs/HAL_OPERATOR_LOG.md
approval:
  execute_without_approval: true
  commit_requires_approval: true
  push_requires_approval: true
```

### 9.4 Execute with `persistence.mode: push`

```yaml
version: "1.0"
mission_id: 2026-07-23-exec-push-001
title: Controlled Write Push
repository:
  name: Mission-Control
  path: .
  base_branch: main
execution:
  agent: cursor
  mode: execute
  sandbox: true
  worktree: false
permissions:
  read: true
  create_files: true
  modify_files: false
  delete_files: false
  run_commands: true
  stage_changes: false
  commit: false
  push: false
persistence:
  mode: push
instructions: |
  Create docs/example-push.txt with one line of text.
  Update docs/HAL_OPERATOR_LOG.md with verified results.
deliverables:
  - docs/example-push.txt
  - docs/HAL_OPERATOR_LOG.md
approval:
  execute_without_approval: true
  commit_requires_approval: true
  push_requires_approval: true
  platform_push_approved: true
```

---

## 10. Common validation failures and corrections

| Failure | Typical message | Correction |
| --- | --- | --- |
| Missing required top-level key | `Missing required keys: permissions` (etc.) | Add every required key listed in §1 |
| Wrong spec version | `Unsupported version: … (expected 1.0)` | Set `version: "1.0"` (or `1.0`) |
| Invalid YAML | `Invalid YAML: …` | Fix YAML syntax |
| Non-mapping persistence | `persistence must be a mapping` | Use `persistence: { mode: … }` |
| Bad persistence mode | `Unsupported persistence.mode: …` | Use `none`, `commit`, or `push` |
| Plan endpoint with execute mode | `Unsupported mode: execute (expected plan)` | Use `mode: plan` for `POST /run` |
| Execute endpoint with plan mode | `Unsupported mode: plan (expected execute)` | Use `mode: execute` for `POST /runs` |
| Non-cursor agent | `Unsupported agent: … (expected cursor)` | Set `execution.agent: cursor` |
| Worktree requested | `Worktrees are not supported in Phase 2` / `… for execute` | Set `execution.worktree: false` |
| Mutating permission on plan run | `Permission not allowed for run: create_files` (etc.) | Keep run false-permissions false |
| Forbidden execute permission | `Permission not allowed for execute: push` (etc.) | Keep delete/stage/commit/push false |
| Execute without file perms (non-push) | `Execute requires at least one of: create_files or modify_files` | Enable create and/or modify, or use approved `persistence.mode: push` |
| Push without platform approval | `PLATFORM_PUSH_APPROVAL_REQUIRED: …` | Set `approval.platform_push_approved: true` or `allow_automatic_platform_push: true` |
| Missing/invalid repo path | `repository.path must be a non-empty string` / `does not exist` / `not a directory` | Point `repository.path` at an existing directory |
| Top level not a mapping | `Mission must be a YAML mapping at the top level` | Root document must be a YAML object |

---

## Implementation notes / ambiguities

1. **`ask` mode:** documented in `MISSION_SPEC.md` and supported by the Cursor
   command builder, but no submission path accepts `execution.mode: ask`.
   Sync `POST /run` validates `plan` then invokes Cursor with `--mode ask`.
2. **`sandbox`:** conventional field; not validated or interpreted in code.
3. **Approval policy flags** other than platform-push fields are not enforced
   by validators or the persistence layer.
4. **Deliverable filesystem verification** is specified narratively in
   `MISSION_SPEC.md` but not implemented (see §6).
5. **Structural validation is shallow:** nested types for
   `repository` / `execution` / `permissions` / `approval` /
   `instructions` / `deliverables` are mostly unchecked until run/execute
   eligibility.
6. **Legacy `POST /execute`** does not apply platform persistence despite
   accepting execute missions that declare a `persistence` block.
7. **Isolated async workspaces** require `MISSION_CONTROL_REPOSITORY_URL`;
   that env gate is separate from YAML schema validation.
