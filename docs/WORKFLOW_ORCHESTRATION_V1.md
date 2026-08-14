# Workflow Orchestration v1

Durable, bounded server-side workflow orchestration for Mission Control.
**Disabled by default.** Do not enable in production until follow-up missions
wire API/MCP, the background reconciler, and notification suppression.

## Objective (this mission slice)

Ship the durable model + deterministic state machine so Mission Control—not a
ChatGPT turn—can own implementation → substantive review → targeted fix →
re-review progression. This revision hardens security and atomicity from
review `8a575671-6b80-4280-88da-193bf1386a47`.

| Delivered here | Deferred (follow-up missions) |
| --- | --- |
| SQLite workflow / step / audit tables (schema v2) | HTTP submit/status/history/cancel API |
| CAS via exact `cursor.rowcount` + idempotent claims | MCP tools |
| Spoof-resistant verdict envelope + opaque follow-up context | Lifespan reconciler worker hook |
| Transactional policy gates at every child-launch claim | Outbox/Pushover child-alert suppression wiring |
| Claim / materialization states + mark-then-launch recovery | Runtime `RunRegistry.create_run(run_id=…)` wiring |
| Budget ceilings with documented inclusive semantics | Auto-merge / Railway deploy primitives |
| Feature flag (off by default) | — |

## Files

| Path | Role |
| --- | --- |
| `mission_control/workflow_registry.py` | Durable registry, schema v2 migrations, CAS, audit |
| `mission_control/workflow_orchestrator.py` | Verdict envelope, context builder, gates, state machine |
| `tests/test_workflow_orchestration_v1.py` | Exploit, concurrency, budget, migration tests |
| `docs/WORKFLOW_ORCHESTRATION_V1.md` | This document |

## Feature flag

| Variable | Default | Meaning |
| --- | --- | --- |
| `MISSION_CONTROL_WORKFLOW_ORCHESTRATION` | unset/false | Master enable (must stay off in prod) |
| `MISSION_CONTROL_WORKFLOW_RECONCILE_INTERVAL_SECONDS` | `5` | Future worker poll interval |
| `MISSION_CONTROL_DB_PATH` | `./data/mission-control.db` | Shared SQLite path with run registry |

## Schema

**Current schema version: `2`.** Stored in `workflow_schema_meta`. Opening a DB
with a newer unsupported version **fails closed**
(`WorkflowSchemaUnsupportedError`). Migrations are additive only.

### `workflows`

| Column | Notes |
| --- | --- |
| `workflow_id` | Immutable UUID PK |
| `state` | See states below |
| `version` | Monotonic CAS field |
| `policy_json` | Immutable policy snapshot at submit |
| `step_specs_json` | Exact mission templates (implementation/review/fix/re_review) |
| `parent_run_id` / `current_step_id` | Lineage |
| `fix_cycle_count` | Fix/re-review cycles used |
| `child_run_count` | Ceiling counter |
| `credit_units_used` | Conservative per-child unit counter (estimated) |
| `credit_usage_actual` | Nullable exact usage; enforced when set |
| `last_decision_json` | Last machine decision (+ policy audit) |
| `last_blocker_fingerprint` | Canonical repeated-blocker detection |
| `notification_emitted` | Exactly-one actionable alert latch |
| timestamps / `error` | Audit |

### `workflow_steps`

| Column | Notes |
| --- | --- |
| `step_id` | UUID PK |
| `step_type` | `implementation` \| `review` \| `fix` \| `re_review` |
| `status` | `pending` / `claimed` / `queued` / `running` / terminal |
| `materialization_state` | `claimed` \| `materialized` (v2) |
| `attempt` / `cycle` | Attempt + fix cycle |
| `idempotency_key` | Unique; `{workflow}:{type}:c{N}:a{M}` |
| `child_run_id` | Pre-assigned at claim; **UNIQUE** (v2) |
| `parent_run_id` | Prior child run |
| `mission_yaml` | Exact step mission (+ opaque follow-up context trailer) |
| `policy_json` | Copied snapshot |
| `blocker_fingerprint` / `last_decision_json` | Review artifacts |

### `workflow_transitions`

Append-only audit: `from_state`, `to_state`, `reason`, `detail_json`
(includes `policy_audit` on launches), `version_after`, optional `step_id` /
`child_run_id`, `created_at`.

