# Mission Control Architecture Specification

This document is the **canonical architectural specification** for Mission Control.

It defines what Mission Control is, what it is responsible for, what it deliberately is not, how it fits into the HAL ecosystem, its architectural principles, its current capabilities, and its long-term direction.

Related contracts and operational detail live elsewhere and remain authoritative for their domains:

| Document | Role |
| --- | --- |
| `MISSION_SPEC.md` | Mission contract (YAML structure and semantics) |
| `docs/CANONICAL_MISSION_SCHEMA.md` | Schema and enforcement derived from the current implementation |
| `MISSION_CONTROL_API.md` | HTTP / MCP surface |
| `docs/HAL_OPERATOR.md` | HAL operating procedure |
| `docs/ARCHITECTURAL_DECISIONS.md` | Recorded strategic decisions |
| `PRINCIPLES.md` / `VISION.md` | Guiding principles and long-term vision |

Where this specification and an implementation detail disagree, treat repository behavior and the schema / API docs as the source of truth for current enforcement, and treat this document as the architectural framing.

---

## Overview

Mission Control is an **execution platform** for AI-assisted software engineering.

It is not an LLM, not a conversational assistant, and not a planning system. Planning systems such as HAL decide *what* should be done and author missions. Coding agents such as Cursor perform the implementation work. Mission Control sits between them as the **trusted execution layer**: it validates missions, enforces permissions and repository safety, coordinates isolated execution, verifies declared outcomes, applies platform Git persistence according to an explicit mode, and returns structured authoritative reports.

Mission Control converts an approved engineering mission into a controlled, auditable run. The mission is the contract. Mission Control enforces the contract. The agent executes within the contract. Humans retain decision authority over irreversible outcomes.

Mission Control exists as infrastructure. Its immediate purpose is to accelerate the development, testing, deployment, and operation of LegalAI. General-purpose ambition is earned through that work—not assumed.

---

## Primary Purpose

Mission Control’s primary purpose is to provide a **safe, reproducible execution path** from an engineering mission to verified repository outcomes.

Specifically, it:

- Accepts missions that describe objective, scope, permissions, deliverables, persistence, and approval requirements.
- Rejects invalid or unauthorized work before execution begins.
- Runs coding agents in controlled conditions without trusting agent prose as proof.
- Applies platform-level Git persistence (`none`, `commit`, or `push`) independently of agent claims.
- Returns objective evidence that operators and planning systems can trust.

In the HAL ecosystem, Mission Control is the operations and governance layer: HAL plans and delegates; Mission Control validates, governs, orchestrates, verifies, persists, and reports; execution agents implement.

---

## Core Responsibilities

Mission Control is responsible for the following concerns. Each is expanded in later sections.

1. **Mission validation** — Structural and eligibility checks so invalid missions never execute.
2. **Permission enforcement** — Deny-by-default agent controls and constraint text aligned to the mission.
3. **Execution coordination** — Queuing, isolated workspaces, agent invocation, and lifecycle status.
4. **Verification** — Deliverable and platform checks after agent success, before completion.
5. **Persistence management** — Platform Git actions controlled by `persistence.mode`, not by agent Git flags.
6. **Authoritative reporting** — Structured run results and summaries that operators prefer over agent stdout.
7. **Repository safety** — Isolation, approval gates for privileged actions, and rejection of recursive or unsafe submissions.
8. **Auditability** — Durable run records, stored mission YAML for retries, and evidence retained on failure where collected.

Mission Control does **not** invent product requirements, choose LegalAI roadmap priorities, or replace human approval for irreversible policy decisions.

---

## Mission Validation

Every engineering activity begins with a mission. A mission is a contract, not a prompt and not a conversation.

Before execution, Mission Control validates:

- Mission Spec version support (currently `1.0`)
- Required top-level structure (`version`, `mission_id`, `title`, `repository`, `execution`, `permissions`, `instructions`, `deliverables`, `approval`, plus optional `persistence` / `documentation`)
- Execution eligibility for the requested path (`plan` vs `execute`, agent, worktree policy)
- Permission sets appropriate to the path (including read-only execute and push-only exceptions)
- `persistence.mode` values (`none`, `commit`, `push`) when provided
- Platform-push approval when `persistence.mode` is `push`
- Repository path / clone prerequisites for the chosen execution path
- Preflight conditions for the configured agent (for example Cursor CLI and API key availability)

Invalid missions must never execute. Structural validation (`POST /validate`, `mc.py validate`) is shallow by design; run and execute eligibility add the gates required for safe operation. Full field-level rules are defined in `docs/CANONICAL_MISSION_SCHEMA.md` and `MISSION_SPEC.md`.

