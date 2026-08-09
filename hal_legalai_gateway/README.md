# HAL LegalAI Gateway (Phase 2)

Thin standalone gateway that **consolidates the interface, not the implementation**.

Bridge, Storage, Mission Control, and artifact retrieval remain separately
deployed, testable, and replaceable. Phase 2 adds an **authenticated remote MCP
endpoint** with stable namespaced tools that forward to those downstream MCP
servers. No Case-00 generation, archive mutation, B2, or mission execution logic
is copied into the gateway.

## What Phase 2 provides

| Surface | Purpose |
| --- | --- |
| `POST/GET /mcp` | Authenticated Streamable HTTP MCP endpoint (FastMCP + GitHub OAuth) |
| Namespaced tools | Settled `case.*`, `storage.*`, `mission.*` surface with thin forwarding |
| `registry.json` | Machine-readable map of namespaces → downstream service IDs, URL env vars, health paths, and tool bindings |
| `GET /registry` | Serve the registry plus resolved (non-secret) downstream URLs |
| `GET /health` | Gateway liveness + **independent** downstream status; reports exact `RAILWAY_GIT_COMMIT_SHA` and **runtime registered tool names** (no secrets) |
| Request IDs | `X-Request-ID` / `X-Correlation-ID` middleware; every forwarded call returns correlation metadata |
| Config validation | Fail closed on missing OAuth/service secrets, invalid timeouts, or non-http(s) URLs |

## Settled tool surface (minimum)

| Gateway tool | Downstream service | Downstream MCP tool |
| --- | --- | --- |
| `case.get_artifact` | artifacts | `get_case_artifact` |
| `storage.archive_feedback` | storage | `archive_case00_attorney_feedback` |
| `storage.archive_review_packet` | storage | `archive_case00_review_packet` |
| `storage.verify_archive` | storage | `list_case00_storage` (closest truthful inventory verification) |
| `mission.submit` | mission_control | `submit_run` |
| `mission.status` | mission_control | `get_run` |

Also exposed: Case-00 lifecycle (`case.submit_case00_q1`, `case.get_case00_q1_run`,
`case.cancel_case00_q1_run`, `case.get_case00_q1_artifacts`, `case.get_artifacts`),
`storage.list_inventory`, and mission helpers (`mission.submit_structured`,
`mission.wait`, `mission.submit_and_wait`, `mission.run_repository_command`).

## Authentication and forwarding

**Inbound (required):** FastMCP `GitHubProvider` OAuth — the same ChatGPT Business
custom MCP pattern as the Bridge. Write tools are never exposed unauthenticated.
Missing GitHub OAuth configuration fails startup (fail closed). A static inbound
`GATEWAY_API_KEY` is **not** used.

**Downstream Bridge / Storage / Artifacts:** After inbound GitHub OAuth succeeds,
the gateway authenticates to Bridge with a **dedicated non-expiring service
credential** (`GATEWAY_BRIDGE_AUTHORIZATION`), which must match Bridge
`BRIDGE_SERVICE_TOKEN`. Calls go to the Bridge **service-only** MCP path
(`/mcp/service` by default via `GATEWAY_MCP_PATH`) protected by a FastMCP 2.x
`TokenVerifier` — **not** the public GitHub OAuth `/mcp` surface. The inbound
user OAuth session token is **never** forwarded downstream (different audience;
expires with the user session).

**Downstream Mission Control:** The MCP connector authenticates to the Mission
Control HTTP API with its own server-side `MISSION_CONTROL_API_KEY`. The gateway
gates `mission.*` with inbound GitHub OAuth only and does not put that Mission
Control key on the public wire. Mission Control MCP stays on `/mcp`
(`GATEWAY_MISSION_CONTROL_MCP_PATH`, default `/mcp`).

**Direct Bridge clients:** GitHub OAuth on Bridge `/mcp` remains fully
compatible. Gateway traffic must use `/mcp/service` only.

## Failure isolation and observability

Every forwarded call returns (and logs) at least:

