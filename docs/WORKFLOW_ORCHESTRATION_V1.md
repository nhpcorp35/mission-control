# Workflow Orchestration v1

Durable, bounded server-side workflow orchestration for Mission Control.
**Disabled by default.** Do not enable in production until follow-up missions
wire API/MCP and notification suppression.

## Objective

Ship the durable model + deterministic state machine + lifespan reconciler so
Mission Control—not a ChatGPT turn—can own implementation → substantive
review → targeted fix → re-review progression.

| Delivered | Deferred (follow-up missions) |
| --- | --- |
| SQLite workflow / step / audit / dispatch tables (schema **v3**) | HTTP submit/status/history/cancel API |
| CAS via exact `cursor.rowcount` + idempotent claims | MCP tools |
| Spoof-resistant verdict envelope + opaque follow-up context | Outbox/Pushover child-alert suppression wiring |
| Transactional policy gates at every child-launch claim | Production enablement |
| Claim / materialization states + mark-then-launch recovery | Auto-merge / Railway deploy primitives |
| `RunRegistry.create_run(run_id=…)` reserved-ID contract | — |
| Crash-safe claim→create materializer + enqueue-once | — |
| Durable dispatch intents + execution-observed ack | — |
| **Lifespan-managed background reconciler** (flag off by default) | — |
| Budget ceilings with documented inclusive semantics | — |
| Feature flag (off by default) | — |

## Files

| Path | Role |
| --- | --- |
| `mission_control/workflow_registry.py` | Durable registry, schema v3, CAS, claim + mark dual-CAS, dispatch intents |
| `mission_control/workflow_orchestrator.py` | Verdict envelope, context builder, gates, state machine |
| `mission_control/workflow_materializer.py` | Crash-safe claim→`RunRegistry` materialize + enqueue-once; strips opaque follow-up trailer for YAML structure parse only (exact stored text still used for `create_run`) |
| `mission_control/workflow_reconciler.py` | Lifespan background reconciler (bounded ticks) |
| `mission_control/run_registry.py` | Reserved-ID `create_run(run_id=…)` materialization contract |
| `app/api.py` | Lifespan start/stop when feature flag is enabled |
| `tests/test_workflow_orchestration_v1.py` | Exploit, concurrency, budget, migration tests |
| `tests/test_workflow_materialization.py` | Materialize crash / concurrency / policy tests |
| `tests/test_workflow_reconciler.py` | Reconciler progression / recovery / fairness tests |
| `tests/test_run_registry.py` | Reserved-ID create / conflict / concurrency tests |
| `docs/WORKFLOW_ORCHESTRATION_V1.md` | This document |

## Feature flag and reconciler config

| Variable | Default | Meaning |
| --- | --- | --- |
| `MISSION_CONTROL_WORKFLOW_ORCHESTRATION` | unset/false | Master enable (must stay off in prod) |
| `MISSION_CONTROL_WORKFLOW_RECONCILE_INTERVAL_SECONDS` | `5` | Worker poll interval (floor **0.5s**) |
| `MISSION_CONTROL_WORKFLOW_RECONCILE_BATCH_SIZE` | `16` | Max workflows processed per tick (1–64) |
| `MISSION_CONTROL_WORKFLOW_RECONCILE_MAX_TICK_SECONDS` | `2` | Per-tick wall-clock budget |
| `MISSION_CONTROL_DB_PATH` | `./data/mission-control.db` | Shared SQLite path with run registry |

## Schema

**Current schema version: `3`.** Stored in `workflow_schema_meta`. Opening a DB
with a newer unsupported version **fails closed**
(`WorkflowSchemaUnsupportedError`). Migrations are additive only.

- **v1 → v2:** `materialization_state` + `UNIQUE(child_run_id)` on steps
- **v2 → v3:** `workflow_dispatch_intents` (unique per `child_run_id`) for
  durable RunQueue handoff / lease / ack / poison

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

### `workflow_dispatch_intents` (v3)

| Column | Notes |
| --- | --- |
| `child_run_id` | PK; one intent per reserved child |
| `workflow_id` / `step_id` | Lineage |
| `state` | `pending` \| `leased` \| `acked` \| `poisoned` |
| `attempt_count` / `lease_owner` / `lease_expires_at` | Multi-replica lease |
| `next_attempt_at` / `last_error` | Backoff (secret-safe reason only) |
| timestamps | `created_at` / `updated_at` / `acked_at` |

## Lifespan reconciler

`WorkflowReconciler` follows the notification delivery worker pattern:
start on API lifespan when the feature flag is **explicitly** enabled; on
shutdown set the stop latch and **await** the thread.