Preferred production submission for routine execute work is structured acceptance (`POST /runs/structured` / MCP `submit_structured_run`), which renders Mission Spec v1.0 YAML with safe execute defaults. Raw YAML (`POST /runs` / MCP `submit_run`) remains fully supported when exact document control is required.

---

## Permission Enforcement

Permissions are **deny-by-default agent controls**. They describe what the coding agent may do inside the workspace. They do **not** authorize Mission Control’s platform Git persistence.

Core agent file controls:

| Permission | Role |
| --- | --- |
| `create_files` | Agent may create files |
| `modify_files` | Agent may modify existing files |
| `delete_files` | Agent may delete files (forbidden for current execute eligibility) |
| `read` / `run_commands` | Inspection and non-mutating command use (conventional / constrained by path) |

Legacy agent Git flags (`stage_changes`, `commit`, `push`) are ignored for platform persistence. Clients must select platform Git behavior with `persistence.mode`.

Mission Control appends constraint text to agent instructions based on the allowed create/modify combination (create-and-modify, modify-only, create-only, or read-only). Plan / read-only paths keep mutation permissions false. Genuine read-only execute missions are supported with an exact permission set so inspection and planning can run through the async execute pipeline without writes.

Enforcement is layered: eligibility validation rejects unauthorized missions; instruction constraints constrain the agent; platform persistence and deliverable gates do not trust agent compliance alone.

---

## Execution Coordination

Mission Control coordinates the execution lifecycle without performing the implementation work itself.

### Trusted execution layer

```text
Planning (HAL / operators)
        │
        ▼
Mission Spec (contract)
        │
        ▼
Mission Control (validate → enforce → orchestrate → verify → persist → report)
        │
        ▼
Execution agent (Cursor today; replaceable)
        │
        ▼
Isolated workspace → optional platform persistence → remote / local sync
```

### Preferred async execute lifecycle (`POST /runs`)

1. **Accept** — Structural validation + execute eligibility + preflight; reject recursive local submission.
2. **Queue** — FIFO acceptance; only one Cursor execution active at a time.
3. **Prepare workspace** — Clone the configured repository URL at `repository.base_branch` into an isolated temporary directory.
4. **Execute** — Invoke the coding agent against that workspace with mission instructions and permission constraints.
5. **Verify** — Confirm declared file deliverables exist in the workspace (see Verification).
6. **Persist** — Apply platform persistence per `persistence.mode` (see Persistence Management).
7. **Report** — Store authoritative structured evidence and lifecycle status.
8. **Cleanup** — Delete the temporary workspace.

Legacy synchronous paths (`POST /run` for plan-mode inspection; `POST /execute` for in-place execute) remain for compatibility. Prefer isolated async `POST /runs` (and structured / submit-and-wait variants) for production mutation workflows so persistence and deliverable gates apply.

Mission Control must not depend on a single coding agent. Cursor is the current adapter. The mission contract stays stable; only the execution adapter changes when future agents are introduced.

---

## Verification

Mission Control treats **repository state and platform checks** as evidence. Agent stdout is diagnostic, not proof.

For asynchronous `POST /runs` (and the shared registered-run lifecycle):

1. Workspace preparation must succeed.
2. Agent execution must return success.
3. Declared **file** deliverables must exist as regular files under the isolated workspace (typed `file:` / `kind: file` preferred; bare path-like strings supported conservatively).
4. Only then may platform persistence run.
5. Only after successful persistence (when attempted) may the run be marked `completed`.

Missing file deliverables fail the run before persistence with a machine-readable error. Descriptive deliverables are not checked on disk. File *content* is not validated—only presence of regular files at safe relative paths. Absolute or escaping paths are not read outside the workspace.

Operators should independently verify significant claims (tests, source inspection, Git history) per `docs/HAL_OPERATOR.md`. Mission Control’s gates reduce false completion; they do not replace engineering judgment.

Optional documentation policy (`documentation.mode: none | required`) adds agent instructions and authoritative `result.documentation` evidence. Path heuristics classify documentation-looking changes; agent prose is never treated as verified documentation proof.

---

## Persistence Management

Platform Git persistence is a Mission Control responsibility, applied **after** successful agent execution and deliverable verification on the isolated async path.

Supported modes:

| Mode | Stage | Local commit | Push to `repository.base_branch` |
| --- | --- | --- | --- |
| `none` | no | no | no |
| `commit` | yes if dirty | yes if dirty | no |
| `push` | yes if dirty | yes if dirty | yes if a commit was created |

Rules:

- Omitted `persistence` on raw Mission Spec YAML resolves to `none`.
- Structured submission may infer `push` for create/modify missions and `none` for read-only when `persistence_mode` is omitted; explicit values are never overridden.
- Clean working trees succeed with `commit_sha: null` and no Git mutations.
- `push` requires explicit platform-push approval (`approval.platform_push_approved: true` or `approval.allow_automatic_platform_push: true`), checked at eligibility and again at the persistence boundary.
- Agent `permissions.push` / `permissions.commit` never authorize platform persistence.
- `commit` never pushes.
- Legacy `POST /execute` does not apply `persist_workspace_changes`; prefer `POST /runs` when platform persistence is required.

`persistence.mode: push` is privileged: it can update the shared remote and trigger downstream deploy / sync. It must remain gated.

---

## Authoritative Reporting

Every accepted async run has a lifecycle (`queued`, `running`, `completed`, `failed`, `timed_out`) and, when evidence is collected, a structured result.

**Trust boundary:**

| Field | Trust |
| --- | --- |
| `summary`, `result`, `commit_sha` | Authoritative Mission Control evidence |
| `stdout`, `stderr` | Agent-authored diagnostics (captured before platform persistence when persistence runs) |

Prefer `summary`, `result.persistence`, and `commit_sha` when judging whether platform commit/push occurred. Agent stdout may correctly say the agent did not commit while Mission Control still performed a successful platform persist.

Structured `result` typically includes:

- `files_changed` — paths changed in the isolated workspace
- `commands` — subprocesses Mission Control executed (for example the Cursor agent)
- `deliverables` — file-deliverable verification outcome
- `persistence` — mode, attempted/ok, `commit_sha`, `pushed`
- `documentation` — requested mode and status when documentation policy applies
- `warnings` — explicit limitations (never fabricated metrics)
- `summary` — Mission Control-authored text aligned with persistence outcome

Failed and timed-out runs retain partial evidence actually collected. Retries create a new run from stored mission YAML of a failed run without mutating the source record.

HAL and operators should interpret completion from these fields and from repository state—not from conversational agent summaries alone.

---

## What Mission Control Is Not

Mission Control deliberately is **not**:

- An LLM or foundation model
- A conversational chat product or general assistant
- A planning / executive reasoning system (that is HAL’s role)
- The product being built (LegalAI is the product; Mission Control is infrastructure)
- A replacement for human product ownership or final approval
- An unbounded autonomous coder with repository write access by default
- A substitute for tests, code review, or operator verification
- A multi-tenant enterprise control plane (not a current claim)
- A Kubernetes / HA platform (deferred until product need justifies it)

Mission Control may *invoke* an LLM-backed coding agent, but the platform’s job is governance and execution fidelity—not dialogue.

---

## Architecture

### Layered system

```text
Allen — Product owner (priorities, final approval)
  ↓
HAL — Executive / planning layer (missions, delegation, interpretation)
  ↓
Mission Control — Trusted execution / governance layer
  ↓
Execution agent — Cursor today (implementation within bounds)
  ↓
Isolated Git workspace → platform persistence → GitHub
  ↓
HAL Sync → local repository → Obsidian (operator workspace)
```

### Responsibility split

| Layer | Owns |
| --- | --- |
| Allen | Business objectives, product direction, irreversible approvals |
| HAL | Planning, mission generation, coordination, result interpretation, follow-up missions |
| Mission Control | Validation, permissions, orchestration, verification, persistence, authoritative reporting |
| Execution agent | Implementation and investigation inside mission constraints |
| GitHub / Git | Shared source of truth for published changes |
| HAL Sync | Safe fast-forward of configured local clones from `origin` |
| Local repo + Obsidian | Operator-visible working copy and notes over synced files |

### Internal architectural ideas

- **Mission as contract** — Stable YAML/spec surface; adapters change underneath.
- **Deny by default** — Permissions and privileged persistence require explicit grants.
- **Isolation for async execute** — Fresh clone per run; no shared mutable workspace across runs.
- **Single active agent execution** — FIFO queue to keep Cursor concurrency bounded.
- **Platform persistence separate from agent Git** — Clear trust and approval boundaries.
- **Evidence over prose** — Structured results outrank agent narrative.

Detailed endpoint behavior is specified in `MISSION_CONTROL_API.md`. Schema enforcement detail is in `docs/CANONICAL_MISSION_SCHEMA.md`.

---

## Current Capabilities

The following capabilities exist in the current production-oriented system (HTTP API, MCP tools, CLI validation/run helpers, and the async run pipeline). This list describes what is implemented today—not a wishlist.

