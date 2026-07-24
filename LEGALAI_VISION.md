# LegalAI Vision

**Scope note:** This document is derived only from evidence in the Mission Control repository. That repository treats Legal AI / LegalAI as the primary product Mission Control exists to accelerate; it does not contain a standalone LegalAI product codebase or a detailed LegalAI feature specification. Where detail is missing, this document says so rather than inventing it.

**Evidence sources:** `README.md`, `VISION.md`, `PRINCIPLES.md`, `ROADMAP.md`, `ANCHOR.md`, `ARCHITECTURE.md`, `MISSION_SPEC.md`, `docs/HAL_OPERATOR_LOG.md`, and Legal-AI references in mission examples under `missions/reference/`.

---

## Product vision

Legal AI is the primary product. Mission Control is infrastructure built to help create it faster and more safely—not the product itself.

From Mission Control’s stated purpose, Legal AI should deliver **attorney value**, prove useful through **validation with real matters**, and support a **successful business** (including revenue). Exact product form, workflows, and capabilities are **not specified** in this repository.

Legal AI is also the proving ground through which Mission Control earns the right to generalize. Success for Legal AI is therefore both a product outcome and the first real test of the AI development workflow around it.

---

## Problem being solved

This repository does **not** define a detailed end-user problem statement for Legal AI (for example, which legal tasks, practice areas, or failure modes the product addresses).

What *is* stated:

- Legal AI development should not be slowed by tooling distraction.
- Product work should stay oriented toward attorney usefulness and real-matter validation rather than infrastructure for its own sake.
- Engineering for Legal AI should be able to proceed under a disciplined, reviewable mission workflow (via Mission Control) when that workflow removes friction.

Anything beyond that—specific legal pain points, market gaps, or claimed AI legal capabilities—is **unsupported here** and left unspecified.

---

## Intended users

**Grounded signals:**

| Role / audience | What the repo says |
| --- | --- |
| Attorneys | Product success is framed around “attorney value,” “attorney validation,” and “validating with real matters.” |
| Allen | Product owner: sets priorities, product direction, and final approval. |
| Hal | Technical lead: architecture, planning, engineering judgment; operates Mission Control for Legal AI work. |
| Coding agents | Implement within approved mission boundaries; not decision-makers. |

**Ambiguous / not defined here:**

- End-customer segments (firm size, practice area, jurisdiction, consumer vs. enterprise).
- Whether attorneys are the only end users, or also collaborators, reviewers, or internal validators.
- Buyer vs. user distinction, pricing, or go-to-market model (beyond a high-level mention of revenue / successful business).

---

## Core product principles

These principles are inferred from how Legal AI is positioned relative to Mission Control and product priorities. They are product-facing restatements of repository rules—not inventing Legal AI feature rules.

1. **Attorney value first** — Work that does not improve attorney usefulness, real-matter validation, or business outcomes is secondary.
2. **Real-matter validation** — Usefulness is judged against real matters, not speculative capability claims.
3. **Product over infrastructure** — Mission Control (and similar tooling) continues only when it removes demonstrated friction from Legal AI; if tooling slows Legal AI, Legal AI takes priority.
4. **Human decision authority** — Priorities and final approval stay with humans (Allen / Hal roles as documented); agents execute within bounds.
5. **Safety and reviewability in how Legal AI is built** — Repository work should remain reversible, reviewable, and auditable; no irreversible engineering action without explicit approval. (This is an engineering/process principle evidenced for Legal AI development; it is not a claim about legal advice safety or regulatory compliance.)
6. **Earn claims through demonstrated need** — Capabilities and abstractions should follow proven need, not speculative expansion. (Stated for Mission Control’s evolution via Legal AI; apply the same caution to Legal AI product claims not evidenced in-repo.)

---

## Near-term priorities

From the operator log and priority docs:

1. **Execute LegalAI product work** as the primary focus once Mission Control is stable enough as an execution engine (`docs/HAL_OPERATOR_LOG.md`).
2. **Revisit Mission Control only** for blocking defects or strategic automation that removes the user from text loops—not as an open-ended distraction.
3. Keep attention on **attorney validation, product usefulness, and revenue**—Mission Control must not pull focus from those.

**Not defined in this repository:** a LegalAI feature backlog, UX roadmap, model/provider plan, data strategy, or release milestones for the LegalAI product itself.

**Repo signal only (not a product claim):** Mission examples name a repository `Legal-AI` with example base branch `contradiction-engine-v2`. The meaning, status, and product role of that branch/name are **not explained** here.

---

## Long-term direction

**Supported:**

- Legal AI remains the primary project and first proof point for the broader Mission Control vision.
- Successful Legal AI development is how Mission Control “earns generalization.”
- Legal AI should progress as a real product business (attorney value, validation, revenue)—not only as a demo for tooling.

**Unsupported / ambiguous in this repository:**

- Long-term Legal AI product architecture, feature set, or platform ambitions.
- Specific legal domains, jurisdictions, or compliance postures.
- Partnerships, customers, metrics, or legal/compliance certifications.
- Whether “LegalAI” and “Legal AI” / “Legal-AI” naming differences imply separate products (they appear to refer to the same primary product).

---

## Non-goals and boundaries

Explicit or strongly implied boundaries from this repository:

| Boundary | Basis |
| --- | --- |
| Mission Control is not the product | `VISION.md`, `PRINCIPLES.md` |
| Do not let Mission Control distract from attorney validation, product usefulness, or revenue | `README.md`, `PRINCIPLES.md` |
| Do not continue Mission Control work unless it removes demonstrated Legal AI friction | `ROADMAP.md`, `ANCHOR.md`, `VISION.md` |
| Coding agents do not set Legal AI product direction or grant themselves commit/push authority | Roles in `README.md` / `VISION.md` |
| Do not invent Legal AI capabilities, customers, metrics, partnerships, or compliance claims from this repo alone | Absence of such evidence; this document’s scope note |

**Also out of scope for claims based on this repo alone:**

- That Legal AI provides legal advice, replaces attorneys, or meets any regulatory standard.
- Any specific model accuracy, court acceptance, or privilege/confidentiality guarantees.
- Implementation details of a “contradiction engine” or other Legal-AI subsystems (name appears only as an example branch).

---

## Honesty check

| Topic | Status in this repository |
| --- | --- |
| Legal AI is the primary product | Clear |
| Built for attorney value / real-matter validation / business success | Clear at principle level |
| Concrete product vision (features, UX, architecture) | **Missing / ambiguous** |
| Target market and personas beyond “attorneys” | **Ambiguous** |
| Roadmap of Legal AI product milestones | **Missing** (Mission Control roadmap only) |
| Compliance, legal claims, partnerships, metrics | **Not present—do not invent** |

This file is a usable first vision for orientation inside the Mission Control context. A fuller LegalAI product vision should be authored or imported from the Legal-AI repository and evidence when available.
