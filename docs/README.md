# Mission Control Documentation

Mission Control is an AI execution platform. This directory is the canonical
landing page and primary entry point for its documentation, organized by
architectural responsibility.

---

## Core Architecture

| Document | Role |
| --- | --- |
| [MISSION_CONTROL_SPECIFICATION.md](MISSION_CONTROL_SPECIFICATION.md) | Canonical architectural specification describing Mission Control, its responsibilities, boundaries, and long-term direction. |
| [ARCHITECTURAL_DECISIONS.md](ARCHITECTURAL_DECISIONS.md) | Records significant architectural decisions and the reasoning behind them. |

---

## Mission Definition

| Document | Role |
| --- | --- |
| [CANONICAL_MISSION_SCHEMA.md](CANONICAL_MISSION_SCHEMA.md) | Canonical definition of the Mission Specification format used by Mission Control. |

---

## HAL Integration

| Document | Role |
| --- | --- |
| [HAL_OPERATOR.md](HAL_OPERATOR.md) | Operational guide describing how HAL creates, submits, and interprets missions. |
| [HAL_OPERATOR_LOG.md](HAL_OPERATOR_LOG.md) | Historical record of HAL integration milestones and operational improvements. |

---

## Testing & Verification

These maintained verification artifacts provide historical verification of
significant platform capabilities:

| Document | Role |
| --- | --- |
| [HAL_SYNC_TEST.md](HAL_SYNC_TEST.md) | Historical verification of Mission Control automatic synchronization. |
| [MC_POST_FIX_TEST.md](MC_POST_FIX_TEST.md) | Historical verification of post-fix structured persistence. |
| [MC_POST_FIX_TEST1.md](MC_POST_FIX_TEST1.md) | Historical verification of post-fix structured persistence. |

---

## Repository Structure

| Path | Responsibility |
| --- | --- |
| `app/` | HTTP API |
| `mission_control/` | Validation, execution, verification, persistence |
| `mcp_connector/` | MCP integration layer |
| `docs/` | Canonical documentation |
| `tests/` | Regression and verification tests |
| `tools/` | Operational utilities |

---

## Documentation Principles

- Maintain one canonical source for each architectural topic.
- Prefer references over duplicated documentation.
- Separate architecture, implementation, and operational guidance.
- Distinguish implemented behavior from future direction.
- Keep documentation synchronized with platform evolution.

---

## Recommended Reading Order

1. [MISSION_CONTROL_SPECIFICATION.md](MISSION_CONTROL_SPECIFICATION.md)
2. [CANONICAL_MISSION_SCHEMA.md](CANONICAL_MISSION_SCHEMA.md)
3. [HAL_OPERATOR.md](HAL_OPERATOR.md)
4. [ARCHITECTURAL_DECISIONS.md](ARCHITECTURAL_DECISIONS.md)
5. [HAL_OPERATOR_LOG.md](HAL_OPERATOR_LOG.md)

This order helps new contributors understand what Mission Control is, how
missions are defined, how HAL operates the platform, why the architecture
evolved, and how the platform has matured.
