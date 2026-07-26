# Architectural Decisions

This document records significant architectural and strategic decisions for LegalAI and its supporting infrastructure.

The goal is to preserve **why** decisions were made, not simply **what** the current implementation is.

---

## 2026-07 — Mission Control Strategic Role

### Status

Accepted

### Decision

LegalAI is the primary product.

Mission Control exists to accelerate the development, testing, deployment, and operation of LegalAI.

Mission Control is not an independent development priority unless improvements directly benefit LegalAI.

### Rationale

As Mission Control has matured, it has become tempting to continue expanding it into a general-purpose AI development platform.

While that remains a long-term direction, LegalAI is the primary business objective.

Infrastructure work should therefore be prioritized only when it materially improves LegalAI development or operation.

### Development Allocation

Target allocation:

- LegalAI: 80–85%
- Mission Control: 15–20%

As a practical guideline:

- 5–6 development days per week on LegalAI
- 1–2 development days per week on Mission Control

### Decision Framework

Before beginning a Mission Control feature, ask:

> Will this materially improve LegalAI development or operation within the near term?

If the answer is **yes**, prioritize it.

If **no**, defer it.

---

## 2026-07 — Layered System Architecture

### Status

Accepted

### Architecture

```text
Allen
  ↓
HAL — Executive AI Layer
  ↓
Mission Control — Operations / Governance Layer
  ↓
Execution Agent — Cursor today
  ↓
LegalAI
```

### Responsibilities

#### Allen

- Defines business objectives
- Sets product direction
- Provides final approval where required

#### HAL

HAL is the executive AI layer responsible for:

- planning
- mission generation
- delegation
- coordination
- interpretation
- reducing manual operator effort

#### Mission Control

Mission Control is responsible for:

- validation
- governance
- permissions
- execution orchestration
- verification
- persistence
- audit trail

#### Execution Agents

Execution agents are responsible for implementation.

Current:

- Cursor

Possible future agents:

- Claude Code
- Codex
- OpenHands
- other compatible agents

---

## 2026-07 — Mission Control Roadmap

### Near-Term Priorities

1. Run cancellation
2. Schema tightening
3. Diff and artifact capture
4. Pull request persistence mode
5. Webhook infrastructure

These features provide immediate value to LegalAI development.

### Deferred

The following capabilities are intentionally postponed until LegalAI requires them:

- capability-based agent routing
- mission decomposition
- distributed execution
- enterprise RBAC
- Kubernetes and high-availability infrastructure
- multi-tenant architecture

Deferring these features keeps Mission Control focused on accelerating LegalAI rather than becoming infrastructure for its own sake.

---

## Guiding Principle

Mission Control succeeds when it continually reduces the manual effort required to build, test, verify, and deploy LegalAI.

Every significant enhancement should answer one question:

> Does this help us deliver LegalAI faster, more safely, or with less manual work?

If not, it should probably wait.
