# Workflow Orchestration v1

Durable, bounded server-side workflow orchestration for Mission Control.
**Disabled by default.** Do not enable in production until follow-up missions
wire API/MCP, the background reconciler, and notification suppression.

## Objective (this mission slice)

Ship the durable model + deterministic state machine so Mission Control—not a
ChatGPT turn—can own implementation → substantive review → targeted fix →
re-review progression.

| Delivered here | Deferred (follow-up missions) |
| --- | --- |
| SQLite workflow / step / audit tables | HTTP submit/status/history/cancel API |
| CAS + idempotent child-launch claims | MCP tools |
| v1 policy state machine | Lifespan reconciler worker hook |
| Deterministic + integration-style unit tests | Outbox/Pushover child-alert suppression wiring |
| This design + rollout doc | `RunRegistry.create_run(run_id=…)` materialize helper |
| Feature flag (off by default) | Auto-merge / Railway deploy primitives |

## Files

| Path | Role |
| --- | --- |
| `mission_control/workflow_registry.py` | Durable registry, schema, CAS, audit |
| `mission_control/workflow_orchestrator.py` | Policy gates + reconcile state machine |
| `tests/test_workflow_orchestration_v1.py` | Transition, CAS, budget, security tests |
| `docs/WORKFLOW_ORCHESTRATION_V1.md` | This document |

## Feature flag

| Variable | Default | Meaning |
| --- | --- | --- |
| `MISSION_CONTROL_WORKFLOW_ORCHESTRATION` | unset/false | Master enable (must stay off in prod) |
| `MISSION_CONTROL_WORKFLOW_RECONCILE_INTERVAL_SECONDS` | `5` | Future worker poll interval |
| `MISSION_CONTROL_DB_PATH` | `./data/mission-control.db` | Shared SQLite path with run registry |

## Schema

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
| `credit_units_used` | Conservative per-child unit counter |
| `credit_usage_actual` | Nullable; reserved for future exact metering |
| `last_decision_json` | Last machine decision |
| `last_blocker_fingerprint` | Repeated-blocker detection |
| `notification_emitted` | Exactly-one actionable alert latch |
| timestamps / `error` | Audit |

### `workflow_steps`

| Column | Notes |
| --- | --- |
| `step_id` | UUID PK |
| `step_type` | `implementation` \| `review` \| `fix` \| `re_review` |
| `status` | pending/queued/running/terminal |
| `attempt` / `cycle` | Attempt + fix cycle |
| `idempotency_key` | Unique; `{workflow}:{type}:c{N}:a{M}` |
| `child_run_id` | Pre-assigned at claim (at-most-once) |
| `parent_run_id` | Prior child run |
| `mission_yaml` | Exact step mission (may include redacted context) |
| `policy_json` | Copied snapshot |
| `blocker_fingerprint` / `last_decision_json` | Review artifacts |

### `workflow_transitions`

Append-only audit: `from_state`, `to_state`, `reason`, `detail_json`,
`version_after`, optional `step_id` / `child_run_id`, `created_at`.

## States

**Non-terminal:** `pending`, `running`

**Terminal (canonical):** `completed`, `needs_approval`, `blocked`,
`budget_exhausted`, `failed`, `cancelled`

v1 prefers `needs_approval` after MERGE-READY (even if policy sets
`allow_auto_merge` / `allow_auto_deploy`) until typed merge/deploy primitives
exist. `completed` is reserved for a later mission that finishes an
explicitly authorized post-approval path.

## Transition table

| From | Event | To / action | Gate |
| --- | --- | --- | --- |
| (new) | submit | `pending` + audit | Policy + exact step specs required |
| `pending`/`running` | reconcile, no steps | claim launch `implementation` | Child/credit ceilings |
| `running` | implementation `completed` | claim launch `review` | Same repo/branch lineage; review read-only |
| `running` | review `MERGE-READY` | `needs_approval` | Default; auto merge/deploy deferred |
| `running` | review `BLOCKED` + findings | claim launch `fix` (cycle++) | New fingerprint; `fix_cycle ≤ max` |
| `running` | fix `completed` | claim launch `re_review` | Ceilings |
| `running` | re-review `MERGE-READY` | `needs_approval` | Same as review |
| `running` | repeated blocker fingerprint | `blocked` | Intervention |
| `running` | max fix cycles | `blocked` | Intervention |
| `running` | malformed/ambiguous verdict | `blocked` | Intervention |
| `running` | child `failed`/`cancelled` | `failed` | — |
| `running` | child `timed_out` | `blocked` | Intervention |
| any non-terminal | cancel | `cancelled` | CAS |
| any non-terminal | wall-clock / credit / child ceiling | `budget_exhausted` | Policy snapshot ceilings |
| any non-terminal | scope/repo/branch/permission escalation | `blocked` | Default deny |
| terminal actionable | alert latch | `notification_emitted=1` | Once |

## Policy snapshot (default deny)

Immutable at submit. **Never** inferred from prose:

- `allow_auto_merge`, `allow_auto_deploy` → false
- `allow_destructive_actions`, `allow_permission_expansion`,
  `allow_database_migrations`, `allow_secret_changes`,
  `allow_scope_or_repo_changes` → false
- `max_fix_cycles` default `2`
- `max_child_runs` / `max_credit_units` / `max_wall_clock_seconds`
- `repository_name`, `base_branch`, `target_branch`, `implementation_scope`

Auto-followups may only target the same repository and approved branch
lineage. Review steps must stay read-only with `persistence.mode: none`.

## At-most-once child launch

1. Reconciler decides `LAUNCH_CHILD` with durable `idempotency_key`.
2. `claim_child_launch` under `BEGIN IMMEDIATE` + version CAS assigns
   `child_run_id` and inserts the step (`UNIQUE idempotency_key`).
3. Restart / concurrent claim with the same key returns the same
   `child_run_id` (`already_claimed=true`) — no duplicate step.
4. **Follow-up:** materialize that `child_run_id` into `RunRegistry` +
   `RunQueue` (requires `create_run` accepting a reserved id).

## Notifications (design; wiring deferred)

- Workflow-managed child terminal alerts → suppress (preserve audit rows).
- Exactly one actionable workflow alert for:
  `needs_approval` | `completed` | `blocked` | `budget_exhausted` | `failed`
- Latch: `workflows.notification_emitted`

## Test evidence

```bash
python -m unittest tests.test_workflow_orchestration_v1 -v
```

Covered:

- Every major transition and policy gate
- Implementation → review without external status/HTTP
- BLOCKED → fix → MERGE-READY re-review → `needs_approval`
- Restart/idempotent claim (no duplicate child)
- Concurrent CAS reconcilers
- Repeated blocker, malformed verdict, timeout, child failure, cancel
- Child/credit/wall-clock ceilings
- Notification suppress + single workflow alert latch
- Repo/branch/scope/merge/deploy/permission escalation denials

## Residual risks

1. Child runs are **reserved** but not yet inserted into `runs` / queue —
   without the materialize follow-up, workflows wait at
   `awaiting_child_materialize`.
2. Notification suppression is decided in-process; outbox/Pushover not hooked.
3. No HTTP/MCP surface yet — operators cannot submit via API.
4. Auto-merge/deploy explicitly deferred; policy bits are recorded only.
5. Credit metering is a conservative unit counter, not provider usage.
6. Feature flag must remain off until worker + API missions land.

## Rollout plan

1. **Merge this slice** (flag off) — schema + library only.
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

Not applicable in-agent: git commit/push are owned by Mission Control
platform persistence (this mission must not commit or push).
