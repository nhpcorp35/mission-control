# Mission Control Optimization Authority

## Purpose and Authority

This file is the **canonical source of truth** for Mission Control issues, cost problems, reliability defects, and optimization work discovered during LegalAI Case-00.

- **Allen** is not responsible for manually tracking technical details recorded here.
- **HAL** owns keeping this register current through future approved missions.
- No Mission Control optimization work is authoritative unless it appears in this register with a stable ID, evidence, owner/next action, and completion verification.

---

## Status Definitions

| Status | Meaning |
|--------|---------|
| Observed | Reported or repeatedly seen; not yet independently confirmed |
| Verified | Confirmed with evidence suitable for planning |
| Planned | Accepted for implementation; sequenced, not started |
| In Progress | Active implementation under an approved mission |
| Blocked | Cannot proceed until a named dependency clears |
| Implemented | Code/process change landed; not yet proven in operation |
| Verified Complete | Implementation proven with completion evidence |
| Deferred | Intentionally postponed with rationale |

---

## Priority Definitions

| Priority | Meaning |
|----------|---------|
| P0 | Cost or reliability emergency; stop or contain immediately |
| P1 | High ROI; large waste or failure reduction for modest effort |
| P2 | Important; schedule after P0/P1 |
| P3 | Later; useful but not blocking near-term throughput |

---

## Issue Register