When the flag is off, `start()` returns `False` and creates **no** thread /
activity. Lifespan still calls `stop()` (no-op) so shutdown stays symmetric.

### Tick contract (bounded)

Each tick, independently of HTTP/MCP/`mission.status` reads:

1. **Discover** eligible non-terminal workflows (fair rotated order).
2. **Observe** child `RunRegistry` statuses and apply orchestrator decisions
   (mark terminal steps, claim next children, enforce ceilings / blockers).
3. **Materialize** claimed/unmaterialized steps via `materialize_claimed_child`.
4. **Redrive** pending / expired-lease durable dispatch intents.
5. **Finalize** intents once `RunRegistry` observes `running` or terminal
   (`completed` / `failed` / `timed_out`).

Run terminal-state writes alone must make the next tick advance the workflow.
No status-API or user polling is required.

### Correctness vs process-local

| Boundary | Role |
| --- | --- |
| SQLite CAS (`version`, claim idempotency, mark dual-CAS) | Correctness |
| Dispatch intent leases (`BEGIN IMMEDIATE`) | Correctness across replicas |
| Process-local fairness cursor / poison skip / tick lock | Work reduction only |

Two reconcilers (two DB connections) must not launch duplicate child runs or
execute a child twice — uniqueness + leases prevent that.

### Bounds and isolation

- Interval default **5s**, floor **0.5s**; sleep uses Event wait (no busy poll).
- Batch size + per-tick time budget bound work.
- Fair round-robin ordering so one workflow cannot starve others.
- Per-workflow exceptions are isolated; after repeated failures the worker
  skips that id briefly (poison isolation) without blocking the tick.
- Infrastructure errors (SQLite/OS) apply jittered exponential backoff.
- Logs are structured and secret-safe: never mission YAML, raw findings, or
  credentials. Lightweight counters live on `WorkflowReconciler.counters`.

### Startup / crash recovery

The same tick recovers:

| Residual state | Recovery |
| --- | --- |
| Claimed / unmaterialized step | `materialize_claimed_child` |
| Materialized queued child + pending intent | `redrive_materialized_dispatch` |
| Expired dispatch lease | Listed as redrivable → reclaim + redrive |
| Terminal child not yet reconciled | `ChildRunView` from `RunRegistry` → orchestrator |
| Partial transition (mark without follow-up claim) | Orchestrator follow-up recovery |

Ceilings (child / fix / wall-clock / credit) and repeated blocker fingerprints
stop workflows deterministically via existing orchestrator gates.

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

Child terminal observation uses authoritative `RunStatus` values
`completed` / `failed` / `timed_out` (no separate cancelled run status).
Orchestrator still accepts a `cancelled` child view for fail-closed mapping
when projected externally.

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
5. **Materialize** via `materialize_claimed_child`. Uses exact stored
   step YAML + reserved `child_run_id` + canonical `retried_from` ownership.
   Marks the step materialized only after `created` or
   `recovered_idempotently`, with CAS on both workflow version **and**
   `materialization_state='claimed'`. Process-local enqueue is idempotent;
   durable dispatch ack waits for `RunRegistry` `running`/terminal. Feature
   flag must be on for the call; default remains off.

## Claim-to-create crash contract

Deterministic protocol (SQLite PK + CAS are the correctness boundary; do not
rely on process-local locks):

```text
CLAIMED + exact mission_yaml + parent binding
  → final policy / ceiling / authority gates
  → RunRegistry.create_run(run_id=child_run_id, mission_yaml=exact,
                           retried_from=ownership)
  → mark_step_materialized (CAS) + unique pending dispatch intent
  → claim lease → RunQueue.enqueue (process-local; idempotent)
  → if RunRegistry status still queued: release lease to pending
       (no failure backoff / attempt burn; intent stays redrivable)
  → durable ack only when RunRegistry status is running or terminal
```

Authoritative execution-claim boundary: `RunRegistry` status `running` or
terminal (`completed` / `failed` / `timed_out`). Process-local `RunQueue`
acceptance alone must never finalize/ack dispatch — queue memory dies with
the process while a queued registry row must remain redriveable.

