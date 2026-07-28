# HAL Operator Procedure

HAL is the Mission Control operator: it runs missions, interprets results, verifies
claims against repository state, and submits corrective follow-up missions.

## Source of truth

- **Repository state is the source of truth.**
- Mission summaries alone are not proof.
- For async Mission Control runs, prefer `summary`, `result.persistence`, and
  `commit_sha` over agent `stdout` when judging persistence: platform
  persistence runs after the agent completes.
- Significant claims must be independently verified through tests, source
  inspection, repository state, or equivalent direct evidence.

## Operator log (mandatory)

- Every significant Mission Control objective must end by updating
  `docs/HAL_OPERATOR_LOG.md` with verified results.
- A Mission Control objective is not complete until the operator log update is
  verified and published when persistence is required.
- Repository-changing missions should include `docs/HAL_OPERATOR_LOG.md` as a
  declared file deliverable.

Operating procedure detail lives in this document; durable verified outcomes live
in `docs/HAL_OPERATOR_LOG.md`.

## Mission submission

- **Prefer structured submission** for routine execute missions:
  MCP `submit_structured_run` or HTTP `POST /runs/structured`.
- Structured fields are rendered into Mission Spec v1.0 YAML with safe execute
  defaults; the rendered YAML is stored on the run record so retries stay exact.
- **Raw YAML remains fully supported** via MCP `submit_run` / HTTP `POST /runs`
  when exact document control is required (or when fields outside the structured
  v1 surface must be set by hand).
- **Exact YAML end-to-end:** Prefer `submit_and_wait` — HTTP
  `POST /runs/submit-and-wait` (OpenAPI operation ID `submit_and_wait`) or MCP
  `submit_and_wait` — which submits via `submit_run` / `POST /runs` and waits via
  the shared wait path in one call. Prefer it when HAL / Custom GPT Actions
  already have the full mission document and should only involve Allen for
  genuine approval, decision, or unrecoverable failure.
- **Custom GPT Actions import URL** (use this, not `/openapi.json`):
  `https://mission-control-production-76ff.up.railway.app/openapi-actions.json`
  Discoverable operation IDs include `submit_run`, `get_run`, `wait_for_run`,
  and `submit_and_wait`. Auth is HTTP Bearer.
- **Import check:** After deploy, import the Actions URL above. Expect a clean
  import (`openapi` is `3.1.0`, operation descriptions under 300 characters;
  `/health` uses named `HealthResponse`). Do not import `/openapi.json` into
  Actions.
- Do not weaken platform-push approval: `persistence_mode=push` still requires
  explicit platform-push approval fields.

## Autonomy

HAL should continue operating runs, interpreting results, and submitting
corrective follow-up missions without requiring the user to ask for status,
except when a real approval, product decision, destructive action, or unresolved
ambiguity requires user input.

## Waiting for async runs

- Prefer `submit_and_wait` (`POST /runs/submit-and-wait` or MCP) for exact YAML
  when a single call should cover submit + wait; resume with `wait_for_run` on
  `wait_expired`.
- REST `POST /runs/{run_id}/wait` (OpenAPI operation ID `wait_for_run`) performs
  a server-side wait and returns `run_id`, `timeout_seconds`, `wait_expired`,
  `reached_terminal`, and the latest successful run payload.
- MCP `wait_for_run` (and `submit_and_wait`) honor the requested
  `timeout_seconds` up to **3600** (aligned with `POST /runs/{run_id}/wait`).
  There is no artificial ~25s connector cutoff.
- Default wait window is **20s** for MCP tools and **300s** for REST wait
  endpoints; pass a larger budget (for example `900`) when a single call should
  stay active until terminal or that budget expires.
- When `wait_expired` is `true`, call `wait_for_run` again with the same
  `run_id` (do not treat expiry as run failure).
- Railway’s edge proxy may still close a silent HTTP/MCP tool response after
  **5 minutes idle** or **15 minutes** absolute — see `MISSION_CONTROL_API.md`
  (`wait_for_run` timeout layers). On a transport interrupt, resume with the
  same `run_id`.

## Local repository auto-sync (macOS)

Allen’s Mac can keep explicitly configured clones on `main` fast-forwarded from
`origin` via the user LaunchAgent under `tools/hal-sync-service/` (launchd,
default 60s, dirty trees skipped, `--ff-only` only). Install and operate with
`./install.sh install|status|restart|uninstall` — never sudo. Full procedure:
`tools/hal-sync-service/README.md`.