| ID | Priority | Status | Issue | Verified evidence | Impact | Recommended correction | Dependencies | Next action |
|----|----------|--------|-------|-------------------|--------|------------------------|--------------|-------------|
| MCO-001 | P0 | Verified | Excessive Mission Control / Cursor spend (~two $100 credit additions in one week) | Repeated credit top-ups during Case-00 mission volume | Unsustainable burn; blocks scale to Case-01/02 | Instrument cost telemetry; cut redundant missions, rebuilds, and cold starts | MCO-012 | Baseline spend after Case-00 attorney approval |
| MCO-002 | P1 | Verified | Repeated Cursor Agent startup and full repository-context overhead | Each narrow mission pays cold-start + re-ingest cost | Multiplies wall time and tokens per trivial check | Persistent project profiles; reuse warm context; prefer bundled work | MCO-019 | Design profile/context reuse after telemetry baseline |
| MCO-003 | P1 | Verified | Too many narrowly separated read-only verification missions | Case-00 pattern of many small verify-only missions | Startup tax dominates useful work | Bundle safe read-only checks; raise default mission grain | MCO-006, MCO-020 | Planner + bundling rules post Case-00 |
| MCO-004 | P1 | Verified | Repeated rebuild of unchanged evidence and benchmark artifacts | Same Case-00 artifacts regenerated across runs | Wasted compute; longer missions; stale/confusion risk | Content-addressed cache; skip rebuild when hashes match | MCO-013, MCO-014 | Cache keys + skip policy after timeout/resume work |
| MCO-005 | P1 | Verified | Oversized repetitive prompts; no persistent project context | Long duplicated instructions across missions | Token waste; inconsistent agent behavior | Short templates + durable project profiles | MCO-019 | Draft LegalAI Mission Control profile |
| MCO-006 | P1 | Observed | Mission bundling where safe | Fragmented verify missions (see MCO-003) | Fewer missions → lower startup and scheduling overhead | Auto-bundle compatible read-only / same-surface tasks | MCO-020 | Define compatibility and blast-radius rules |
| MCO-007 | P0 | Verified | Automatic mission sizing and splitting before executor timeout | Work exceeds executor limits without pre-split | Hard failures mid-work; full restarts | Planner estimates size; split before submit | MCO-020 | Implement sizing heuristic after telemetry |
| MCO-008 | P0 | Verified | Dual ~600s timeouts with no persistence | Case-00 executor timeouts; work lost | Complete restart cost; duplicate spend | Persist progress before timeout; align timeout policy | MCO-009 | Design checkpoint schema |
| MCO-009 | P0 | Verified | No resume/checkpoint after timeout (restart from scratch) | Timed-out missions re-run full scope | Double spend on partial progress | Checkpoint + resume from last durable step | MCO-008 | Implement resume path for Mission Control executor |
| MCO-010 | P1 | Observed | Incremental verification by changed surface area | Full re-verify used when only subset changed | Unnecessary test/read volume | Diff-scoped verification against base commit | MCO-011 | Define surface-area mapping |
| MCO-011 | P2 | Observed | Fast / standard / release verification modes | One-size verification for all missions | Over-verify routine work; under-verify releases | Mode ladder: fast → standard → release | MCO-010 | Specify mode gates and required checks |
| MCO-012 | P0 | Observed | Missing cost, token, runtime, startup, retry, timeout telemetry | Spend visible only via credit balance | Cannot prioritize or prove savings | Emit structured metrics per mission | — | First implementation item after Case-00 approval |
| MCO-013 | P1 | Observed | No cache keyed by repo commit, case artifact hashes, pipeline version | Rebuilds ignore identical inputs (MCO-004) | Redundant generation/eval | Introduce keyed cache with explicit invalidation | MCO-014 | Define key schema + storage |
| MCO-014 | P1 | Observed | Cannot skip unchanged work deterministically | No hash/commit gate before expensive steps | Always-pay full pipeline cost | Deterministic skip when keys match | MCO-013 | Implement skip-unchanged guards |
| MCO-015 | P2 | Observed | Need built-in generation/evaluation pipeline with contamination boundaries | Ad-hoc Case-00 eval loops | Cross-contamination; weak reproducibility | First-class gen/eval pipeline with hard boundaries | MCO-013 | Spec contamination boundaries |
| MCO-016 | P0 | Verified | Temporary workspace success confused with pushed/persisted code | Local/executor success without authoritative persistence signal | False “done”; lost work; rework | Authoritative persistence reporting (branch, commit, push state) | — | Require persistence report on every mutating mission |
| MCO-017 | P1 | Observed | No preflight branch/base-commit freshness; unsafe rebase/replan | Stale bases cause conflict and redo | Failed merges; wasted missions | Preflight freshness check; safe rebase or replan | MCO-016 | Add preflight gate |
| MCO-018 | P1 | Observed | Blind repeated generations; no single bounded repair workflow | Retries regenerate without structured repair | Cost spikes; non-convergent loops | One bounded repair workflow; cap blind regenerations | MCO-012 | Define repair budget and stop conditions |
| MCO-019 | P2 | Observed | Missing reusable short mission templates / project profiles | Prompt duplication (MCO-005) | Inconsistent missions; token bloat | Curated templates + LegalAI project profile | — | Author templates after core reliability work |
| MCO-020 | P1 | Observed | No mission planner estimating files, tests, runtime, split need | Oversized missions hit timeouts (MCO-007/008) | Preventable executor failures | Planner: estimate scope → split or bundle | MCO-007, MCO-012 | Implement estimator using telemetry baselines |

---

## Prioritized Implementation Sequence

**Gate:** Begin implementation only after Case-00 is attorney-approved. Until then, maintain this register only; do not land optimization code under this authority.

1. **Telemetry / baseline** — MCO-012 (enables all ROI decisions); start MCO-001 measurement
2. **Timeout prevention and splitting** — MCO-007, MCO-020 (sizing)
3. **Resume / checkpointing** — MCO-008, MCO-009
4. **Caching / skip-unchanged** — MCO-013, MCO-014, MCO-004
5. **Incremental verification modes** — MCO-010, MCO-011
6. **Persistent profiles / templates** — MCO-019, MCO-005, MCO-002
7. **Safe bundling** — MCO-006, MCO-003
8. **Integrated benchmark pipeline** — MCO-015 (with contamination boundaries)

Supporting reliability tracks (schedule in parallel once telemetry exists): MCO-016 persistence reporting, MCO-017 preflight freshness, MCO-018 bounded repair.

---

# Mandatory Mission Operating Rules

## 1. Diagnose before implementation

- No implementation mission may be proposed until a read-only diagnosis has identified the verified root cause.
- Symptoms alone are not sufficient grounds for code changes.

## 2. One mission = one objective