## Hardened verdict contract

Review children must emit **exactly one terminal envelope**:

```text
<<<MC_REVIEW_VERDICT_V1>>>
{"kind":"blocked","findings":["missing tests"]}
<<<END_MC_REVIEW_VERDICT_V1>>>
```

Rules:

- `kind` exact enum: `merge_ready` | `blocked`
- `blocked` requires a non-empty bounded `findings` array (≤32 items, ≤500 chars each)
- `merge_ready` must not include findings
- Envelope must be terminal (only whitespace after the closing marker)
- Duplicate / ambiguous / malformed / non-JSON → intervention (`blocked`)
- Prose `MERGE-READY` / `BLOCKED`, code fences, blockquotes, quoted prior
  output, and instructions to print those words are **ignored**

## Follow-up context contract

Fix missions are built from an **immutable template** plus an opaque JSON
trailer (not YAML-merged authority):

```text
<<<MC_FOLLOWUP_CONTEXT_V1>>>
{"findings":["…"],"prior_excerpt":"…"}
<<<END_MC_FOLLOWUP_CONTEXT_V1>>>
```

Every context field (including findings and extras) is redacted and bounded.
Injected `permissions` / `persistence` / `repository` / `branch` text inside
findings cannot alter authority — authorization comes only from the immutable
policy snapshot + transactional launch gates.

## States

**Non-terminal:** `pending`, `running`

**Terminal (canonical):** `completed`, `needs_approval`, `blocked`,
`budget_exhausted`, `failed`, `cancelled`

v1 prefers `needs_approval` after merge-ready (even if policy sets
`allow_auto_merge` / `allow_auto_deploy`) until typed merge/deploy primitives
exist. `completed` is reserved for a later mission that finishes an
explicitly authorized post-approval path.

### Claim / materialization

| Step status | Materialization | Meaning |
| --- | --- | --- |
| `claimed` | `claimed` | Launch CAS committed; run not yet in `RunRegistry` |
| `queued` / `running` | `materialized` | Child visible to reconciler / worker |
| terminal | — | Step finished |

A committed claim with no materialized run stays `awaiting_child_materialize`
(NOOP) and remains restart-safe / idempotent — it never becomes
`no_active_step`. If a mark commits and the process crashes before the
follow-up claim, reconcile **re-derives** the pending launch from the latest
completed step.

## Transition table

| From | Event | To / action | Gate |
| --- | --- | --- | --- |
| (new) | submit | `pending` + audit | Policy + exact step specs required |
| `pending`/`running` | reconcile, no steps | claim launch `implementation` | Child/credit ceilings + policy gates |
| `running` | implementation `completed` | claim launch `review` | Same repo/branch lineage; review read-only |
| `running` | review merge-ready envelope | `needs_approval` | Default; auto merge/deploy deferred |
| `running` | review blocked envelope | claim launch `fix` (cycle++) | New fingerprint; `fix_cycle < max` before next |
| `running` | fix `completed` | claim launch `re_review` | Ceilings + gates |
| `running` | re-review merge-ready | `needs_approval` | Same as review |
| `running` | repeated blocker fingerprint | `blocked` | Canonical fingerprint |
| `running` | max fix cycles | `blocked` | Intervention |
| `running` | malformed/ambiguous verdict | `blocked` | Intervention |
| `running` | child `failed`/`cancelled` | `failed` | — |
| `running` | child `timed_out` | `blocked` | Intervention |
| any non-terminal | cancel | `cancelled` | CAS |
| any non-terminal | wall-clock / credit / child ceiling | `budget_exhausted` | See budget semantics |
| any non-terminal | scope/repo/branch/permission escalation | `blocked` | Default deny (transactional) |
| terminal actionable | alert latch | `notification_emitted=1` | Once |

## Budget semantics (inclusive)

| Ceiling | Deny / exhaust when |
| --- | --- |
| `child_run_count` | `child_run_count + 1 > max_child_runs` (i.e. already `>= max` before launch) |
| estimated credit | `credit_units_used + credit_unit_per_child_run > max_credit_units` |
| actual credit | when `credit_usage_actual` is set: `credit_usage_actual >= max_credit_units` |
| wall-clock | `elapsed >= max_wall_clock_seconds` |
| fix cycles | `fix_cycle_count + 1 > max_fix_cycles` before launching another fix |