- `request_id` / `correlation_id`
- `downstream_service` / `downstream_tool`
- `duration_ms`
- `failure_stage` (`unconfigured` | `auth` | `connect` | `timeout` | `http` |
  `protocol` | `tool` | `parse` | `internal` | `null` on success)

A failure in one namespace/tool does not cancel or poison other tools. Downstream
health probes remain concurrent and independent (Phase 1 behavior preserved).
Secrets are redacted from logs, error envelopes, and health payloads.

## Namespaces → downstreams

| Namespace | Downstream key | Default URL env |
| --- | --- | --- |
| `case` | `bridge` (artifacts for `case.get_artifact*`) | `GATEWAY_BRIDGE_URL` / `GATEWAY_ARTIFACTS_URL` |
| `storage` | `storage` | `GATEWAY_STORAGE_URL` |
| `mission` | `mission_control` | `GATEWAY_MISSION_CONTROL_URL` |

`GATEWAY_MISSION_CONTROL_URL` must point at the Mission Control **MCP**
Streamable HTTP base (`SERVICE_MODE=mcp`), not only the REST API service.

## Configuration (Railway variables)

### Gateway service

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `GITHUB_OAUTH_CLIENT_ID` | **yes** | — | Inbound GitHub OAuth client (ChatGPT custom MCP) |
| `GITHUB_OAUTH_CLIENT_SECRET` | **yes** | — | Inbound GitHub OAuth secret (never log) |
| `GATEWAY_PUBLIC_URL` | **yes** | — | Public `https://…` base URL for OAuth callbacks / metadata |
| `JWT_SIGNING_KEY` | **yes** | — | FastMCP JWT signing key for inbound OAuth tokens |
| `REDIS_HOST` | **yes** | — | Redis host for OAuth client storage (same pattern as Bridge) |
| `REDIS_PORT` | no | `6379` | Redis port |
| `STORAGE_ENCRYPTION_KEY` | **yes** | — | Fernet key for encrypted OAuth client storage |
| `GATEWAY_BRIDGE_AUTHORIZATION` | **yes** | — | Dedicated service Bearer for Bridge/Storage/Artifacts; must match Bridge `BRIDGE_SERVICE_TOKEN`; **not** a user OAuth token |
| `ALLOWED_GITHUB_LOGIN` | no | `nhpcorp35` | GitHub login allowed to invoke gateway tools |
| `PORT` | no | `8080` | Listen port |
| `RAILWAY_GIT_COMMIT_SHA` | no | `unknown` | Exact deployed SHA when Railway injects it |
| `GATEWAY_HEALTH_TIMEOUT_SECONDS` | no | `5` | Per-probe timeout (`0.1`–`30`) |
| `GATEWAY_CONNECT_TIMEOUT_SECONDS` | no | `5` | Downstream MCP connect timeout (`0.1`–`30`) |
| `GATEWAY_READ_TIMEOUT_SECONDS` | no | `30` | Downstream MCP read timeout (`0.1`–`120`, ≥ connect) |
| `GATEWAY_MCP_PATH` | no | `/mcp/service` | Bridge/Storage/Artifacts Streamable HTTP path (service-only TokenVerifier surface) |
| `GATEWAY_MISSION_CONTROL_MCP_PATH` | no | `/mcp` | Mission Control Streamable HTTP path |
| `GATEWAY_BRIDGE_URL` | no | production bridge URL from registry | Absolute `http(s)` base URL |
| `GATEWAY_STORAGE_URL` | no | same default as bridge | Independently replaceable |
| `GATEWAY_MISSION_CONTROL_URL` | no | production Mission Control URL | Absolute `http(s)` MCP base URL |
| `GATEWAY_ARTIFACTS_URL` | no | same default as bridge | Independently replaceable |

Retired: `GATEWAY_API_KEY` (static inbound key). Do not set it for ChatGPT Business OAuth.

