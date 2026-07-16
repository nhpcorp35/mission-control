# Mission Control Architecture

## 1. Purpose

Mission Control coordinates AI-assisted software development by separating decision-making, workflow enforcement, and code execution into distinct layers.

## 2. System Architecture

```text
Allen
(Product Owner)
        │
        ▼
Hal
(Technical Lead)
        │
        ▼
Mission
(Engineering Contract)
        │
        ▼
Mission Control
(Workflow Engine)
        │
        ▼
Coding Agent
(Cursor / Codex / Claude / ...)
        │
        ▼
Isolated Git Worktree
        │
        ▼
Review
        │
        ▼
Approval
        │
        ▼
Repository