## Policy snapshot (default deny)

Immutable at submit. **Never** inferred from prose or follow-up context:

- `allow_auto_merge`, `allow_auto_deploy` → false
- `allow_destructive_actions`, `allow_permission_expansion`,
  `allow_database_migrations`, `allow_secret_changes`,
  `allow_scope_or_repo_changes` → false
- `max_fix_cycles` default `2`
- `max_child_runs` / `max_credit_units` / `max_wall_clock_seconds`
- `repository_name`, `base_branch`, `target_branch`, `implementation_scope`

`enforce_launch_policy_gates` runs inside `claim_child_launch` under
`BEGIN IMMEDIATE` immediately before the claim write; audit evidence is
persisted on the transition / `last_decision_json`.

## At-most-once child launch

1. Reconciler decides `LAUNCH_CHILD` with durable `idempotency_key`.
2. Orchestrator + registry re-check policy gates; claim under
   `BEGIN IMMEDIATE` + version CAS uses **exact `cursor.rowcount`** (not
   `connection.total_changes`).
3. Step inserted as `status=claimed`, `materialization_state=claimed`, with
   unique `child_run_id` and `UNIQUE(idempotency_key)`.
4. Restart / concurrent claim with the same key returns the same
   `child_run_id` (`already_claimed=true`) — no duplicate step.
5. **Follow-up:** materialize via future `RunRegistry.create_run(run_id=…)`
   using `reserved_child_run_materialization_spec` (contract prepared; not
   wired at runtime yet). Call `mark_step_materialized` after insert.

## Notifications (design; wiring deferred)

- Workflow-managed child terminal alerts → suppress (preserve audit rows).
- Exactly one actionable workflow alert for:
  `needs_approval` | `completed` | `blocked` | `budget_exhausted` | `failed`
- Latch: `workflows.notification_emitted`

## Test evidence

```bash
python -m unittest tests.test_workflow_orchestration_v1 -v
python -m unittest discover -s tests -v
```

Covered:

- Spoof-resistant verdicts (safe + malicious, fences, blockquotes, examples)
- YAML authority injection + secrets in all context fields
- Unused-helper / transactional gate enforcement on claim
- Concurrent same-connection and multi-connection CAS
- Crash after claim before materialize; crash after mark before launch
- Duplicate `child_run_id` uniqueness; schema upgrade + newer-version reject
- All budget boundaries (child, estimated credit, actual credit, wall-clock,
  fix-cycle) with off-by-one checks
- Canonical fingerprint ordering/whitespace
- Feature remains disabled by default

## Residual risks

1. Child runs are **reserved** but not yet inserted into `runs` / queue —
   without the materialize follow-up, workflows wait at
   `awaiting_child_materialize`.
2. Notification suppression is decided in-process; outbox/Pushover not hooked.
3. No HTTP/MCP surface yet — operators cannot submit via API.
4. Auto-merge/deploy explicitly deferred; policy bits are recorded only.
5. Credit metering still uses a conservative unit counter; actual usage is
   optional until provider metering lands.
6. Feature flag must remain off until worker + API missions land.
7. Corrective git commit/push is owned by Mission Control platform
   persistence (agents must not commit/push in this mission).

## Rollout plan

1. **Merge this slice** (flag off) — hardened schema + library only.
2. **Mission B — materialize + worker:** `create_run(run_id=…)`, lifespan
   reconciler behind the flag, structured metrics logs.
3. **Mission C — API + MCP:** submit/status/history/cancel + connector tools.
4. **Mission D — notifications:** suppress workflow-managed child terminals;
   one actionable workflow alert.
5. **Mission E — staging enable:** flag on in non-prod; soak; then prod.

## Split recommendation (remaining work)

Per split-run policy (≤4 files / one objective), do **not** expand this
mission further. Recommended follow-ups:

1. `RunRegistry` reserved-id create + orchestrator materialize + lifespan hook
2. FastAPI + MCP submit/status/history/cancel
3. Notification outbox suppression + workflow alert enqueue
4. Staging enablement + operator runbook

## Platform commit SHA

Base review commit: `af08a3da3f1b06835625c9d4f6ba6f17bc3db09f`.

Corrective commit SHA: owned by Mission Control platform persistence after
this workspace is collected (this mission must not commit or push).