| Window | Retry behavior |
| --- | --- |
| Before create | Fresh `create_run` |
| After create, before mark | `recovered_idempotently` → mark → enqueue |
| After mark, before enqueue | Pending intent; redrive claims + enqueues |
| After enqueue, registry still `queued` | Intent remains pending/redrivable; restart with empty process queue re-enqueues; same-process duplicate suppress is idempotent (no attempt burn) |
| Worker reaches `running` / terminal before bookkeeping | Ack exactly once; registry suppress prevents duplicate execution |
| Existing matching run | Idempotent recover |
| Existing mismatch | Poison / fail closed (`conflict_class`, no secrets) |
| Missing parent binding | Reject **before** create |
| Final policy denial | Block workflow; no registry row |
| Expired dispatch lease | Reclaim + redrive (leases must not strand queued work) |

Concurrent materializers / redrivers across two DB connections yield one
registry row and at most one process-local enqueue per live queue. The
lifespan reconciler redrives pending intents after process death via the
same primitives.

## Reserved RunRegistry IDs

`RunRegistry.create_run` accepts an optional caller-reserved `run_id`.

| Call | Return |
| --- | --- |
| `create_run(...)` (no `run_id`) | `RunRecord` — existing UUID4 allocation |
| `create_run(run_id=…, mission_yaml=…, retried_from=…)` | `ReservedRunCreateResult` |

`ReservedRunCreateResult.outcome`:

| Outcome | Meaning |
| --- | --- |
| `created` | Inserted the reserved UUID once |
| `recovered_idempotently` | Row already existed with **exact** `mission_yaml` + `retried_from` |
| `conflict` | Fail closed; existing row is never overwritten or recycled |

Conflict classes (stable strings): `invalid_run_id`, `noncanonical_run_id`,
`ownership_mismatch`, `repository_mismatch`, `permissions_mismatch`,
`execution_mismatch`, `mission_yaml_mismatch`, `existing_run_collision`.

Rules:

- Reserved IDs must be canonical `str(uuid.UUID(...))` form (lowercase + hyphens).
- Inserts use `BEGIN IMMEDIATE` + primary-key create-once across connections.
- Idempotent recover compares immutable mission identity / launch metadata only.
- Lifecycle logs must not include raw mission YAML or secrets.
- Feature flag `MISSION_CONTROL_WORKFLOW_ORCHESTRATION` remains off by default.

## Notifications (design; wiring deferred)

- Workflow-managed child terminal alerts → suppress (preserve audit rows).
- Exactly one actionable workflow alert for:
  `needs_approval` | `completed` | `blocked` | `budget_exhausted` | `failed`
- Latch: `workflows.notification_emitted`

## Test evidence

```bash
python -m unittest tests.test_run_registry -v
python -m unittest tests.test_workflow_orchestration_v1 -v
python -m unittest tests.test_workflow_materialization -v
python -m unittest tests.test_workflow_reconciler -v
```

Covered (reconciler slice):

- Implementation terminal → later tick creates/queues review (no status API)
- Review terminal → fix/re-review or needs_approval / stop
- Startup recovery for claim/materialize/dispatch/terminal residuals
- Two concurrent reconcilers do not duplicate child creation/execution
- Feature-off: no thread; enable starts; shutdown cancels/awaits
- Interval floor, batch/time bounds, fairness, poison isolation, infra backoff
- Child terminals via actual `RunStatus` (`completed`/`failed`/`timed_out`)
- Ceilings / repeated blocker / approval boundary
- Existing materializer, registry, queue, workflow, execution-lifecycle suites

## Residual risks

1. Notification suppression is decided in-process; outbox/Pushover not hooked.
2. No HTTP/MCP surface yet — operators cannot submit via API.
3. Auto-merge/deploy explicitly deferred; policy bits are recorded only.
4. Credit metering still uses a conservative unit counter; actual usage is
   optional until provider metering lands.
5. Feature flag must remain off until API + notification missions land.
6. Corrective git commit/push is owned by Mission Control platform
   persistence (agents must not commit/push in this mission).

## Rollout plan

1. **Merged:** schema + orchestrator + materializer + reconciler (flag off).
2. **Mission C — API + MCP:** submit/status/history/cancel + connector tools.
3. **Mission D — notifications:** suppress workflow-managed child terminals;
   one actionable workflow alert.
4. **Mission E — staging enable:** flag on in non-prod; soak; then prod.

## Split recommendation (remaining work)

Per split-run policy (≤4 files / one objective), do **not** expand this
mission further. Recommended follow-ups:

1. FastAPI + MCP submit/status/history/cancel
2. Notification outbox suppression + workflow alert enqueue
3. Staging enablement + operator runbook

## Platform commit SHA

Base review commit: `91dca4c0cb0fc7d9279c3b83132250b7b1531862`.

Corrective commit SHA: owned by Mission Control platform persistence after
this workspace is collected (this mission must not commit or push).