### Bridge service (cutover)

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `BRIDGE_SERVICE_TOKEN` | recommended for gateway | — | Non-expiring service credential for **`/mcp/service` only**; value must match Gateway `GATEWAY_BRIDGE_AUTHORIZATION` (with or without `Bearer ` prefix) |
| Existing GitHub OAuth / Redis / JWT vars | **yes** | — | Unchanged; direct Bridge OAuth clients keep using public `/mcp` |

**Verify after deploy:** Gateway health + a live `storage.list_inventory` call
should reach Bridge `/mcp/service` with the service bearer. Public `/mcp` must
still expose GitHub OAuth discovery. Missing/invalid service tokens must return
401 on `/mcp/service` without leaking credentials.

## Local run

```bash
cd hal_legalai_gateway
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..
export GITHUB_OAUTH_CLIENT_ID=...
export GITHUB_OAUTH_CLIENT_SECRET=...
export GATEWAY_PUBLIC_URL=http://localhost:8080
export JWT_SIGNING_KEY=...
export REDIS_HOST=127.0.0.1
export STORAGE_ENCRYPTION_KEY=...   # Fernet key
export GATEWAY_BRIDGE_AUTHORIZATION='Bearer dev-bridge-service-token'
PYTHONPATH=. uvicorn hal_legalai_gateway.server:app --host 0.0.0.0 --port 8080
```

Smoke:

```bash
curl -sS localhost:8080/health | python -m json.tool
curl -sS localhost:8080/registry | python -m json.tool
# MCP requires GitHub OAuth (ChatGPT custom MCP connection)
```

## Tests

From the repository root:

```bash
python -m unittest tests.test_hal_legalai_gateway tests.test_github_actions_bridge_service_auth -v
```

## Docker

```bash
docker build -t hal-legalai-gateway ./hal_legalai_gateway
docker run --rm -p 8080:8080 \
  -e RAILWAY_GIT_COMMIT_SHA="$(git rev-parse HEAD)" \
  -e GITHUB_OAUTH_CLIENT_ID=... \
  -e GITHUB_OAUTH_CLIENT_SECRET=... \
  -e GATEWAY_PUBLIC_URL=https://... \
  -e JWT_SIGNING_KEY=... \
  -e REDIS_HOST=... \
  -e STORAGE_ENCRYPTION_KEY=... \
  -e GATEWAY_BRIDGE_AUTHORIZATION=... \
  hal-legalai-gateway
```

## Railway provisioning (manual)

This repository does **not** auto-create a new Railway service for the gateway.
`hal_legalai_gateway/railway.json` + `Dockerfile` are code-only deploy config.

**Manual steps (operator):**

1. In the Railway project that hosts Mission Control / the bridge, **create a new service** (if not already present from Phase 1).
2. Point the service root / watch path at `hal_legalai_gateway` (Dockerfile builder).
3. Set **required** GitHub OAuth + Redis/JWT/Fernet vars and `GATEWAY_BRIDGE_AUTHORIZATION`.
4. On the Bridge service, set matching `BRIDGE_SERVICE_TOKEN` (protects
   `/mcp/service` only; public `/mcp` OAuth unchanged).
5. Set `GATEWAY_*_URL` overrides as needed; ensure `GATEWAY_MISSION_CONTROL_URL`
   targets MCP mode. Leave `GATEWAY_MCP_PATH` at `/mcp/service` unless
   intentionally overriding the Bridge service path.
6. Confirm Railway injects `RAILWAY_GIT_COMMIT_SHA`.
7. Expose public networking; health check path is `/health`.
8. Verify `GET /health` shows `deployed_commit_sha`, `registered_tools`, `auth.inbound=github_oauth`, and independent `downstream.*` entries.
9. Point ChatGPT Business custom MCP at the gateway `/mcp` OAuth URL **only after** live verification. Do not retire existing plugins until that cutover is confirmed. This change does not claim live cutover.

## Non-goals

- No Case-00 Q1 generation, private benchmark access, recipient-policy, or storage archive mutation inside the gateway
- No replacement of Mission Control, bridge, or B2 business logic
- No automatic Railway service creation from this repo alone
- No retirement of existing ChatGPT / HAL plugins in this phase (cutover is operator-gated after live gateway verification)