- Do not combine diagnosis, implementation, focused tests, production verification, generation, and evaluation unless an explicit exception is recorded.

## 3. Two-strike loop-prevention rule

- If two implementation attempts target the same apparent symptom without eliminating it, stop.
- Return to read-only diagnosis before proposing another implementation.

## 4. Production evidence controls

- When synthetic tests pass but production verification fails, production evidence is authoritative.
- Diagnose the gap before changing code.

## 5. Smallest safe correction

- Modify only the function or configuration proven responsible.

## 6. Verify only what changed

- Production verification should focus on the changed behavior and its direct invariants.

## 7. Cost-aware mission design

- Treat every Mission Control execution as consuming time and money.
- Use the smallest repository surface, shortest prompt, narrowest tests, and least output necessary.

## 8. Generation gate

- Do not perform live generation until verified production blockers are cleared.

## 9. Transport failure is not mission failure

- A timeout, 504, or UI disconnect does not prove the underlying run failed.
- Check run status before submitting another mission.

## 10. Persistence verification

- A mission is complete only after:
  - tests pass (if applicable),
  - commit created,
  - push succeeds,
  - origin/main verified.

## 11. No repeated implementation without fresh diagnosis

- If the same production symptom survives two verified fixes, perform a new diagnosis before any additional implementation.

## 12. Architecture must be observed, not inferred

- Architecture must be observed, not inferred.
- Repository location, service roles, runtime ownership, workspace behavior, artifact paths, persistence, and deployment topology must be verified from authoritative sources (repository configuration, deployment metadata, or read-only inspection) before implementation or operational decisions.
- If architecture is ambiguous or undocumented, require a read-only architecture discovery mission before proceeding.

## 13. HAL Direct Submission

- HAL submits Mission Control missions directly.
- Mission prompts are implementation details and are not shown unless the user explicitly requests them.
- HAL reports only the objective, run ID, status, result, and next recommended action.

# Required Mission Sequence

1. Read-only diagnosis
2. Micro-fix
3. Focused tests
4. Read-only production verification
5. Live generation
6. Segregated evaluation

Stages should not be skipped or bundled without an explicit recorded reason.

# Verified Engineering Decisions

Verified parser fixes, retrieval decisions, architecture decisions, and benchmark lessons should be recorded here once and treated as project knowledge rather than repeatedly rediscovered.

# Loop Check Trigger

If the user asks:

> "Hal, are we looping?"

the current work must pause and:

- review the last two implementation attempts;
- determine whether they addressed the same symptom;
- return to diagnosis if appropriate before proposing another implementation.

---

## Decision Log

| Date | Decision |
|------|----------|
| 2026-08-05 | Adopt Architecture Verification (Rule 12): architecture must be observed, not inferred; verify from authoritative sources before implementation or operational decisions; require read-only architecture discovery when ambiguous or undocumented. |
| 2026-08-05 | Adopt HAL direct structured submission of Mission Control missions (Rule 13) to reduce copy/paste, schema errors, context bloat, and user eye strain. |
| 2026-08-05 | Adopt the Mandatory Mission Operating Rules; record creation of the durable generation CLI, separation of diagnosis from implementation, the production-first verification philosophy, execution-budget lessons learned, and the goal of reducing unnecessary Mission Control runs while preserving engineering knowledge. |
| 2026-08-03 | Finish Case-00 before any Mission Control optimization implementation. |
| 2026-08-03 | Create this authority file now as the single register for issues and optimizations. |
| 2026-08-03 | Optimize Mission Control before starting Case-01 and Case-02. |

---

## Maintenance Rules

1. Every future Mission Control optimization issue **must** receive a stable ID (`MCO-NNN`) in this register before work is planned or executed.
2. Each entry **must** include verified (or clearly labeled observed) evidence, impact, recommended correction, dependencies, and an owner / next action.
3. Status may move to **Implemented** only when the change has landed; to **Verified Complete** only when completion verification evidence is recorded in this file (or linked from the evidence column).
4. HAL updates this file through approved missions; do not rely on ad-hoc memory or chat history as the system of record.
5. Do not mark complete without completion verification.
