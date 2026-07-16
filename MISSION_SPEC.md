# Mission Specification

Version: 1.0

---

# Purpose

The Mission Specification defines the contract between Hal, Mission Control, and the coding agent.

Every engineering activity begins with a mission.

The mission describes the work.

Mission Control enforces the mission.

The coding agent executes the mission.

The repository receives only approved work.

The specification is intentionally independent of Cursor, Codex, Claude Code, or any future coding agent.

---

# Mission Lifecycle

Every mission follows the same lifecycle.

```
Allen
    ↓
Hal
    ↓
Mission
    ↓
Mission Control
    ↓
Coding Agent
    ↓
Worktree
    ↓
Review
    ↓
Approval
    ↓
Repository
```

---

# Mission Structure

Every mission contains the following sections.

```yaml
version:

mission_id:

title:

repository:

execution:

permissions:

instructions:

deliverables:

approval:
```

Mission Control must reject missions that do not satisfy the specification.

---

# Version

Identifies the specification version.

Example

```yaml
version: 1.0
```

Mission Control must reject unsupported versions.

---

# Mission ID

A unique identifier.

Recommended format:

```
YYYY-MM-DD-###
```

Example

```yaml
mission_id: 2026-07-16-001
```

Mission IDs should never be reused.

---

# Title

A concise description.

Example

```yaml
title: Repository Verification
```

---

# Repository

Defines the target repository.

Example

```yaml
repository:

  name: Legal-AI

  path: /Users/allenk/Desktop/Legal-AI

  base_branch: contradiction-engine-v2
```

Mission Control must verify:

- repository exists
- Git repository
- branch exists

before execution begins.

---

# Execution

Defines how the mission executes.

Example

```yaml
execution:

  agent: cursor

  mode: plan

  sandbox: true

  worktree: true
```

Supported modes:

```
ask

plan

execute
```

### ask

Read-only questions.

### plan

Read-only investigation.

### execute

Repository modifications allowed if permissions permit.

---

# Permissions

Permissions are deny-by-default.

Example

```yaml
permissions:

  read: true

  create_files: false

  modify_files: false

  delete_files: false

  run_commands: true

  stage_changes: false

  commit: false

  push: false
```

Mission Control enforces permissions regardless of agent behavior.

---

# Instructions

Instructions are written by Hal.

They describe:

- objective
- scope
- constraints
- required documents
- stopping conditions

Example

```yaml
instructions: |

  Read VISION.md.

  Read ANCHOR.md.

  Verify repository status.

  Do not modify files.
```

---

# Deliverables

Defines the required outputs.

Example

```yaml
deliverables:

  - repository status

  - discrepancies

  - files examined

  - Git status

  - recommendations
```

Mission Control marks the mission incomplete if required deliverables are missing.

---

# Approval

Example

```yaml
approval:

  execute_without_approval: true

  commit_requires_approval: true

  push_requires_approval: true
```

Approval to execute is not approval to commit.

Approval to commit is not approval to push.

Each approval is independent.

---

# Safety Requirements

Mission Control must always enforce:

- isolated Git worktrees
- repository verification
- permission validation
- mission validation
- complete result capture
- review before irreversible actions

---

# Result Package

Every mission produces a result package.

Recommended structure:

```
results/

    <mission-id>/

        mission.yaml

        report.md

        transcript.jsonl

        git-status.txt

        diff.patch

        tests.txt

        usage.json
```

Mission results become permanent engineering records.

---

# Agent Independence

Mission Control does not depend on any specific coding agent.

Supported agents may include:

- Cursor
- Codex
- Claude Code
- Future systems

The Mission Specification remains unchanged.

Only the execution adapter changes.

---

# Validation Rules

Before execution Mission Control verifies:

- mission syntax
- supported specification version
- repository exists
- repository clean enough for requested operation
- permissions valid
- execution mode valid

Invalid missions must never execute.

---

# Engineering Philosophy

A mission is not a prompt.

A mission is not a conversation.

A mission is a contract.

Hal defines the engineering mission.

Mission Control enforces it.

The coding agent executes it.

Allen reviews and approves the outcome.

The repository records the approved work.

---

# Guiding Principle

The mission is permanent.

The executor is replaceable.