- Mission Spec v1.0 structural validation
- Plan-mode synchronous inspection (`POST /run`) and execute eligibility gates
- Asynchronous execute runs with isolated workspaces (`POST /runs`)
- Structured mission submission with safe defaults (`POST /runs/structured`)
- Submit-and-wait and server/MCP wait helpers for operator / HAL flows
- Cursor agent adapter (current execution backend)
- Permission constraint injection for create/modify/read-only sets
- Platform persistence modes: `none`, `commit`, `push`
- Platform-push approval enforcement (eligibility + persistence boundary)
- File deliverable verification before persistence on the async path
- Optional documentation policy (`none` / `required`) with structured result fields
- Authoritative structured run results and summaries
- Run lookup, retry of failed runs from stored YAML, FIFO queueing
- Recursive submission rejection and credential isolation from agent subprocesses
- Public health / validate endpoints; API-key protection for execution surfaces
- Custom GPT Actions–oriented OpenAPI view for HAL / ChatGPT operation import
- HAL Sync service (macOS LaunchAgent) for safe local fast-forward sync
- Operator procedure and operator log conventions for verified outcomes

Known limitations of the current phase (non-exhaustive): shallow structural schema checking; legacy `POST /execute` without platform persistence; worktrees not supported as a mission-requested execution feature on current paths; single-agent focus (Cursor) in eligibility; no general multi-tenant RBAC or distributed scheduler.

---

## Design Principles

These principles govern Mission Control architecture. They align with `PRINCIPLES.md` and `docs/ARCHITECTURAL_DECISIONS.md`.

1. **Product first** — LegalAI is the primary product; Mission Control earns its keep by reducing friction for that product.
2. **Execution platform, not assistant** — Govern and run missions; do not chat as the product interface.
3. **Safety before speed** — Repository integrity outranks automation convenience.
4. **Mission driven** — Work starts from an explicit contract with permissions, deliverables, and approvals.
5. **Deny by default** — Privileges are explicit; push is gated twice when used.
6. **Evidence over claims** — Prefer platform `summary` / `result` / Git state to agent stdout.
7. **Agent independence** — The executor is replaceable; the mission remains the stable interface.
8. **Human decision authority** — Humans set priorities and approve irreversible policy; agents execute within bounds.
9. **Simplicity over speculation** — Add infrastructure when real workflow pain demands it.
10. **Reproducibility and audit** — Runs, stored YAML, and operator logs make outcomes reviewable.
11. **Earn generalization** — Broad platform ambitions follow repeated success on LegalAI—not the reverse.

---

## Current Production Workflow

End-to-end production flow as operated today:

```text
HAL (plan / author mission)
  → Mission Control (validate, queue, isolate, run Cursor, verify, persist, report)
  → Cursor (implement inside constraints)
  → Mission Control platform persistence (none | commit | push)
  → GitHub (shared remote when push succeeds)
  → HAL Sync (fast-forward local clone on Allen’s Mac when clean)
  → Local repository
  → Obsidian (operator notes / visibility over the synced tree)
```

Operational notes:

- HAL prefers structured submission for routine execute missions, or exact YAML via submit-and-wait when the full document is already available.
- Mutating structured missions without an explicit persistence mode resolve to `push` and still require platform-push approval.
- After push, GitHub holds the published state. HAL Sync never force-resets or merges; it only fast-forwards clean local trees.
- Obsidian is not part of Mission Control’s server runtime; it is the local operator surface over the synced repository.
- HAL interprets authoritative Mission Control results, verifies significant claims against the repository, updates `docs/HAL_OPERATOR_LOG.md` when required, and submits follow-up missions without treating agent chat as proof.

This workflow keeps planning (HAL), governance (Mission Control), implementation (Cursor), publication (GitHub), and local visibility (HAL Sync + Obsidian) as distinct stages.

---

## Future Direction

Mission Control’s long-term direction is to become a general **AI project operating system**: coordinating human decision-making, technical leadership, missions, replaceable coding agents, repository safety, verification, and approval into one reproducible workflow.

That goal is **aspirational**. It is not a claim about features that already exist.

Near-term improvements under consideration (when they remove demonstrated LegalAI friction) include areas such as:

- Run cancellation
- Schema tightening
- Diff and artifact capture
- Pull request persistence mode
- Webhook infrastructure

Deferred until product need justifies them:

- Capability-based agent routing
- Mission decomposition
- Distributed execution
- Enterprise RBAC
- Kubernetes / high-availability topology
- Multi-tenant architecture

Guiding test for future work:

> Does this help deliver LegalAI faster, more safely, or with less manual work?

If not, it should wait.

Mission Control succeeds when operators stop managing tools and start trusting a predictable execution path—from mission contract through verification and persistence—while LegalAI remains the product that matters.

---

*End of canonical specification.*
